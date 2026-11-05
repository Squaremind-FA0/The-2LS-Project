"""G5 — roster: shifts, the on-shift dispatch filter, and clock-in/out."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models import Job, JobOffer, JobStatus, Shift, TimeEntry
from app.models.job import utcnow
from app.services import dispatch
from app.services import time_entries as entries_service
from tests import test_dispatch as td
from tests.factories import auth_header, tenant_context

# Re-exported so pytest collects them for this module's concurrency tests.
db_file_based = td.db_file_based
file_company = td.file_company

BASE = datetime(2030, 1, 7, 9, 0)


def iso(value: datetime) -> str:
    return value.isoformat()


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


def post_shift(client, worker, start: datetime, end: datetime):
    return client.post(
        "/api/shifts",
        json={"starts_at": iso(start), "ends_at": iso(end)},
        headers=auth_header(worker),
    )


# --- Phase 1: shift API and overlap guard (tasks 7-10) --------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0, 8),  # identical
        (-1, 1),  # overlaps the start
        (7, 9),  # overlaps the end
        (2, 3),  # inside
        (-1, 9),  # encloses
    ],
)
def test_shift_overlap_guard_rejects_overlap_with_409(client, two_companies, start, end):
    a, _ = two_companies
    assert post_shift(client, a.worker, BASE, BASE + hours(8)).status_code == 201

    response = post_shift(client, a.worker, BASE + hours(start), BASE + hours(end))

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Shift overlaps existing shift for this worker")
    assert "new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at" in (
        response.json()["detail"]
    )


def test_adjacent_shifts_do_not_overlap_because_the_interval_is_half_open(client, two_companies):
    a, _ = two_companies
    assert post_shift(client, a.worker, BASE, BASE + hours(8)).status_code == 201
    assert post_shift(client, a.worker, BASE + hours(8), BASE + hours(16)).status_code == 201
    assert post_shift(client, a.worker, BASE - hours(8), BASE).status_code == 201


def test_overlap_is_checked_per_worker_only(client, two_companies, transactional_db):
    a, b = two_companies
    colleague = td.add_worker(transactional_db, a.id, "colleague")
    assert post_shift(client, a.worker, BASE, BASE + hours(8)).status_code == 201
    assert post_shift(client, colleague, BASE, BASE + hours(8)).status_code == 201
    assert post_shift(client, b.worker, BASE, BASE + hours(8)).status_code == 201


@pytest.mark.parametrize("length", [0, -1])
def test_shift_must_start_before_it_ends(client, two_companies, length):
    a, _ = two_companies
    response = post_shift(client, a.worker, BASE, BASE + hours(length))
    assert response.status_code == 400
    assert response.json()["detail"] == "starts_at must be before ends_at"


def test_shift_response_and_stored_owner_come_from_the_token(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    response = post_shift(client, a.worker, BASE, BASE + hours(8))
    body = response.json()
    assert response.status_code == 201
    assert body["worker_id"] == a.worker.id
    assert set(body) == {"id", "worker_id", "starts_at", "ends_at", "created_at"}
    with tenant_context(transactional_db, a.id):
        shift = transactional_db.scalars(select(Shift)).one()
        assert (shift.company_id, shift.worker_id) == (a.id, a.worker.id)


def test_shift_rejects_smuggled_owner_fields(client, two_companies):
    a, b = two_companies
    response = client.post(
        "/api/shifts",
        json={"starts_at": iso(BASE), "ends_at": iso(BASE + hours(1)), "worker_id": b.worker.id},
        headers=auth_header(a.worker),
    )
    assert response.status_code == 422


def test_shift_times_are_stored_as_naive_utc(client, two_companies, transactional_db):
    a, _ = two_companies
    sydney = timezone(timedelta(hours=11))
    start = BASE.replace(tzinfo=sydney)
    response = client.post(
        "/api/shifts",
        json={"starts_at": iso(start), "ends_at": iso(start + hours(1))},
        headers=auth_header(a.worker),
    )
    assert response.status_code == 201
    with tenant_context(transactional_db, a.id):
        shift = transactional_db.scalars(select(Shift)).one()
        assert shift.starts_at == start.astimezone(UTC).replace(tzinfo=None)


@pytest.mark.parametrize("role", ["admin", "dispatcher"])
def test_only_workers_use_the_shift_api(client, two_companies, role):
    a, _ = two_companies
    user = getattr(a, role)
    assert post_shift(client, user, BASE, BASE + hours(1)).status_code == 403
    assert client.get("/api/shifts", headers=auth_header(user)).status_code == 403


def test_worker_lists_only_own_shifts_with_paging(client, two_companies, transactional_db):
    a, _ = two_companies
    colleague = td.add_worker(transactional_db, a.id, "colleague")
    for day in range(3):
        start = BASE + timedelta(days=day)
        assert post_shift(client, a.worker, start, start + hours(8)).status_code == 201
    assert post_shift(client, colleague, BASE, BASE + hours(8)).status_code == 201
    headers = auth_header(a.worker)

    listed = client.get("/api/shifts", headers=headers).json()
    assert [s["starts_at"] for s in listed] == [
        iso(BASE + timedelta(days=day)) for day in range(3)
    ]
    assert {s["worker_id"] for s in listed} == {a.worker.id}

    page = client.get("/api/shifts?limit=1&offset=1", headers=headers).json()
    assert [s["starts_at"] for s in page] == [iso(BASE + timedelta(days=1))]

    own = client.get(f"/api/shifts?worker_id={a.worker.id}", headers=headers)
    assert own.status_code == 200 and len(own.json()) == 3
    other = client.get(f"/api/shifts?worker_id={colleague.id}", headers=headers)
    assert other.status_code == 403


def test_concurrent_overlapping_shifts_only_one_wins(db_file_based, file_company):
    body = {"starts_at": iso(BASE), "ends_at": iso(BASE + hours(8))}
    responses = td.fire(
        [("POST", "/api/shifts", {"json": body, "headers": auth_header(file_company.worker)})] * 4
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


# --- Phase 2: on-shift dispatch filter (tasks 11-12) ----------------------------------------


def test_off_shift_worker_is_never_offered_even_if_nearest(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    off_shift = td.add_worker(transactional_db, a.id, "offshift", 0.5, on_shift=False)
    on_shift = td.add_worker(transactional_db, a.id, "onshift", 5.0)
    job_id = td.add_job(transactional_db, a.id)

    assert td.match_ids(transactional_db, a.id, job_id) == [on_shift.id]

    assert td.start_offering(client, a.dispatcher, job_id).status_code == 200
    with tenant_context(transactional_db, a.id):
        offered = transactional_db.scalars(
            select(JobOffer.worker_id).where(JobOffer.job_id == job_id)
        ).all()
    assert off_shift.id not in offered
    assert offered == [on_shift.id]


@pytest.mark.parametrize(
    ("start", "end", "matched"),
    [
        (-1, 1, True),  # covers now
        (0, 1, True),  # starts_at is inclusive
        (-1, 0, False),  # ends_at is exclusive
        (-3, -1, False),  # already over
        (1, 3, False),  # not started
    ],
)
def test_on_shift_filter_uses_the_half_open_window(
    transactional_db, two_companies, start, end, matched
):
    a, _ = two_companies
    now = utcnow()
    worker = td.add_worker(transactional_db, a.id, "boundary", 1.0, on_shift=False)
    td.add_shift(
        transactional_db, a.id, worker.id, start=now + hours(start), end=now + hours(end)
    )
    job_id = td.add_job(transactional_db, a.id)

    result = td.match_ids(transactional_db, a.id, job_id, now=now)

    assert result == ([worker.id] if matched else [])


# --- Phase 3: job clock-in/out and state machine (tasks 13-19) ------------------------------


def assigned_job(db, company_id, worker_id, status=JobStatus.assigned) -> int:
    job_id = td.add_job(db, company_id, status=status)
    with tenant_context(db, company_id):
        db.get(Job, job_id).assigned_worker_id = worker_id
        db.flush()
    return job_id


def on_shift_worker_job(db, company, status=JobStatus.assigned) -> int:
    td.add_shift(db, company.id, company.worker.id)
    return assigned_job(db, company.id, company.worker.id, status)


def clock(client, action, job_id, user):
    return client.post(f"/api/jobs/{job_id}/clock-{action}", headers=auth_header(user))


def time_entries(db, company_id, job_id) -> list[TimeEntry]:
    with tenant_context(db, company_id):
        db.expire_all()
        return list(db.scalars(select(TimeEntry).where(TimeEntry.job_id == job_id)))


def test_clock_in_requires_assigned_and_moves_job_to_in_progress(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)

    before = utcnow()
    response = clock(client, "in", job_id, a.worker)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "job_id", "worker_id", "started_at", "ended_at"}
    assert (body["job_id"], body["worker_id"], body["ended_at"]) == (job_id, a.worker.id, None)
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.in_progress
    entry = time_entries(transactional_db, a.id, job_id)[0]
    assert before <= entry.started_at <= utcnow()


@pytest.mark.parametrize(
    "status",
    [JobStatus.offering, JobStatus.in_progress, JobStatus.completed, JobStatus.cancelled],
)
def test_clock_in_refuses_a_job_that_is_not_assigned_status(
    client, two_companies, transactional_db, status
):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a, status)

    assert clock(client, "in", job_id, a.worker).status_code == 409
    assert td.fresh(transactional_db, a.id, Job, job_id).status is status
    assert time_entries(transactional_db, a.id, job_id) == []


def test_clock_in_without_on_shift_shift_is_409(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    now = utcnow()
    td.add_shift(transactional_db, a.id, a.worker.id, start=now - hours(3), end=now - hours(1))

    response = clock(client, "in", job_id, a.worker)

    assert response.status_code == 409
    assert response.json()["detail"] == "Worker is not on shift"
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.assigned
    assert time_entries(transactional_db, a.id, job_id) == []


def test_another_workers_shift_does_not_put_this_worker_on_shift(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    td.add_worker(transactional_db, a.id, "colleague")  # on shift
    job_id = assigned_job(transactional_db, a.id, a.worker.id)
    assert clock(client, "in", job_id, a.worker).json()["detail"] == "Worker is not on shift"


def test_clock_in_twice_is_409(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    assert clock(client, "in", job_id, a.worker).status_code == 200

    again = clock(client, "in", job_id, a.worker)

    assert again.status_code == 409
    assert again.json()["detail"] == "Job already has a TimeEntry"
    assert len(time_entries(transactional_db, a.id, job_id)) == 1


@pytest.mark.parametrize("action", ["in", "out"])
def test_clock_is_refused_for_another_workers_job_and_hidden_across_companies(
    client, two_companies, transactional_db, action
):
    a, b = two_companies
    colleague = td.add_worker(transactional_db, a.id, "colleague")
    job_id = on_shift_worker_job(transactional_db, a)

    refused = clock(client, action, job_id, colleague)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Job not assigned to this worker"
    assert clock(client, action, job_id, b.worker).status_code == 404
    assert clock(client, action, 999_999, a.worker).status_code == 404


@pytest.mark.parametrize("role", ["admin", "dispatcher"])
@pytest.mark.parametrize("action", ["in", "out"])
def test_only_workers_clock(client, two_companies, transactional_db, action, role):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    assert clock(client, action, job_id, getattr(a, role)).status_code == 403


def test_clock_out_requires_in_progress_and_moves_job_to_completed(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    started = clock(client, "in", job_id, a.worker).json()["started_at"]

    response = clock(client, "out", job_id, a.worker)

    assert response.status_code == 200
    body = response.json()
    assert body["started_at"] == started
    assert body["ended_at"] is not None
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.completed


def test_clock_out_before_clock_in_is_404(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    response = clock(client, "out", job_id, a.worker)
    assert response.status_code == 404
    assert response.json()["detail"] == "Job or TimeEntry not found"
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.assigned


def test_clock_out_twice_is_409(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    clock(client, "in", job_id, a.worker)
    first = clock(client, "out", job_id, a.worker).json()["ended_at"]

    again = clock(client, "out", job_id, a.worker)

    assert again.status_code == 409
    assert again.json()["detail"] == "TimeEntry already ended"
    assert time_entries(transactional_db, a.id, job_id)[0].ended_at.isoformat() == first


def test_clock_out_is_allowed_after_the_shift_ended(client, two_companies, transactional_db):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.in_progress)
    with tenant_context(transactional_db, a.id):
        transactional_db.add(
            TimeEntry(job_id=job_id, worker_id=a.worker.id, started_at=utcnow() - hours(1))
        )
        transactional_db.flush()
    assert clock(client, "out", job_id, a.worker).status_code == 200


def test_clock_out_of_a_job_cancelled_mid_work_keeps_the_hours_and_the_status(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    clock(client, "in", job_id, a.worker)
    cancel = client.patch(
        f"/api/jobs/{job_id}", json={"status": "cancelled"}, headers=auth_header(a.dispatcher)
    )
    assert cancel.status_code == 200

    response = clock(client, "out", job_id, a.worker)

    assert response.status_code == 200 and response.json()["ended_at"] is not None
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.cancelled


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.assigned, "in_progress"),
        (JobStatus.assigned, "cancelled"),
        (JobStatus.in_progress, "completed"),
        (JobStatus.in_progress, "cancelled"),
    ],
)
def test_dispatcher_can_drive_the_extended_transitions(
    client, two_companies, transactional_db, start, target
):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, start)
    response = client.patch(
        f"/api/jobs/{job_id}", json={"status": target}, headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 200
    assert response.json()["status"] == target


def test_db_rejects_a_second_time_entry_for_a_job(transactional_db, two_companies):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.in_progress)
    with tenant_context(transactional_db, a.id):
        transactional_db.add_all(
            [TimeEntry(job_id=job_id, worker_id=a.worker.id, started_at=utcnow()) for _ in "ab"]
        )
        with pytest.raises(IntegrityError):
            transactional_db.flush()
        transactional_db.rollback()


# SQLite serialises whole requests, so a real race never reaches the race guards
# below. Stale objects in the session reproduce what a concurrent writer does
# on Postgres, deterministically.


def test_clock_in_refuses_a_job_cancelled_after_it_was_read(transactional_db, two_companies):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    with tenant_context(transactional_db, a.id):
        stale = transactional_db.get(Job, job_id)
        assert stale.status is JobStatus.assigned
        transactional_db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.cancelled)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(dispatch.Conflict, match="changed concurrently"):
            entries_service.clock_in(transactional_db, job_id, a.worker.id)
    assert time_entries(transactional_db, a.id, job_id) == []


def test_clock_in_falls_back_on_the_unique_constraint(
    transactional_db, two_companies, monkeypatch
):
    a, _ = two_companies
    job_id = on_shift_worker_job(transactional_db, a)
    with tenant_context(transactional_db, a.id):
        transactional_db.add(TimeEntry(job_id=job_id, worker_id=a.worker.id, started_at=utcnow()))
        transactional_db.commit()
        monkeypatch.setattr(entries_service, "_entry_for", lambda db, job_id: None)
        with pytest.raises(dispatch.Conflict, match="already has a TimeEntry"):
            entries_service.clock_in(transactional_db, job_id, a.worker.id)
    assert len(time_entries(transactional_db, a.id, job_id)) == 1
    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.assigned


def test_clock_out_refuses_an_entry_closed_after_it_was_read(transactional_db, two_companies):
    a, _ = two_companies
    job_id = assigned_job(transactional_db, a.id, a.worker.id, JobStatus.in_progress)
    closed_at = utcnow() - hours(1)
    with tenant_context(transactional_db, a.id):
        stale = TimeEntry(job_id=job_id, worker_id=a.worker.id, started_at=closed_at - hours(1))
        transactional_db.add(stale)
        transactional_db.flush()
        assert stale.ended_at is None
        transactional_db.execute(
            update(TimeEntry)
            .where(TimeEntry.id == stale.id)
            .values(ended_at=closed_at)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(dispatch.Conflict, match="already ended"):
            entries_service.clock_out(transactional_db, job_id, a.worker.id)


def test_concurrent_clock_in_only_one_wins(db_file_based, file_company):
    with db_file_based() as db:
        job_id = on_shift_worker_job(db, file_company)
        db.commit()
    url = f"/api/jobs/{job_id}/clock-in"
    responses = td.fire([("POST", url, {"headers": auth_header(file_company.worker)})] * 4)
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]
    with db_file_based() as db:
        assert len(time_entries(db, file_company.id, job_id)) == 1


def test_concurrent_clock_out_only_one_wins(db_file_based, file_company):
    with db_file_based() as db:
        job_id = assigned_job(db, file_company.id, file_company.worker.id, JobStatus.in_progress)
        with tenant_context(db, file_company.id):
            db.add(
                TimeEntry(
                    job_id=job_id, worker_id=file_company.worker.id, started_at=utcnow() - hours(1)
                )
            )
            db.commit()
    url = f"/api/jobs/{job_id}/clock-out"
    responses = td.fire([("POST", url, {"headers": auth_header(file_company.worker)})] * 4)
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]


def test_e2e_shift_offer_accept_clock_in_and_out(client, two_companies, transactional_db):
    a, _ = two_companies
    headers = auth_header(a.worker)
    now = utcnow()
    assert post_shift(client, a.worker, now - hours(1), now + hours(8)).status_code == 201
    located = client.put(
        "/api/workers/me/location", json={"lat": td.JOB_LAT, "lng": td.JOB_LNG}, headers=headers
    )
    assert located.status_code == 200
    created = client.post(
        "/api/jobs", json=td.job_body(trade="a"), headers=auth_header(a.dispatcher)
    )
    job_id = created.json()["id"]
    assert td.start_offering(client, a.dispatcher, job_id).status_code == 200
    offer = td.offers_for(transactional_db, a.id, job_id)[a.worker.id]

    assert client.post(f"/api/offers/{offer.id}/accept", headers=headers).status_code == 200
    assert clock(client, "in", job_id, a.worker).status_code == 200
    assert clock(client, "out", job_id, a.worker).status_code == 200

    assert td.fresh(transactional_db, a.id, Job, job_id).status is JobStatus.completed
    assert time_entries(transactional_db, a.id, job_id)[0].ended_at is not None
