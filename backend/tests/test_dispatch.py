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
