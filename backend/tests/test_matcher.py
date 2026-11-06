"""M4 — the dispatch matcher: pure geometry/filtering (app/dispatch/matcher.py)
plus the candidate-selection query that wraps it (services/dispatch.py's
find_nearby_workers). Radius, trade, availability, cross-company and
ordering cases moved here from test_dispatch.py (G3); the two bare-function
tests at the end have no database at all.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.dispatch import matcher
from app.models import JobOffer, JobStatus, OfferStatus, User, WorkerProfile
from app.models.job import utcnow
from tests.factories import tenant_context
from tests.test_dispatch import (
    JOB_LAT,
    JOB_LNG,
    KM_PER_DEG_LAT,
    add_job,
    add_worker,
    match_ids,
    north_of,
)

# --- pure functions, no database --------------------------------------------------------


def test_haversine_matches_known_distance():
    # Sydney -> Melbourne is ~713.4 km great-circle.
    assert matcher.haversine_km(-33.8688, 151.2093, -37.8136, 144.9631) == pytest.approx(
        713.4, abs=1.0
    )
    assert matcher.haversine_km(JOB_LAT, JOB_LNG, north_of(JOB_LAT, 9.9), JOB_LNG) == (
        pytest.approx(9.9, abs=1e-6)
    )


def test_bounding_box_contains_radius():
    import math

    box = matcher.bounding_box(JOB_LAT, JOB_LNG, 10.0)
    assert box.min_lat < north_of(JOB_LAT, -10) and box.max_lat > north_of(JOB_LAT, 10)
    assert box.min_lng < JOB_LNG < box.max_lng
    # 10 km east must sit inside the longitude window.
    east = JOB_LNG + 10 / (KM_PER_DEG_LAT * math.cos(math.radians(JOB_LAT)))
    assert box.min_lng < east < box.max_lng


def test_bounding_box_near_antimeridian_skips_lng_filter():
    assert matcher.bounding_box(0.0, 179.99, 10.0).min_lng is None


def test_has_trade_is_case_insensitive_on_bare_strings():
    assert matcher.has_trade(["Plumbing", "electrical"], "plumbing")
    assert matcher.has_trade([" Plumbing "], "plumbing")
    assert not matcher.has_trade(["electrical"], "plumbing")
    assert not matcher.has_trade([], "plumbing")


def test_rank_candidates_sorts_by_distance_then_id_and_cuts_the_radius():
    # No ORM, no session: plain transient User rows are enough, since
    # rank_candidates only reads .id off them.
    near, mid, far, tie = (User(id=i) for i in (3, 2, 4, 2))
    rows = [
        (far, north_of(JOB_LAT, 7.0), JOB_LNG),
        (near, north_of(JOB_LAT, 0.5), JOB_LNG),
        (mid, north_of(JOB_LAT, 3.0), JOB_LNG),
        (tie, north_of(JOB_LAT, 3.0), JOB_LNG),  # same distance as mid, lower id
        (User(id=99), north_of(JOB_LAT, 50.0), JOB_LNG),  # outside the radius
    ]

    ranked = matcher.rank_candidates(JOB_LAT, JOB_LNG, rows, max_distance_km=10.0)

    assert [c.user.id for c in ranked] == [near.id, tie.id, mid.id, far.id]
    assert ranked[0].distance_km < ranked[1].distance_km < ranked[-1].distance_km


# --- candidate selection (find_nearby_workers), database-backed ------------------------


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


def test_availability_filter_excludes_unavailable_worker(transactional_db, two_companies):
    """is_available is the worker's own switch, distinct from the admin-set
    active flag already covered by test_inactive_users_and_profiles_excluded
    (app/models/worker_profile.py:27-29)."""
    a, _ = two_companies
    off = add_worker(transactional_db, a.id, "off", 1.0)
    on = add_worker(transactional_db, a.id, "on", 2.0)
    with tenant_context(transactional_db, a.id):
        transactional_db.scalars(
            select(WorkerProfile).where(WorkerProfile.user_id == off.id)
        ).one().is_available = False
        transactional_db.flush()
    job_id = add_job(transactional_db, a.id)

    assert match_ids(transactional_db, a.id, job_id) == [on.id]


def test_workers_already_offered_this_job_excluded(transactional_db, two_companies):
    a, _ = two_companies
    offered = add_worker(transactional_db, a.id, "offered", 1.0)
    other = add_worker(transactional_db, a.id, "other", 2.0)
    job_id = add_job(transactional_db, a.id, status=JobStatus.dispatching)
    with tenant_context(transactional_db, a.id):
        transactional_db.add(
            JobOffer(job_id=job_id, worker_id=offered.id, status=OfferStatus.declined,
                     expires_at=utcnow())
        )
        transactional_db.flush()

    assert match_ids(transactional_db, a.id, job_id) == [other.id]
