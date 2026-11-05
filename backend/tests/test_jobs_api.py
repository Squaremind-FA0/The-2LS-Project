"""HTTP contract of /api/jobs and the worker-side /api/me/* routes."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import Job, JobEvent, JobStatus, Role, Shift, TimeEntry, WorkerProfile
from app.models.job import utcnow
from app.services import dispatch
from tests.factories import WORKER_PASSWORD, auth_header, make_user, tenant_context


def body(**overrides) -> dict:
    return {
        "title": "Fix tap",
        "description": "Kitchen tap drips",
        "trade": "plumbing",
        "address": "1 George St",
        "lat": -33.86,
        "lng": 151.2,
        "scheduled_for": "2026-12-01T09:00:00Z",
        **overrides,
    }


def create(client, user, **overrides) -> dict:
    response = client.post("/api/jobs", json=body(**overrides), headers=auth_header(user))
    assert response.status_code == 201, response.text
    return response.json()


def assigned_job(db, company_id, worker_id, status=JobStatus.assigned) -> int:
    with tenant_context(db, company_id):
        job = Job(
            title="T", trade="plumbing", address="A", lat=1.0, lng=2.0, status=status,
            assigned_worker_id=worker_id,
        )
        db.add(job)
        db.flush()
        return job.id


def put_on_shift(db, company_id, worker_id) -> None:
    now = utcnow()
    with tenant_context(db, company_id):
        db.add(Shift(
            worker_id=worker_id, starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=1),
        ))
        db.flush()


# --- dispatcher / admin CRUD --------------------------------------------------------------


@pytest.mark.parametrize("who", ["dispatcher", "admin"])
def test_staff_can_create_read_update_and_delete(client, two_companies, who):
    a, _ = two_companies
    user = getattr(a, who)
    headers = auth_header(user)

    job = create(client, user)
    assert job["status"] == "draft"
    assert job["version"] == 1
    assert job["created_by"] == user.id
    assert (job["title"], job["description"]) == ("Fix tap", "Kitchen tap drips")
    assert job["assigned_worker_id"] is None

    read = client.get(f"/api/jobs/{job['id']}", headers=headers)
    assert read.status_code == 200
    assert read.json() == job
    assert [j["id"] for j in client.get("/api/jobs", headers=headers).json()] == [job["id"]]

    updated = client.patch(
        f"/api/jobs/{job['id']}", json={"version": 1, "title": "Replace tap"}, headers=headers
    )
    assert updated.status_code == 200
    assert (updated.json()["title"], updated.json()["version"]) == ("Replace tap", 2)
    assert updated.json()["description"] == "Kitchen tap drips"  # untouched

    assert client.delete(f"/api/jobs/{job['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/jobs/{job['id']}", headers=headers).status_code == 404


def test_scheduled_for_and_description_are_optional(client, two_companies):
    a, _ = two_companies
    payload = body()
    del payload["scheduled_for"], payload["description"]
    response = client.post("/api/jobs", json=payload, headers=auth_header(a.dispatcher))
    assert response.status_code == 201
    assert response.json()["scheduled_for"] is None
    assert response.json()["description"] == ""


def test_scheduled_for_can_be_cleared_and_is_normalised_to_naive_utc(client, two_companies):
    a, _ = two_companies
    headers = auth_header(a.dispatcher)
    job = create(client, a.dispatcher)
    url = f"/api/jobs/{job['id']}"

    moved = client.patch(
        url, json={"version": 1, "scheduled_for": "2026-12-01T09:00:00+10:00"}, headers=headers
    ).json()
    assert moved["scheduled_for"] == "2026-11-30T23:00:00"

    cleared = client.patch(url, json={"version": 2, "scheduled_for": None}, headers=headers).json()
    assert cleared["scheduled_for"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"title": "x"},  # no version
        {"version": 1},  # nothing to do
        {"version": 1, "title": ""},
        {"version": 1, "title": None},
        {"version": 1, "trade": None},
        {"version": 1, "lat": 91},
        {"version": 1, "status": "dispatching", "title": "both at once"},
        {"version": 1, "status": None},
        {"version": 1, "status": "assigned"},  # accept-only
        {"version": 1, "status": "unfilled"},  # sweep-only
        {"version": 1, "company_id": 2},
        {"version": 1, "assigned_worker_id": 2},
        {"version": 99, "version_id": 1},
    ],
)
def test_invalid_patch_bodies_are_rejected(client, two_companies, patch):
    a, _ = two_companies
    job = create(client, a.dispatcher)
    response = client.patch(
        f"/api/jobs/{job['id']}", json=patch, headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [{"title": ""}, {"title": "   "}, {"status": "assigned"}, {"created_by": 1}, {"version": 9}],
)
def test_create_rejects_bad_and_smuggled_fields(client, two_companies, overrides):
    a, _ = two_companies
    response = client.post(
        "/api/jobs", json=body(**overrides), headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 422


def test_create_requires_a_title(client, two_companies):
    a, _ = two_companies
    payload = body()
    del payload["title"]
    assert client.post(
        "/api/jobs", json=payload, headers=auth_header(a.dispatcher)
    ).status_code == 422


def test_unavailable_worker_is_not_matched_for_a_job(client, two_companies, transactional_db):
    # Without this, /api/me/availability would be cosmetic.
    a, _ = two_companies
    headers = auth_header(a.worker)
    client.patch("/api/me/location", json={"lat": -33.86, "lng": 151.2}, headers=headers)
    put_on_shift(transactional_db, a.id, a.worker.id)
    with tenant_context(transactional_db, a.id):
        job = Job(title="T", trade="a", address="A", lat=-33.86, lng=151.2)
        transactional_db.add(job)
        transactional_db.flush()
        assert dispatch.find_nearby_workers(transactional_db, job) != []

    client.patch("/api/me/availability", json={"is_available": False}, headers=headers)

    with tenant_context(transactional_db, a.id):
        transactional_db.expire_all()
        assert dispatch.find_nearby_workers(transactional_db, job) == []


def test_only_a_draft_can_be_deleted(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.dispatching)
    response = client.delete(f"/api/jobs/{job_id}", headers=auth_header(a.dispatcher))
    assert response.status_code == 409
    assert client.get(f"/api/jobs/{job_id}", headers=auth_header(a.dispatcher)).status_code == 200


def test_details_of_a_finished_job_cannot_be_edited(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.completed)
    response = client.patch(
        f"/api/jobs/{job_id}", json={"version": 1, "title": "Rewrite"},
        headers=auth_header(a.dispatcher),
    )
    assert response.status_code == 409


# --- cancel -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.draft, JobStatus.assigned, JobStatus.in_progress])
def test_cancel_moves_a_live_job_and_records_the_actor(
    client, two_companies, transactional_db, status
):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, status)
    response = client.post(
        f"/api/jobs/{job_id}/cancel", json={"version": 1}, headers=auth_header(a.admin)
    )
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["version"]) == ("cancelled", 2)
    with tenant_context(transactional_db, a.id):
        [event] = transactional_db.scalars(
            select(JobEvent).where(JobEvent.job_id == job_id)
        ).all()
        assert (event.from_status, event.to_status) == (status, JobStatus.cancelled)
        assert event.actor_id == a.admin.id


@pytest.mark.parametrize(
    "status", [JobStatus.completed, JobStatus.cancelled, JobStatus.unfilled]
)
def test_a_terminal_job_cannot_be_cancelled(client, two_companies, transactional_db, status):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, status)
    response = client.post(
        f"/api/jobs/{job_id}/cancel", json={"version": 1}, headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 400


# --- roles --------------------------------------------------------------------------------


def test_worker_cannot_create_a_job(client, two_companies):
    a, _ = two_companies
    response = client.post("/api/jobs", json=body(), headers=auth_header(a.worker))
    assert response.status_code == 403


def test_worker_is_forbidden_from_every_staff_job_route(client, two_companies):
    a, _ = two_companies
    job = create(client, a.dispatcher)
    url, headers = f"/api/jobs/{job['id']}", auth_header(a.worker)
    assert client.get("/api/jobs", headers=headers).status_code == 403
    assert client.get(url, headers=headers).status_code == 403
    assert client.patch(url, json={"version": 1, "title": "x"}, headers=headers).status_code == 403
    assert client.post(f"{url}/cancel", json={"version": 1}, headers=headers).status_code == 403
    assert client.delete(url, headers=headers).status_code == 403


def test_job_routes_require_authentication(client, two_companies):
    assert client.get("/api/jobs/1").status_code == 401
    assert client.get("/api/me/jobs").status_code == 401
    assert client.patch("/api/me/availability", json={"is_available": False}).status_code == 401


# --- tenancy ------------------------------------------------------------------------------


def test_another_companys_job_does_not_exist(client, two_companies):
    a, b = two_companies
    job = create(client, a.dispatcher)
    url, headers = f"/api/jobs/{job['id']}", auth_header(b.dispatcher)

    assert client.get(url, headers=headers).status_code == 404
    assert client.patch(url, json={"version": 1, "title": "x"}, headers=headers).status_code == 404
    assert client.patch(
        url, json={"version": 1, "status": "cancelled"}, headers=headers
    ).status_code == 404
    assert client.post(f"{url}/cancel", json={"version": 1}, headers=headers).status_code == 404
    assert client.delete(url, headers=headers).status_code == 404
    assert client.get("/api/jobs", headers=headers).json() == []

    # And nothing happened to it.
    assert client.get(url, headers=auth_header(a.dispatcher)).json() == job


def test_worker_cannot_touch_another_companys_job(client, two_companies, transactional_db):
    a, b = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    headers = auth_header(b.worker)
    assert client.post(f"/api/me/jobs/{job_id}/start", headers=headers).status_code == 404
    assert client.post(f"/api/me/jobs/{job_id}/complete", headers=headers).status_code == 404


# --- optimistic concurrency ---------------------------------------------------------------


def test_stale_version_on_details_returns_409_and_loses_nothing(client, two_companies):
    a, _ = two_companies
    headers = auth_header(a.dispatcher)
    job = create(client, a.dispatcher)
    url = f"/api/jobs/{job['id']}"

    assert client.patch(url, json={"version": 1, "title": "First"}, headers=headers).status_code \
        == 200
    stale = client.patch(url, json={"version": 1, "title": "Second"}, headers=headers)

    assert stale.status_code == 409
    current = client.get(url, headers=headers).json()
    assert (current["title"], current["version"]) == ("First", 2)


@pytest.mark.parametrize(
    "send",
    [
        lambda client, url, headers, version: client.patch(
            url, json={"version": version, "status": "cancelled"}, headers=headers
        ),
        lambda client, url, headers, version: client.post(
            f"{url}/cancel", json={"version": version}, headers=headers
        ),
    ],
    ids=["patch-status", "cancel"],
)
def test_stale_version_on_a_status_change_returns_409(client, two_companies, send):
    a, _ = two_companies
    headers = auth_header(a.dispatcher)
    job = create(client, a.dispatcher)
    url = f"/api/jobs/{job['id']}"
    assert client.patch(url, json={"version": 1, "title": "New"}, headers=headers).status_code \
        == 200

    assert send(client, url, headers, 1).status_code == 409
    assert client.get(url, headers=headers).json()["status"] == "draft"


def test_stale_version_outranks_illegal_transition(client, two_companies, transactional_db):
    # Someone holding an old copy needs "reload", not a complaint about a status
    # they have never seen.
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.completed)
    with tenant_context(transactional_db, a.id):
        transactional_db.execute(
            update(Job).where(Job.id == job_id).values(version=5)
            .execution_options(synchronize_session=False)
        )
    response = client.post(
        f"/api/jobs/{job_id}/cancel", json={"version": 1}, headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 409


def test_workers_move_makes_the_dispatchers_copy_stale(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    put_on_shift(transactional_db, a.id, a.worker.id)
    headers = auth_header(a.dispatcher)
    version = client.get(f"/api/jobs/{job_id}", headers=headers).json()["version"]

    assert client.post(
        f"/api/me/jobs/{job_id}/start", headers=auth_header(a.worker)
    ).status_code == 200

    response = client.patch(
        f"/api/jobs/{job_id}", json={"version": version, "title": "Late edit"}, headers=headers
    )
    assert response.status_code == 409


def test_details_edit_loses_to_a_write_after_the_read(transactional_db, two_companies):
    # The conditional UPDATE, not the precheck, must be what stops this.
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    with tenant_context(transactional_db, a.id):
        job = transactional_db.get(Job, job_id)
        assert job.version == 1
        transactional_db.execute(
            update(Job).where(Job.id == job_id).values(version=2)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(dispatch.Conflict):
            dispatch.update_job_details(transactional_db, job_id, 1, {"title": "Lost"})


# --- worker side: /api/me/jobs ------------------------------------------------------------


def test_worker_lists_only_their_own_jobs(client, two_companies, transactional_db):
    a, _ = two_companies
    other = make_user(transactional_db, a.id, "other@a.test", WORKER_PASSWORD, Role.worker)
    mine = assigned_job(transactional_db, a.id, a.worker.id)
    assigned_job(transactional_db, a.id, other.id)

    response = client.get("/api/me/jobs", headers=auth_header(a.worker))

    assert response.status_code == 200
    assert [job["id"] for job in response.json()] == [mine]


def test_me_jobs_is_for_workers(client, two_companies):
    a, _ = two_companies
    assert client.get("/api/me/jobs", headers=auth_header(a.dispatcher)).status_code == 403


def test_worker_starts_and_completes_their_own_job(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    put_on_shift(transactional_db, a.id, a.worker.id)
    headers = auth_header(a.worker)

    started = client.post(f"/api/me/jobs/{job_id}/start", headers=headers)
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"

    completed = client.post(f"/api/me/jobs/{job_id}/complete", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["version"] == 3

    with tenant_context(transactional_db, a.id):
        transactional_db.expire_all()
        # Payroll depends on this: start and complete must record the time worked.
        entry = transactional_db.scalars(
            select(TimeEntry).where(TimeEntry.job_id == job_id)
        ).one()
        assert entry.worker_id == a.worker.id
        assert entry.ended_at is not None
        events = transactional_db.scalars(
            select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
        ).all()
        assert [(e.to_status, e.actor_id) for e in events] == [
            (JobStatus.in_progress, a.worker.id),
            (JobStatus.completed, a.worker.id),
        ]


def test_worker_cannot_start_or_complete_someone_elses_job(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    other = make_user(transactional_db, a.id, "other@a.test", WORKER_PASSWORD, Role.worker)
    put_on_shift(transactional_db, a.id, other.id)
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    headers = auth_header(other)

    assert client.post(f"/api/me/jobs/{job_id}/start", headers=headers).status_code == 404
    assert client.post(f"/api/me/jobs/{job_id}/complete", headers=headers).status_code == 404
    read = client.get(f"/api/jobs/{job_id}", headers=auth_header(a.dispatcher)).json()
    assert read["status"] == "assigned"


def test_worker_cannot_start_a_job_off_shift(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    response = client.post(f"/api/me/jobs/{job_id}/start", headers=auth_header(a.worker))
    assert response.status_code == 409


def test_worker_cannot_complete_a_job_never_started(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    response = client.post(f"/api/me/jobs/{job_id}/complete", headers=auth_header(a.worker))
    assert response.status_code == 404


@pytest.mark.parametrize("who", ["dispatcher", "admin"])
def test_staff_cannot_use_the_worker_start_route(client, two_companies, transactional_db, who):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    response = client.post(
        f"/api/me/jobs/{job_id}/start", headers=auth_header(getattr(a, who))
    )
    assert response.status_code == 403


# --- worker side: availability and location -----------------------------------------------


def _profile(db, company_id, user_id) -> WorkerProfile:
    with tenant_context(db, company_id):
        db.expire_all()
        return db.get(WorkerProfile, user_id)


def test_worker_sets_their_own_availability(client, two_companies, transactional_db):
    a, _ = two_companies
    headers = auth_header(a.worker)

    response = client.patch("/api/me/availability", json={"is_available": False}, headers=headers)

    assert response.status_code == 200
    assert response.json()["is_available"] is False
    assert _profile(transactional_db, a.id, a.worker.id).is_available is False


def test_availability_body_cannot_name_another_worker(client, two_companies):
    a, b = two_companies
    response = client.patch(
        "/api/me/availability", json={"is_available": False, "user_id": b.worker.id},
        headers=auth_header(a.worker),
    )
    assert response.status_code == 422


def test_worker_location_updates_profile_and_dispatch_location_together(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    response = client.patch(
        "/api/me/location", json={"lat": -33.5, "lng": 151.25}, headers=auth_header(a.worker)
    )
    assert response.status_code == 200
    assert (response.json()["lat"], response.json()["lng"]) == (-33.5, 151.25)

    profile = _profile(transactional_db, a.id, a.worker.id)
    assert (profile.last_lat, profile.last_lng) == (-33.5, 151.25)
    assert profile.last_seen_at is not None


def test_legacy_location_route_keeps_the_profile_in_step(client, two_companies, transactional_db):
    a, _ = two_companies
    client.put(
        "/api/workers/me/location", json={"lat": 1.5, "lng": 2.5}, headers=auth_header(a.worker)
    )
    profile = _profile(transactional_db, a.id, a.worker.id)
    assert (profile.last_lat, profile.last_lng) == (1.5, 2.5)


@pytest.mark.parametrize("who", ["dispatcher", "admin"])
def test_me_routes_reject_non_workers(client, two_companies, who):
    a, _ = two_companies
    headers = auth_header(getattr(a, who))
    assert client.patch(
        "/api/me/availability", json={"is_available": False}, headers=headers
    ).status_code == 403
    assert client.patch(
        "/api/me/location", json={"lat": 1, "lng": 2}, headers=headers
    ).status_code == 403


@pytest.mark.parametrize(
    "payload", [{"lat": 91, "lng": 0}, {"lat": 0, "lng": 181}, {"lat": 0}, {}]
)
def test_location_body_is_validated(client, two_companies, payload):
    a, _ = two_companies
    assert client.patch(
        "/api/me/location", json=payload, headers=auth_header(a.worker)
    ).status_code == 422


def test_staff_see_worker_availability_and_location(client, two_companies, transactional_db):
    a, _ = two_companies
    worker_headers = auth_header(a.worker)
    client.patch("/api/me/availability", json={"is_available": False}, headers=worker_headers)
    client.patch("/api/me/location", json={"lat": 1.5, "lng": 2.5}, headers=worker_headers)

    [worker] = client.get("/api/workers", headers=auth_header(a.dispatcher)).json()

    assert worker["worker_profile"]["is_available"] is False
    assert worker["worker_profile"]["last_lat"] == 1.5
    assert worker["worker_profile"]["last_seen_at"] is not None
