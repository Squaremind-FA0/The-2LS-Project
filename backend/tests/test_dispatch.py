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
    User,
    WorkerLocation,
    WorkerProfile,
)
from app.models.job import utcnow
from app.services import dispatch
from tests.factories import WORKER_PASSWORD, auth_header, make_company, make_user, tenant_context

JOB_LAT, JOB_LNG = -33.8688, 151.2093  # Sydney CBD
KM_PER_DEG_LAT = 6371.0088 * math.pi / 180


def north_of(lat: float, km: float) -> float:
    return lat + km / KM_PER_DEG_LAT


# --- data helpers -------------------------------------------------------------------------


def add_worker(db, company_id, key, km=None, *, trades=("plumbing",), age=timedelta(0)):
    """A worker (with profile) optionally located ``km`` north of the job."""
    ref = make_user(
        db, company_id, f"{key}@c{company_id}.test", WORKER_PASSWORD, Role.worker,
        trades=list(trades),
    )
    if km is not None:
        set_location(db, company_id, ref.id, north_of(JOB_LAT, km), JOB_LNG, age=age)
    return ref


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


def test_haversine_matches_known_distance():
    # Sydney -> Melbourne is ~713.4 km great-circle.
    assert dispatch.haversine_km(-33.8688, 151.2093, -37.8136, 144.9631) == pytest.approx(
        713.4, abs=1.0
    )
    assert dispatch.haversine_km(JOB_LAT, JOB_LNG, north_of(JOB_LAT, 9.9), JOB_LNG) == (
        pytest.approx(9.9, abs=1e-6)
    )


def test_bounding_box_contains_radius():
    box = dispatch.bounding_box(JOB_LAT, JOB_LNG, 10.0)
    assert box.min_lat < north_of(JOB_LAT, -10) and box.max_lat > north_of(JOB_LAT, 10)
    assert box.min_lng < JOB_LNG < box.max_lng
    # 10 km east must sit inside the longitude window.
    east = JOB_LNG + 10 / (KM_PER_DEG_LAT * math.cos(math.radians(JOB_LAT)))
    assert box.min_lng < east < box.max_lng


def test_bounding_box_near_antimeridian_skips_lng_filter():
    assert dispatch.bounding_box(0.0, 179.99, 10.0).min_lng is None


def test_radius_boundary_9_9km_matched_10_1km_not(transactional_db, two_companies):
    a, _ = two_companies
    near = add_worker(transactional_db, a.id, "near", 9.9)
    add_worker(transactional_db, a.id, "far", 10.1)
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == [near.id]


def test_cross_company_worker_never_matched(transactional_db, two_companies):
    a, b = two_companies
    add_worker(transactional_db, b.id, "rival", 1.0)
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == []


def test_trade_must_match(transactional_db, two_companies):
    a, _ = two_companies
    add_worker(transactional_db, a.id, "sparky", 1.0, trades=("electrical",))
    job_id = add_job(transactional_db, a.id, trade="plumbing")

    assert match_ids(transactional_db, a.id, job_id) == []


def test_trade_match_is_case_insensitive(transactional_db, two_companies):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "caps", 1.0, trades=("Plumbing",))
    job_id = add_job(transactional_db, a.id, trade="plumbing")

    assert match_ids(transactional_db, a.id, job_id) == [w.id]


def test_stale_location_excluded(transactional_db, two_companies):
    a, _ = two_companies
    fresh = add_worker(transactional_db, a.id, "fresh", 2.0, age=timedelta(minutes=4))
    add_worker(transactional_db, a.id, "stale", 1.0, age=timedelta(hours=1))
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == [fresh.id]


def test_results_sorted_nearest_first(transactional_db, two_companies):
    a, _ = two_companies
    w3 = add_worker(transactional_db, a.id, "w3", 7.0)
    w1 = add_worker(transactional_db, a.id, "w1", 0.5)
    w2 = add_worker(transactional_db, a.id, "w2", 3.0)
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == [w1.id, w2.id, w3.id]


def test_limit_caps_results(transactional_db, two_companies):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 0.5)
    add_worker(transactional_db, a.id, "w2", 3.0)
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id, limit=1) == [w1.id]


def test_inactive_users_and_profiles_excluded(transactional_db, two_companies):
    a, _ = two_companies
    off_user = add_worker(transactional_db, a.id, "offuser", 1.0)
    off_profile = add_worker(transactional_db, a.id, "offprofile", 1.5)
    ok = add_worker(transactional_db, a.id, "ok", 2.0)
    with tenant_context(transactional_db, a.id):
        transactional_db.get(User, off_user.id).is_active = False
        transactional_db.scalars(
            select(WorkerProfile).where(WorkerProfile.user_id == off_profile.id)
        ).one().active = False
        transactional_db.flush()
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == [ok.id]


def test_workers_already_offered_this_job_excluded(transactional_db, two_companies):
    a, _ = two_companies
    offered = add_worker(transactional_db, a.id, "offered", 1.0)
    other = add_worker(transactional_db, a.id, "other", 2.0)
    job_id = add_job(transactional_db, a.id, status=JobStatus.offering)
    with tenant_context(transactional_db, a.id):
        transactional_db.add(
            JobOffer(job_id=job_id, worker_id=offered.id, status=OfferStatus.declined,
                     expires_at=utcnow())
        )
        transactional_db.flush()

    assert match_ids(transactional_db, a.id, job_id) == [other.id]


# --- Phase 4: jobs API, location, offer flow (tasks 11-14) ---------------------------------


def job_body(**overrides) -> dict:
    body = {
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
        f"/api/jobs/{job_id}", json={"status": "offering"}, headers=auth_header(dispatcher)
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


@pytest.mark.parametrize("role", ["worker", "admin"])
def test_only_dispatcher_creates_jobs(client, two_companies, role):
    a, _ = two_companies
    response = client.post("/api/jobs", json=job_body(), headers=auth_header(getattr(a, role)))
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
    offering = add_job(transactional_db, a.id, status=JobStatus.offering)
    add_job(transactional_db, b.id)

    for staff in (a.dispatcher, a.admin):
        listed = client.get("/api/jobs", headers=auth_header(staff))
        assert listed.status_code == 200
        assert [j["id"] for j in listed.json()] == [draft, offering]
    only = client.get("/api/jobs?status=offering", headers=auth_header(a.dispatcher)).json()
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
    assert response.json()["status"] == "offering"
    assert response.json()["offer_round"] == 1
    offers = offers_for(transactional_db, a.id, job_id)
    assert set(offers) == {w1.id, w2.id}
    now = utcnow()
    for offer in offers.values():
        assert offer.status is OfferStatus.pending
        assert now < offer.expires_at <= now + timedelta(seconds=settings.DISPATCH_OFFER_TTL_S)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.draft, "draft"),
        (JobStatus.offering, "offering"),
        (JobStatus.offering, "draft"),
        (JobStatus.cancelled, "offering"),
        (JobStatus.assigned, "cancelled"),
        (JobStatus.unfilled, "offering"),
    ],
)
def test_invalid_transitions_are_400(client, two_companies, transactional_db, start, target):
    a, _ = two_companies
    job_id = add_job(transactional_db, a.id, status=start)
    response = client.patch(f"/api/jobs/{job_id}", json={"status": target},
                            headers=auth_header(a.dispatcher))
    assert response.status_code == 400
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

    response = client.patch(f"/api/jobs/{job_id}", json={"status": "cancelled"},
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
    assert offers[w1.id].status is OfferStatus.expired


def test_accepting_twice_is_409(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    start_offering(client, a.dispatcher, job_id)
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id

    assert client.post(f"/api/offers/{offer_id}/accept", headers=auth_header(w1)).is_success
    assert client.post(f"/api/offers/{offer_id}/accept",
                       headers=auth_header(w1)).status_code == 409


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
    assert offers_for(transactional_db, a.id, job_id)[w2.id].status is OfferStatus.expired


def test_db_rejects_second_accepted_offer_for_a_job(transactional_db, two_companies):
    from sqlalchemy.exc import IntegrityError

    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1")
    w2 = add_worker(transactional_db, a.id, "w2")
    job_id = add_job(transactional_db, a.id, status=JobStatus.offering)
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
    job_id = add_job(transactional_db, a.id, status=JobStatus.offering)
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
        assert final[loser.id].status is OfferStatus.expired
        # Assigned: no new offers can be created for it.
        with tenant_context(db, file_company.id):
            assert dispatch.create_offers(db, job) == []


def test_concurrent_same_offer_double_accept(db_file_based, file_company):
    job_id, (w1,) = _seed_offering_job(db_file_based, file_company, 1)
    offer_id = _offering_via_api(db_file_based, file_company, job_id)[w1.id].id

    responses = fire(
        [("POST", f"/api/offers/{offer_id}/accept", {"headers": auth_header(w1)})] * 4
    )

    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]


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
    assert job.status is JobStatus.offering and job.assigned_worker_id is None
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
    assert fresh(transactional_db, a.id, Job, job_id).status is JobStatus.offering
    again = client.post(f"/api/offers/{declined_id}/decline", headers=auth_header(nearest))
    assert again.status_code == 409


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
    assert job.status is JobStatus.offering and job.offer_round == 2


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
    assert fresh(transactional_db, b.id, Job, b_job).status is JobStatus.offering


@pytest.mark.parametrize("role", ["dispatcher", "worker"])
def test_sweep_is_admin_only(client, two_companies, role):
    a, _ = two_companies
    assert sweep(client, getattr(a, role)).status_code == 403


def test_check_offer_expiry_reports_jobs_without_pending_offers(transactional_db,
                                                                two_companies):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1")
    w2 = add_worker(transactional_db, a.id, "w2")
    stale_job = add_job(transactional_db, a.id, status=JobStatus.offering)
    live_job = add_job(transactional_db, a.id, status=JobStatus.offering)
    with tenant_context(transactional_db, a.id):
        transactional_db.add_all([
            JobOffer(job_id=stale_job, worker_id=w1.id, expires_at=utcnow() - timedelta(1)),
            JobOffer(job_id=live_job, worker_id=w2.id, expires_at=utcnow() + timedelta(1)),
        ])
        transactional_db.flush()
        result = dispatch.check_offer_expiry(transactional_db, a.id)

    assert result.expired_count == 1
    assert result.job_ids == [stale_job]
