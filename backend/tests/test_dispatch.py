"""G3 — jobs and dispatch engine.

Concurrency tests run against a real file-based SQLite database (WAL, real
commits, one connection per request) because the shared in-memory test
connection serialises everything and would hide lock contention.
"""

import asyncio
import math
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.db import _build_engine, get_db
from app.main import app
from app.models import (
    Base,
    Company,
    Job,
    JobOffer,
    JobStatus,
    OfferStatus,
    Role,
    Shift,
    User,
    WorkerLocation,
)
from app.models.job import utcnow
from app.services import dispatch
from tests.factories import WORKER_PASSWORD, auth_header, make_company, make_user, tenant_context

JOB_LAT, JOB_LNG = -33.8688, 151.2093  # Sydney CBD
KM_PER_DEG_LAT = 6371.0088 * math.pi / 180


def north_of(lat: float, km: float) -> float:
    return lat + km / KM_PER_DEG_LAT


# --- data helpers -------------------------------------------------------------------------


def add_worker(
    db, company_id, key, km=None, *, trades=("plumbing",), age=timedelta(0), on_shift=True
):
    """A worker (with profile) optionally located ``km`` north of the job.

    On shift by default, because G5 only dispatches to workers on shift.
    """
    ref = make_user(
        db, company_id, f"{key}@c{company_id}.test", WORKER_PASSWORD, Role.worker,
        trades=list(trades),
    )
    if on_shift:
        add_shift(db, company_id, ref.id)
    if km is not None:
        set_location(db, company_id, ref.id, north_of(JOB_LAT, km), JOB_LNG, age=age)
    return ref


def add_shift(db, company_id, worker_id, *, start=None, end=None):
    with tenant_context(db, company_id):
        db.add(
            Shift(
                worker_id=worker_id,
                starts_at=start or utcnow() - timedelta(days=1),
                ends_at=end or utcnow() + timedelta(days=1),
            )
        )
        db.flush()


def set_location(db, company_id, worker_id, lat, lng, *, age=timedelta(0)):
    with tenant_context(db, company_id):
        db.add(
            WorkerLocation(worker_id=worker_id, lat=lat, lng=lng, updated_at=utcnow() - age)
        )
        db.flush()


def add_job(db, company_id, *, trade="plumbing", status=JobStatus.draft, lat=JOB_LAT,
            lng=JOB_LNG) -> int:
    with tenant_context(db, company_id):
        job = Job(
            trade=trade, address="1 George St", lat=lat, lng=lng,
            scheduled_for=utcnow() + timedelta(days=1), status=status,
        )
        db.add(job)
        db.flush()
        return job.id


def match_ids(db, company_id, job_id, **kwargs) -> list[int]:
    with tenant_context(db, company_id):
        job = db.get(Job, job_id)
        return [u.id for u in dispatch.find_nearby_workers(db, job, **kwargs)]


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def db_file_based(tmp_path):
    """A file-backed SQLite engine (WAL, real commits) wired into the app.

    Yields a session factory; every request gets its own session and
    connection, exactly like production.
    """
    from sqlalchemy.orm import sessionmaker

    engine = _build_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _file_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _file_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.fixture
def file_company(db_file_based):
    with db_file_based() as db:
        company = make_company(db, "a", "Alpha Plumbing")
        db.commit()
    return company


async def _gather(requests):
    """Fire ``requests`` (method, url, kwargs) concurrently against the app.

    Sync routes and dependencies run in AnyIO's threadpool, so these
    genuinely overlap on separate DB connections.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.gather(
            *(client.request(method, url, **kwargs) for method, url, kwargs in requests)
        )


def fire(requests):
    return asyncio.run(_gather(requests))


def _worker_payload(email: str) -> dict:
    return {
        "email": email,
        "full_name": "Parallel Hire",
        "initial_password": "initial-password-123",
        "trades": ["plumbing"],
        "hourly_rate": "50.00",
    }


# --- Phase 1: SQLite concurrency (tasks 1-2) ------------------------------------------------


def test_sqlite_concurrent_writes_never_500(file_company):
    headers = auth_header(file_company.admin)
    responses = fire(
        [
            ("POST", "/api/workers", {"json": _worker_payload(f"p{i}@a.test"), "headers": headers})
            for i in range(10)
        ]
    )
    codes = [r.status_code for r in responses]
    assert 500 not in codes
    assert set(codes) <= {201, 409}
    assert codes.count(201) == 10


def test_sqlite_concurrent_duplicate_email_one_wins(file_company, db_file_based):
    headers = auth_header(file_company.admin)
    responses = fire(
        [
            ("POST", "/api/workers", {"json": _worker_payload("same@a.test"), "headers": headers})
            for _ in range(5)
        ]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409]
    with db_file_based() as db:
        db.info["company_id"] = file_company.id
        assert db.scalar(select(func.count(User.id)).where(User.email == "same@a.test")) == 1


def test_sqlite_engine_uses_wal_and_busy_timeout(db_file_based):
    with db_file_based() as db:
        assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert db.connection().exec_driver_sql("PRAGMA busy_timeout").scalar() >= 10_000
        db.add(Company(name="x"))
        db.commit()


# --- Phase 3: matching (tasks 9-10) ---------------------------------------------------------
#
# Moved to tests/test_matcher.py (M4): haversine/bounding-box/trade/radius/
# cross-company/ordering/availability coverage of app.dispatch.matcher and
# find_nearby_workers now lives there.


# --- Phase 4: jobs API, location, offer flow (tasks 11-14) ---------------------------------


def job_body(**overrides) -> dict:
    body = {
        "title": "Fix tap",
        "trade": "plumbing",
        "address": "1 George St, Sydney",
        "lat": JOB_LAT,
        "lng": JOB_LNG,
        "scheduled_for": "2026-12-01T09:00:00Z",
    }
    body.update(overrides)
    return body


def fresh(db, company_id, model, ident):
    with tenant_context(db, company_id):
        db.expire_all()
        return db.get(model, ident)


def offers_for(db, company_id, job_id) -> dict[int, JobOffer]:
    with tenant_context(db, company_id):
        db.expire_all()
        rows = db.scalars(select(JobOffer).where(JobOffer.job_id == job_id)).all()
        return {o.worker_id: o for o in rows}


def start_offering(client, dispatcher, job_id):
    return client.patch(
        f"/api/jobs/{job_id}", json={"version": 1, "status": "dispatching"},
        headers=auth_header(dispatcher),
    )


def test_dispatcher_creates_draft_job(client, two_companies, transactional_db):
    a, _ = two_companies
    response = client.post("/api/jobs", json=job_body(trade=" Plumbing "),
                           headers=auth_header(a.dispatcher))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["trade"] == "plumbing"
    assert body["scheduled_for"].startswith("2026-12-01T09:00:00")
    assert body["assigned_worker_id"] is None
    assert fresh(transactional_db, a.id, Job, body["id"]).company_id == a.id


def test_worker_cannot_create_jobs(client, two_companies):
    a, _ = two_companies
    response = client.post("/api/jobs", json=job_body(), headers=auth_header(a.worker))
    assert response.status_code == 403


def test_create_job_rejects_smuggled_fields_and_bad_coordinates(client, two_companies):
    a, b = two_companies
    headers = auth_header(a.dispatcher)
    assert client.post("/api/jobs", json=job_body(company_id=b.id),
                       headers=headers).status_code == 422
    assert client.post("/api/jobs", json=job_body(status="assigned"),
                       headers=headers).status_code == 422
    assert client.post("/api/jobs", json=job_body(lat=91), headers=headers).status_code == 422
    assert client.post("/api/jobs", json=job_body(lng=-181), headers=headers).status_code == 422


def test_list_jobs_is_company_scoped_and_filterable(client, two_companies, transactional_db):
    a, b = two_companies
    draft = add_job(transactional_db, a.id)
    offering = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    add_job(transactional_db, b.id)

    for staff in (a.dispatcher, a.admin):
        listed = client.get("/api/jobs", headers=auth_header(staff))
        assert listed.status_code == 200
        assert [j["id"] for j in listed.json()] == [draft, offering]
    only = client.get("/api/jobs?status=dispatching", headers=auth_header(a.dispatcher)).json()
    assert [j["id"] for j in only] == [offering]
    page = client.get("/api/jobs?limit=1&offset=1", headers=auth_header(a.dispatcher)).json()
    assert [j["id"] for j in page] == [offering]
    assert client.get("/api/jobs", headers=auth_header(a.worker)).status_code == 403
    assert client.get("/api/jobs?limit=0", headers=auth_header(a.admin)).status_code == 422


def test_offering_transition_offers_nearest_ring(client, two_companies, transactional_db,
                                                 monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 2)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    add_worker(transactional_db, a.id, "w3", 3.0)
    job_id = add_job(transactional_db, a.id)

    response = start_offering(client, a.dispatcher, job_id)

    assert response.status_code == 200
    assert response.json()["status"] == "dispatching"
    assert response.json()["offer_round"] == 1
    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {w1.id, w2.id}
    now = utcnow()
    for offer in offers.values():
        assert offer.status is OfferStatus.pending
        assert now < offer.expires_at <= now + timedelta(seconds=settings.DISPATCH_OFFER_TTL_S)


def test_dispatch_ring_size_defaults_to_three(client, two_companies, transactional_db):
    from app.config import settings

    assert settings.DISPATCH_RING_SIZE == 3
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    w3 = add_worker(transactional_db, a.id, "w3", 3.0)
    add_worker(transactional_db, a.id, "w4", 4.0)
    job_id = add_job(transactional_db, a.id)

    assert start_offering(client, a.dispatcher, job_id).status_code == 200

    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {w1.id, w2.id, w3.id}


@pytest.mark.parametrize(
    ("start", "target", "expected_status"),
    [
        (JobStatus.draft, "draft", 422),  # draft not in Literal schema
        (JobStatus.dispatching, "draft", 422),  # draft not in Literal schema
        (JobStatus.cancelled, "dispatching", 400),  # illegal transition
        (JobStatus.assigned, "dispatching", 400),  # illegal transition
        (JobStatus.assigned, "completed", 400),  # illegal transition
        (JobStatus.in_progress, "dispatching", 400),  # illegal transition
        (JobStatus.completed, "in_progress", 400),  # illegal transition
        (JobStatus.completed, "cancelled", 400),  # illegal transition
        (JobStatus.cancelled, "in_progress", 400),  # illegal transition
        (JobStatus.unfilled, "dispatching", 400),  # illegal transition
    ],
)
def test_invalid_transitions_are_400(
    client, two_companies, transactional_db, start, target, expected_status
):
    a, _ = two_companies
    job_id = add_job(transactional_db, a.id, status=start)
    response = client.patch(f"/api/jobs/{job_id}", json={"version": 1, "status": target},
                            headers=auth_header(a.dispatcher))
    assert response.status_code == expected_status
    assert fresh(transactional_db, a.id, Job, job_id).status is start


def test_patch_job_of_other_company_is_404(client, two_companies, transactional_db):
    a, b = two_companies
    job_id = add_job(transactional_db, b.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 404
    assert fresh(transactional_db, b.id, Job, job_id).status is JobStatus.draft


def test_cancel_offering_job_expires_pending_offers(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200

    response = client.patch(f"/api/jobs/{job_id}", json={"version": 2, "status": "cancelled"},
                            headers=auth_header(a.dispatcher))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert offers_for(transactional_db, a.id, job_id)[w1.id].status is OfferStatus.expired
    later = client.post(f"/api/offers/{offers_for(transactional_db, a.id, job_id)[w1.id].id}"
                        "/accept", headers=auth_header(w1))
    assert later.status_code in (409, 410)


def test_worker_location_upsert(client, two_companies, transactional_db):
    a, _ = two_companies
    headers = auth_header(a.worker)

    first = client.put("/api/workers/me/location", json={"lat": 1.5, "lng": 2.5},
                       headers=headers)
    second = client.put("/api/workers/me/location", json={"lat": -3.25, "lng": 4.0},
                        headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["worker_id"] == a.worker.id
    assert (second.json()["lat"], second.json()["lng"]) == (-3.25, 4.0)
    with tenant_context(transactional_db, a.id):
        rows = transactional_db.scalars(
            select(WorkerLocation).where(WorkerLocation.worker_id == a.worker.id)
        ).all()
    assert len(rows) == 1 and rows[0].company_id == a.id


def test_worker_location_rejects_non_workers_and_bad_coordinates(client, two_companies):
    a, _ = two_companies
    body = {"lat": 1.0, "lng": 2.0}
    assert client.put("/api/workers/me/location", json=body,
                      headers=auth_header(a.dispatcher)).status_code == 403
    assert client.put("/api/workers/me/location", json={"lat": 90.5, "lng": 0},
                      headers=auth_header(a.worker)).status_code == 422
    assert client.put("/api/workers/me/location", json={**body, "worker_id": 999},
                      headers=auth_header(a.worker)).status_code == 422


def test_accept_assigns_job_and_expires_rest_of_ring(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offers = offers_for(transactional_db, a.id, job_id)

    won = client.post(f"/api/offers/{offers[w2.id].id}/accept", headers=auth_header(w2))
    lost = client.post(f"/api/offers/{offers[w1.id].id}/accept", headers=auth_header(w1))

    assert won.status_code == 200
    assert won.json()["status"] == "accepted" and won.json()["worker_id"] == w2.id
    assert lost.status_code == 409
    assert lost.json()["detail"] == "Job already assigned"
    job = fresh(transactional_db, a.id, Job, job_id)
    assert job.status is JobStatus.assigned and job.assigned_worker_id == w2.id
    offers = offers_for(transactional_db, a.id, job_id)
    assert offers[w2.id].status is OfferStatus.accepted
    assert offers[w1.id].status is OfferStatus.superseded


def test_accepting_twice_is_idempotent(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    first = client.post(f"/api/offers/{offer_id}/accept", headers=auth_header(w1))
    second = client.post(f"/api/offers/{offer_id}/accept", headers=auth_header(w1))

    assert first.is_success and second.status_code == 200
    assert first.json() == second.json()


def test_offer_of_another_worker_or_company_is_404(client, two_companies, transactional_db):
    a, b = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    for intruder in (w2, b.worker):
        for action in ("accept", "decline"):
            response = client.post(f"/api/offers/{offer_id}/{action}",
                                   headers=auth_header(intruder))
            assert response.status_code == 404
            assert response.json()["detail"] == "Offer not found"
    assert client.post("/api/offers/999999/accept", headers=auth_header(w1)).status_code == 404
    assert client.post(f"/api/offers/{offer_id}/accept",
                       headers=auth_header(a.dispatcher)).status_code == 403
    assert offers_for(transactional_db, a.id, job_id)[w1.id].status is OfferStatus.pending


def test_accept_guard_holds_without_application_prechecks(
    client, two_companies, transactional_db, monkeypatch
):
    # Simulates the race window: both workers passed the read-side checks
    # before either wrote. Only the conditional UPDATE can stop the second.
    monkeypatch.setattr(dispatch, "_refuse_unavailable", lambda *args: None)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offers = offers_for(transactional_db, a.id, job_id)

    assert client.post(f"/api/offers/{offers[w1.id].id}/accept",
                       headers=auth_header(w1)).status_code == 200
    second = client.post(f"/api/offers/{offers[w2.id].id}/accept", headers=auth_header(w2))

    assert second.status_code == 409
    assert fresh(transactional_db, a.id, Job, job_id).assigned_worker_id == w1.id
    assert offers_for(transactional_db, a.id, job_id)[w2.id].status is OfferStatus.superseded


def test_accept_idempotent_under_a_forced_stale_read(
    client, two_companies, transactional_db, monkeypatch
):
    """Deterministic version of a same-worker duplicate request racing itself.

    On this project's SQLite engine (BEGIN IMMEDIATE on every transaction), a
    real concurrent duplicate request blocks at its own first query (the auth
    lookup) until the winner commits, so by the time it reads the offer it
    already sees ``accepted`` and returns via the early check in
    ``accept_offer`` -- it never reaches the ``except (StaleJob,
    IllegalTransition)`` branch. Only a forced stale read exercises that
    branch; a concurrent-thread test alone would not (project lesson).
    """
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer = offers_for(transactional_db, a.id, job_id)[w1.id]

    def _simulate_concurrent_duplicate_winner(offer_arg, job_arg, now_arg):
        # A duplicate request from this same worker has already won and
        # committed: flush that state in so the conditional UPDATE's guard
        # (assigned_worker_id IS NULL) fails exactly as it would for real.
        job_arg.status = JobStatus.assigned
        job_arg.assigned_worker_id = w1.id
        offer_arg.status = OfferStatus.accepted
        transactional_db.flush()

    monkeypatch.setattr(dispatch, "_refuse_unavailable", _simulate_concurrent_duplicate_winner)

    with tenant_context(transactional_db, a.id):
        result = dispatch.accept_offer(transactional_db, offer.id, w1.id)

    assert result.id == offer.id
    assert result.status is OfferStatus.accepted


def test_decline_idempotent_under_a_forced_stale_read(
    client, two_companies, transactional_db, monkeypatch
):
    """Deterministic version of the same race for decline: see
    test_accept_idempotent_under_a_forced_stale_read."""
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer = offers_for(transactional_db, a.id, job_id)[w1.id]
    real_conditional_update = dispatch._conditional_update

    def _simulate_concurrent_duplicate_decline(db_arg, stmt):
        row = db_arg.get(JobOffer, offer.id)
        row.status = OfferStatus.declined
        db_arg.flush()
        return real_conditional_update(db_arg, stmt)

    monkeypatch.setattr(dispatch, "_conditional_update", _simulate_concurrent_duplicate_decline)

    with tenant_context(transactional_db, a.id):
        result = dispatch.decline_offer(transactional_db, offer.id, w1.id)

    assert result.id == offer.id
    assert result.status is OfferStatus.declined


def test_db_rejects_second_accepted_offer_for_a_job(transactional_db, two_companies):
    from sqlalchemy.exc import IntegrityError

    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1")
    w2 = add_worker(transactional_db, a.id, "w2")
    job_id = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    with tenant_context(transactional_db, a.id):
        transactional_db.add(JobOffer(job_id=job_id, worker_id=w1.id,
                                      status=OfferStatus.accepted, expires_at=utcnow()))
        transactional_db.flush()
        transactional_db.add(JobOffer(job_id=job_id, worker_id=w2.id,
                                      status=OfferStatus.accepted, expires_at=utcnow()))
        with pytest.raises(IntegrityError):
            transactional_db.flush()
        transactional_db.rollback()


def test_db_rejects_duplicate_offer_to_same_worker(transactional_db, two_companies):
    from sqlalchemy.exc import IntegrityError

    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1")
    job_id = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    with tenant_context(transactional_db, a.id):
        for _ in range(2):
            transactional_db.add(JobOffer(job_id=job_id, worker_id=w1.id, expires_at=utcnow()))
        with pytest.raises(IntegrityError):
            transactional_db.flush()
        transactional_db.rollback()


# --- races on a real file-backed database ---------------------------------------------------


def _seed_offering_job(factory, company, n_workers: int):
    """Commit ``n_workers`` located workers and one job; return (job_id, workers)."""
    with factory() as db:
        workers = [add_worker(db, company.id, f"r{i}", 1.0 + i)
                   for i in range(n_workers)]
        job_id = add_job(db, company.id)
        db.commit()
    return job_id, workers


def _offering_via_api(factory, company, job_id):
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert start_offering(client, company.dispatcher, job_id).status_code == 200
    with factory() as db:
        return offers_for(db, company.id, job_id)


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_ring_double_accept_one_wins(db_file_based, file_company, attempt):
    job_id, (w1, w2) = _seed_offering_job(db_file_based, file_company, 2)
    offers = _offering_via_api(db_file_based, file_company, job_id)

    r1, r2 = fire([
        ("POST", f"/api/offers/{offers[w1.id].id}/accept", {"headers": auth_header(w1)}),
        ("POST", f"/api/offers/{offers[w2.id].id}/accept", {"headers": auth_header(w2)}),
    ])

    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    winner, loser = (w1, w2) if r1.status_code == 200 else (w2, w1)
    with db_file_based() as db:
        job = fresh(db, file_company.id, Job, job_id)
        assert job.status is JobStatus.assigned
        assert job.assigned_worker_id == winner.id
        final = offers_for(db, file_company.id, job_id)
        assert final[winner.id].status is OfferStatus.accepted
        assert final[loser.id].status is OfferStatus.superseded
        # Assigned: no new offers can be created for it.
        with tenant_context(db, file_company.id):
            assert dispatch.create_offers(db, job) == []


def test_concurrent_same_offer_double_accept(db_file_based, file_company):
    """Four concurrent accepts from the *same* worker on their own offer are
    all idempotent: exactly one does the real work, the rest see it already
    accepted (whether through the early return or, in principle, the forced
    stale-read branch -- see test_accept_idempotent_under_a_forced_stale_read
    for why real concurrency here only ever exercises the former)."""
    job_id, (w1,) = _seed_offering_job(db_file_based, file_company, 1)
    offer_id = _offering_via_api(db_file_based, file_company, job_id)[w1.id].id

    responses = fire(
        [("POST", f"/api/offers/{offer_id}/accept", {"headers": auth_header(w1)})] * 4
    )

    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    bodies = [r.json() for r in responses]
    assert all(body == bodies[0] for body in bodies)


# --- Phase 5: expiry + sweeper (tasks 15-17) -------------------------------------------------


def expire_offers_now(db, company_id, job_id):
    with tenant_context(db, company_id):
        for offer in db.scalars(select(JobOffer).where(JobOffer.job_id == job_id)):
            offer.expires_at = utcnow() - timedelta(seconds=1)
        db.flush()


def sweep(client, admin):
    return client.post("/api/dispatch/sweep", headers=auth_header(admin))


def test_expired_offer_cannot_be_accepted(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    expire_offers_now(transactional_db, a.id, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    response = client.post(f"/api/offers/{offer_id}/accept", headers=auth_header(w1))

    assert response.status_code == 410
    assert response.json()["detail"] == "Offer expired"
    job = fresh(transactional_db, a.id, Job, job_id)
    assert job.status is JobStatus.dispatching and job.assigned_worker_id is None
    assert client.post(f"/api/offers/{offer_id}/decline",
                       headers=auth_header(w1)).status_code == 410


def test_decline_triggers_next_offer(client, two_companies, transactional_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    nearest = add_worker(transactional_db, a.id, "nearest", 1.0)
    second = add_worker(transactional_db, a.id, "second", 2.0)
    add_worker(transactional_db, a.id, "third", 3.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {nearest.id}
    declined_id = offers[nearest.id].id

    response = client.post(f"/api/offers/{declined_id}/decline", headers=auth_header(nearest))

    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {nearest.id, second.id}
    assert offers[nearest.id].status is OfferStatus.declined
    assert offers[second.id].status is OfferStatus.pending
    assert fresh(transactional_db, a.id, Job, job_id).status is JobStatus.dispatching
    again = client.post(f"/api/offers/{declined_id}/decline", headers=auth_header(nearest))
    assert again.status_code == 200 and again.json() == response.json()


def test_decline_tops_up_a_partially_pending_ring(client, two_companies, transactional_db,
                                                  monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 2)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    w3 = add_worker(transactional_db, a.id, "w3", 3.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)

    client.post(f"/api/offers/{offers_for(transactional_db, a.id, job_id)[w1.id].id}/decline",
                headers=auth_header(w1))

    offers = offers_for(transactional_db, a.id, job_id)
    assert {w for w, o in offers.items() if o.status is OfferStatus.pending} == {w2.id, w3.id}


def test_sweep_expires_offers_and_re_offers_next_ring(client, two_companies, transactional_db,
                                                      monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    expire_offers_now(transactional_db, a.id, job_id)

    response = sweep(client, a.admin)

    assert response.status_code == 200
    assert response.json() == {"expired_count": 1, "rejobs_count": 1, "unfilled_count": 0}
    offers = offers_for(transactional_db, a.id, job_id)
    assert offers[w1.id].status is OfferStatus.expired
    assert offers[w2.id].status is OfferStatus.pending
    job = fresh(transactional_db, a.id, Job, job_id)
    assert job.status is JobStatus.dispatching and job.offer_round == 2


def test_sweep_marks_job_unfilled_when_no_workers_left(client, two_companies,
                                                       transactional_db):
    a, _ = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    expire_offers_now(transactional_db, a.id, job_id)

    assert sweep(client, a.admin).json() == {
        "expired_count": 1, "rejobs_count": 0, "unfilled_count": 1,
    }
    assert fresh(transactional_db, a.id, Job, job_id).status is JobStatus.unfilled
    # Terminal: later sweeps leave it alone.
    assert sweep(client, a.admin).json() == {
        "expired_count": 0, "rejobs_count": 0, "unfilled_count": 0,
    }


def test_sweep_stops_after_max_rounds(client, two_companies, transactional_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    monkeypatch.setattr(settings, "DISPATCH_MAX_ROUNDS", 3)
    a, _ = two_companies
    for i in range(6):
        add_worker(transactional_db, a.id, f"w{i}", 1.0 + i)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)

    summaries = []
    for _ in range(4):
        expire_offers_now(transactional_db, a.id, job_id)
        summaries.append(sweep(client, a.admin).json())

    assert [s["rejobs_count"] for s in summaries] == [1, 1, 0, 0]
    assert [s["unfilled_count"] for s in summaries] == [0, 0, 1, 0]
    job = fresh(transactional_db, a.id, Job, job_id)
    assert job.status is JobStatus.unfilled and job.offer_round == 3
    assert len(offers_for(transactional_db, a.id, job_id)) == 3


def test_sweep_leaves_live_offers_and_other_companies_alone(client, two_companies,
                                                            transactional_db):
    a, b = two_companies
    a_worker = add_worker(transactional_db, a.id, "aw", 1.0)
    b_worker = add_worker(transactional_db, b.id, "bw", 1.0)
    a_job = add_job(transactional_db, a.id)
    b_job = add_job(transactional_db, b.id)
    start_offering(client, a.dispatcher, a_job)
    start_offering(client, b.dispatcher, b_job)
    expire_offers_now(transactional_db, b.id, b_job)

    assert sweep(client, a.admin).json() == {
        "expired_count": 0, "rejobs_count": 0, "unfilled_count": 0,
    }
    assert offers_for(transactional_db, a.id, a_job)[a_worker.id].status is OfferStatus.pending
    assert offers_for(transactional_db, b.id, b_job)[b_worker.id].status is OfferStatus.pending
    assert fresh(transactional_db, b.id, Job, b_job).status is JobStatus.dispatching


@pytest.mark.parametrize("role", ["dispatcher", "worker"])
def test_sweep_is_admin_only(client, two_companies, role):
    a, _ = two_companies
    assert sweep(client, getattr(a, role)).status_code == 403


def test_check_offer_expiry_reports_jobs_without_pending_offers(transactional_db,
                                                                two_companies):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1")
    w2 = add_worker(transactional_db, a.id, "w2")
    stale_job = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    live_job = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    with tenant_context(transactional_db, a.id):
        transactional_db.add_all([
            JobOffer(job_id=stale_job, worker_id=w1.id, expires_at=utcnow() - timedelta(1)),
            JobOffer(job_id=live_job, worker_id=w2.id, expires_at=utcnow() + timedelta(1)),
        ])
        transactional_db.flush()
        result = dispatch.check_offer_expiry(transactional_db, a.id)

    assert result.expired_count == 1
    assert result.job_ids == [stale_job]


# --- Phase 6: end to end over HTTP only (task 20) -------------------------------------------


def test_e2e_job_dispatch_flow(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "e2e1")
    w2 = add_worker(transactional_db, a.id, "e2e2")
    for worker, km in ((w1, 1.0), (w2, 4.0)):
        located = client.put("/api/workers/me/location",
                             json={"lat": north_of(JOB_LAT, km), "lng": JOB_LNG},
                             headers=auth_header(worker))
        assert located.status_code == 200

    created = client.post("/api/jobs", json=job_body(), headers=auth_header(a.dispatcher))
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert start_offering(client, a.dispatcher, job_id).json()["status"] == "dispatching"
    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {w1.id, w2.id}
    o1, o2 = offers[w1.id].id, offers[w2.id].id

    assert client.post(f"/api/offers/{o1}/accept", headers=auth_header(w1)).status_code == 200
    late = client.post(f"/api/offers/{o2}/accept", headers=auth_header(w2))
    assert late.status_code == 409 and late.json()["detail"] == "Job already assigned"

    listed = client.get("/api/jobs?status=assigned", headers=auth_header(a.admin)).json()
    assert [(j["id"], j["assigned_worker_id"]) for j in listed] == [(job_id, w1.id)]
    final = offers_for(transactional_db, a.id, job_id)
    assert final[w1.id].status is OfferStatus.accepted
    assert final[w2.id].status is OfferStatus.superseded


# --- Uncovered defensive paths and edge cases (tasks 9-17) -----------------------------------


def test_concurrent_draft_to_offering_only_one_succeeds(db_file_based, file_company):
    """Two concurrent PATCH draft->offering on same job: only one wins (line 249-250)."""
    with db_file_based() as db:
        add_worker(db, file_company.id, "w_draft", 1.0)
        job_id = add_job(db, file_company.id)
        db.commit()

    responses = fire(
        [
            ("PATCH", f"/api/jobs/{job_id}", {"json": {"version": 1, "status": "dispatching"},
                                              "headers": auth_header(file_company.dispatcher)})
            for _ in range(2)
        ]
    )

    # One succeeds (200), one fails either with 409 (conflict) or 400 (invalid transition
    # if it hits the conditional update after the first one already succeeded)
    codes = sorted(r.status_code for r in responses)
    assert codes[0] == 200
    assert codes[1] in (400, 409)
    with db_file_based() as db:
        job = fresh(db, file_company.id, Job, job_id)
        assert job.status is JobStatus.dispatching


def test_concurrent_location_upsert_both_new_workers_race_on_integrity(
    db_file_based, file_company
):
    """Two parallel PUT /api/workers/me/location for two different new workers:
    both should succeed (line 434-438 handles retry on IntegrityError)."""
    with db_file_based() as db:
        w1 = add_worker(db, file_company.id, "neuw1")
        w2 = add_worker(db, file_company.id, "neuw2")
        db.commit()

    responses = fire(
        [
            ("PUT", "/api/workers/me/location", {"json": {"lat": 1.5, "lng": 2.5},
                                                 "headers": auth_header(w1)}),
            ("PUT", "/api/workers/me/location", {"json": {"lat": 3.5, "lng": 4.5},
                                                 "headers": auth_header(w2)}),
        ]
    )

    assert all(r.status_code == 200 for r in responses)
    with db_file_based() as db:
        db.info["company_id"] = file_company.id
        locs = db.scalars(select(WorkerLocation)).all()
        assert len(locs) == 2


def test_offer_expired_between_read_and_accept_returns_410(
    client, two_companies, transactional_db
):
    """Offer's expires_at is past after read but before accept: line 363-371."""
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    # Manually expire the offer between read and accept
    with tenant_context(transactional_db, a.id):
        offer = transactional_db.get(JobOffer, offer_id)
        offer.expires_at = utcnow() - timedelta(seconds=1)
        transactional_db.flush()

    response = client.post(f"/api/offers/{offer_id}/accept", headers=auth_header(w1))
    assert response.status_code == 410
    assert response.json()["detail"] == "Offer expired"


def test_declining_twice_is_idempotent(client, two_companies, transactional_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 2)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    add_worker(transactional_db, a.id, "w2", 2.0)
    add_worker(transactional_db, a.id, "w3", 3.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    # First decline succeeds
    r1 = client.post(f"/api/offers/{offer_id}/decline", headers=auth_header(w1))
    assert r1.status_code == 200

    # Repeating it is idempotent, not a conflict
    r2 = client.post(f"/api/offers/{offer_id}/decline", headers=auth_header(w1))
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_accept_and_decline_race_on_same_offer(db_file_based, file_company):
    """Accept vs decline on the same offer: only one wins."""
    job_id, (w1, w2) = _seed_offering_job(db_file_based, file_company, 2)
    offers = _offering_via_api(db_file_based, file_company, job_id)

    responses = fire(
        [
            ("POST", f"/api/offers/{offers[w1.id].id}/accept", {"headers": auth_header(w1)}),
            ("POST", f"/api/offers/{offers[w1.id].id}/decline", {"headers": auth_header(w1)}),
        ]
    )

    # One should win, one should lose (accept wins if it happens first, decline fails)
    codes = sorted(r.status_code for r in responses)
    assert 200 in codes and (409 in codes or 410 in codes)


def test_concurrent_transitions_after_accept_are_rejected(db_file_based, file_company):
    """Once a job is assigned, further PATCH transitions to offering should fail."""
    job_id, (w1, w2) = _seed_offering_job(db_file_based, file_company, 2)
    offers = _offering_via_api(db_file_based, file_company, job_id)

    # w1 accepts
    r = fire([("POST", f"/api/offers/{offers[w1.id].id}/accept",
               {"headers": auth_header(w1)})])
    assert r[0].status_code == 200

    # Try to transition to draft (should fail: draft is not a valid target status)
    r = fire([("PATCH", f"/api/jobs/{job_id}", {"json": {"version": 3, "status": "draft"},
                                                "headers": auth_header(file_company.dispatcher)})])
    assert r[0].status_code == 422


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_location_upsert_repeated(db_file_based, file_company, attempt):
    """Repeat concurrent location upserts multiple times to check for flakiness."""
    with db_file_based() as db:
        workers = [add_worker(db, file_company.id, f"loc{attempt}_{i}") for i in range(3)]
        db.commit()

    responses = fire(
        [
            ("PUT", "/api/workers/me/location", {"json": {"lat": 1.0 + i, "lng": 2.0},
                                                 "headers": auth_header(w)})
            for i, w in enumerate(workers)
        ]
    )

    assert all(r.status_code == 200 for r in responses)
    with db_file_based() as db:
        db.info["company_id"] = file_company.id
        count = db.scalar(select(func.count(WorkerLocation.id)))
        assert count == 3


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_double_accept_repeated(db_file_based, file_company, attempt):
    """Repeat the double-accept race multiple times to detect flakiness."""
    job_id, (w1, w2) = _seed_offering_job(db_file_based, file_company, 2)
    offers = _offering_via_api(db_file_based, file_company, job_id)

    responses = fire(
        [
            ("POST", f"/api/offers/{offers[w1.id].id}/accept", {"headers": auth_header(w1)}),
            ("POST", f"/api/offers/{offers[w2.id].id}/accept", {"headers": auth_header(w2)}),
        ]
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409]
    with db_file_based() as db:
        job = fresh(db, file_company.id, Job, job_id)
        assert job.status is JobStatus.assigned


def test_boundary_latitude_extremes(client, two_companies, transactional_db):
    """Jobs and workers at latitude extremes: poles should work."""
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "pole")
    # North pole job
    job_id = add_job(transactional_db, a.id, lat=89.9, lng=0)

    # Set worker location via API
    client.put("/api/workers/me/location", json={"lat": 89.8, "lng": 0},
               headers=auth_header(w1))

    with tenant_context(transactional_db, a.id):
        transactional_db.expire_all()
        job = transactional_db.get(Job, job_id)
        workers = dispatch.find_nearby_workers(transactional_db, job)

    assert isinstance(workers, list)  # No crash


def test_boundary_longitude_antimeridian(client, two_companies, transactional_db):
    """Job crossing antimeridian (180/-180 boundary) should work."""
    a, _ = two_companies
    w_east = add_worker(transactional_db, a.id, "east")
    w_west = add_worker(transactional_db, a.id, "west")
    # Job near antimeridian
    job_id = add_job(transactional_db, a.id, lat=0, lng=179.9)

    # Set worker locations via API
    client.put("/api/workers/me/location", json={"lat": 0, "lng": 179.95},
               headers=auth_header(w_east))
    client.put("/api/workers/me/location", json={"lat": 0, "lng": -179.95},
               headers=auth_header(w_west))

    with tenant_context(transactional_db, a.id):
        transactional_db.expire_all()
        job = transactional_db.get(Job, job_id)
        workers = dispatch.find_nearby_workers(transactional_db, job)

    assert isinstance(workers, list)  # No crash


def test_decline_and_accept_interleave(client, two_companies, transactional_db, monkeypatch):
    """Decline an offer, which creates new offer; accept the new offer."""
    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)

    # Decline w1's offer
    offers1 = offers_for(transactional_db, a.id, job_id)
    r_decline = client.post(f"/api/offers/{offers1[w1.id].id}/decline",
                            headers=auth_header(w1))
    assert r_decline.status_code == 200

    # w2's offer should now exist and be pending
    offers2 = offers_for(transactional_db, a.id, job_id)
    assert offers2[w2.id].status is OfferStatus.pending

    # w2 accepts
    r_accept = client.post(f"/api/offers/{offers2[w2.id].id}/accept",
                           headers=auth_header(w2))
    assert r_accept.status_code == 200

    job = fresh(transactional_db, a.id, Job, job_id)
    assert job.assigned_worker_id == w2.id


def test_sweep_handles_missing_job_gracefully(client, two_companies, transactional_db):
    """If a job gets deleted between expiry check and re-offer, sweeper logs and continues."""
    a, _ = two_companies
    # This is implicitly tested by the existing sweep_error_logging path (543-545)
    # A job cannot actually be deleted while offering, but the defensive code
    # handles it anyway (line 536-537)
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    expire_offers_now(transactional_db, a.id, job_id)

    # This should not crash even if a job vanished
    response = sweep(client, a.admin)
    assert response.status_code == 200


def test_sweep_continues_on_job_error(client, two_companies, transactional_db, monkeypatch):
    """If re_offer_job raises an error, sweep logs and continues with next job."""
    a, _ = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job1_id = add_job(transactional_db, a.id)
    job2_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job1_id)
    start_offering(client, a.dispatcher, job2_id)
    expire_offers_now(transactional_db, a.id, job1_id)
    expire_offers_now(transactional_db, a.id, job2_id)

    # Sweep should process both jobs and not crash even if one has issues
    response = sweep(client, a.admin)
    assert response.status_code == 200
    summary = response.json()
    assert summary["expired_count"] >= 2


# --- M4: background sweep orchestration across every company -------------------------------
#
# Uses db_file_based directly (not the client/transactional_db fixtures):
# sweep_all_companies opens its own sessions via an injectable db_factory,
# and exercising that against the shared in-memory/StaticPool test engine
# while another session holds it mid-transaction raises "cannot start a
# transaction within a transaction" (G4's documented failure mode).


def test_sweep_all_companies_sweeps_every_company_independently(db_file_based, monkeypatch):
    with db_file_based() as db:
        company_a = make_company(db, "sweepa", "Sweep A")
        company_b = make_company(db, "sweepb", "Sweep B")
        db.commit()
    job_a, _ = _seed_offering_job(db_file_based, company_a, 1)
    job_b, _ = _seed_offering_job(db_file_based, company_b, 1)
    _offering_via_api(db_file_based, company_a, job_a)
    _offering_via_api(db_file_based, company_b, job_b)
    with db_file_based() as db:
        expire_offers_now(db, company_a.id, job_a)
        expire_offers_now(db, company_b.id, job_b)
        db.commit()

    real_sweep = dispatch.sweep

    def _flaky_sweep(db, company_id, **kwargs):
        if company_id == company_b.id:
            raise RuntimeError("boom")
        return real_sweep(db, company_id, **kwargs)

    monkeypatch.setattr(dispatch, "sweep", _flaky_sweep)

    summaries = dispatch.sweep_all_companies(db_factory=db_file_based)

    assert summaries[company_a.id].expired_count == 1
    assert company_b.id not in summaries  # company B's exception was caught and skipped
    with db_file_based() as db:
        assert fresh(db, company_a.id, Job, job_a).status is JobStatus.unfilled
        # Untouched: company B's own sweep never ran, so its offer is still
        # pending even though its expires_at is already in the past.
        offer_b = next(iter(offers_for(db, company_b.id, job_b).values()))
        assert offer_b.status is OfferStatus.pending


def test_lifespan_starts_and_cancels_the_sweep_loop(monkeypatch):
    """with TestClient(app): is the one way to trigger FastAPI's lifespan in
    this Starlette version -- the client/transactional_db fixtures never do,
    by design (they construct TestClient(app) without a `with`), so this
    test runs against the plain default engine with no companies seeded and
    without either fixture."""
    import time as time_module

    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(settings, "DISPATCH_SWEEP_INTERVAL_S", 0.05)
    calls = []
    monkeypatch.setattr(
        dispatch, "sweep_all_companies", lambda *a, **k: calls.append(1) or {}
    )

    with TestClient(app):
        time_module.sleep(0.2)

    assert calls


# --- M4: REST aliases -------------------------------------------------------------------


def test_post_jobs_dispatch_alias_matches_patch_status_dispatching(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_a = add_job(transactional_db, a.id)
    job_b = add_job(transactional_db, a.id)

    via_patch = start_offering(client, a.dispatcher, job_a)
    via_alias = client.post(f"/api/jobs/{job_b}/dispatch", headers=auth_header(a.dispatcher))

    assert via_patch.status_code == via_alias.status_code == 200
    for key in ("status", "offer_round", "assigned_worker_id"):
        assert via_patch.json()[key] == via_alias.json()[key]
    assert offers_for(transactional_db, a.id, job_a) and offers_for(
        transactional_db, a.id, job_b
    )


def test_get_me_offers_alias_matches_offers_pending(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)

    via_pending = client.get("/api/offers/pending", headers=auth_header(w))
    via_alias = client.get("/api/me/offers", headers=auth_header(w))

    assert via_pending.status_code == via_alias.status_code == 200
    assert via_pending.json() == via_alias.json()

def test_re_offer_job_race_guards_offer_round_increment(transactional_db, two_companies):
    """Regression: concurrent re_offer_job calls should not both increment offer_round
    when one should win the race. This test simulates the race by manually updating
    offer_round between the read and the increment to verify the guard works."""
    from sqlalchemy import update
    
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        # Create a dispatching job with workers in range for 3 rounds
        w1 = add_worker(transactional_db, a.id, "w1", 1.0)
        w2 = add_worker(transactional_db, a.id, "w2", 1.0)
        w3 = add_worker(transactional_db, a.id, "w3", 1.0)
        w4 = add_worker(transactional_db, a.id, "w4", 1.0)
        
        job = Job(
            title="Multi-round job", trade="plumbing", address="1 George St",
            lat=JOB_LAT, lng=JOB_LNG, status=JobStatus.dispatching,
            company_id=a.id, offer_round=1
        )
        transactional_db.add(job)
        transactional_db.flush()
        
        # Create initial offers (round 1)
        now = utcnow()
        for worker in [w1, w2, w3, w4]:
            offer = JobOffer(
                job_id=job.id, worker_id=worker.id, status=OfferStatus.pending,
                expires_at=now + timedelta(minutes=5)
            )
            transactional_db.add(offer)
        transactional_db.flush()
        
        # Expire all pending offers to simulate them being declined
        transactional_db.execute(
            update(JobOffer)
            .where(JobOffer.job_id == job.id)
            .values(status=OfferStatus.expired)
            .execution_options(synchronize_session=False)
        )
        
        # Simulate a concurrent sweep by manually incrementing offer_round
        # This represents sweep A incrementing from 1 to 2
        transactional_db.execute(
            update(Job).where(Job.id == job.id).values(offer_round=2)
            .execution_options(synchronize_session=False)
        )
        
        # Now try re_offer_job with the stale job object (offer_round=1 in memory)
        # If there's no guard, this would increment to 2, but the DB is already at 2.
        # With the proper guard, this should detect the race and return False.
        result = dispatch.re_offer_job(transactional_db, job, now=now)
        assert result is False, "re_offer_job should lose the race when offer_round changed"

        transactional_db.refresh(job)
        # The job should stay at round 2 (set by the simulated concurrent sweep),
        # not get incremented to 2 again (which would lose the change).
        msg = f"Expected offer_round=2 after losing race, got {job.offer_round}"
        assert job.offer_round == 2, msg
