"""G5 — roster: shifts, the on-shift dispatch filter, and clock-in/out."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Shift
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
