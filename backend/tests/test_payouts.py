"""G5 — payouts: pay runs, pay lines, audit rows and the CSV export."""

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update

from app.models import (
    AuditLog,
    Job,
    JobStatus,
    PayLine,
    PayRun,
    PayRunStatus,
    Role,
    TimeEntry,
    User,
    WorkerProfile,
)
from app.models.worker_profile import rate_to_cents
from app.services import dispatch
from app.services import payruns as service
from tests import test_dispatch as td
from tests.factories import WORKER_PASSWORD, auth_header, make_user, tenant_context

db_file_based = td.db_file_based
file_company = td.file_company

PERIOD = {"period_start": "2030-01-01", "period_end": "2030-01-31"}
BASE = datetime(2030, 1, 7, 9, 0)


def add_worker(db, company_id, key, rate="25.00", name=None):
    ref = make_user(
        db, company_id, f"{key}@c{company_id}.test", WORKER_PASSWORD, Role.worker,
        hourly_rate=Decimal(rate),
    )
    if name is not None:
        with tenant_context(db, company_id):
            db.get(User, ref.id).full_name = name
            db.flush()
    return ref


def add_entry(db, company_id, worker_id, *, start=BASE, minutes=60, seconds=None, open_ended=False):
    """A completed job with one TimeEntry (a job has at most one)."""
    job_id = td.add_job(db, company_id, status=JobStatus.completed)
    length = timedelta(seconds=seconds) if seconds is not None else timedelta(minutes=minutes)
    with tenant_context(db, company_id):
        db.get(Job, job_id).assigned_worker_id = worker_id
        db.add(
            TimeEntry(
                job_id=job_id, worker_id=worker_id, started_at=start,
                ended_at=None if open_ended else start + length,
            )
        )
        db.flush()
    return job_id


def create_payrun(client, admin, **overrides):
    return client.post("/api/payruns", json={**PERIOD, **overrides}, headers=auth_header(admin))


def patch_status(client, admin, payrun_id, target):
    return client.patch(
        f"/api/payruns/{payrun_id}", json={"status": target}, headers=auth_header(admin)
    )


def lines_of(db, company_id, payrun_id) -> dict[int, PayLine]:
    with tenant_context(db, company_id):
        db.expire_all()
        lines = db.scalars(select(PayLine).where(PayLine.payrun_id == payrun_id)).all()
        return {line.worker_id: line for line in lines}


def audit_rows(db, company_id, payrun_id) -> list[AuditLog]:
    with tenant_context(db, company_id):
        db.expire_all()
        return list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "payrun", AuditLog.entity_id == payrun_id)
                .order_by(AuditLog.id)
            )
        )


def export_rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


# --- creating and listing pay runs (tasks 20, 24-26) ----------------------------------------


def test_post_payruns_creates_a_draft(client, two_companies, transactional_db):
    a, _ = two_companies
    response = create_payrun(client, a.admin)
    assert response.status_code == 201
    body = response.json()
    assert body["company_id"] == a.id
    assert (body["period_start"], body["period_end"]) == ("2030-01-01", "2030-01-31")
    assert body["status"] == "draft"
    assert "created_at" in body


def test_payrun_period_must_not_be_inverted(client, two_companies):
    a, _ = two_companies
    response = create_payrun(client, a.admin, period_start="2030-02-01", period_end="2030-01-31")
    assert response.status_code == 400
    # A single-day period is fine.
    one_day = create_payrun(client, a.admin, period_start="2030-01-31", period_end="2030-01-31")
    assert one_day.status_code == 201


def test_overlapping_pay_periods_are_refused_so_hours_cannot_be_paid_twice(client, two_companies):
    a, b = two_companies
    assert create_payrun(client, a.admin).status_code == 201

    overlap = create_payrun(client, a.admin, period_start="2030-01-31", period_end="2030-02-28")
    assert overlap.status_code == 409
    adjacent = create_payrun(client, a.admin, period_start="2030-02-01", period_end="2030-02-28")
    assert adjacent.status_code == 201
    assert create_payrun(client, b.admin).status_code == 201  # per company


def test_payrun_create_rejects_smuggled_fields(client, two_companies):
    a, b = two_companies
    assert create_payrun(client, a.admin, status="paid").status_code == 422
    assert create_payrun(client, a.admin, company_id=b.id).status_code == 422


@pytest.mark.parametrize("role", ["worker", "dispatcher"])
def test_payrun_admin_routes_are_admin_only(client, two_companies, role):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    user = getattr(a, role)
    headers = auth_header(user)
    assert client.get("/api/payruns", headers=headers).status_code == 403
    assert create_payrun(client, user, period_start="2031-01-01",
                         period_end="2031-01-31").status_code == 403
    assert patch_status(client, user, payrun_id, "approved").status_code == 403
    assert client.get(f"/api/payruns/{payrun_id}/export", headers=headers).status_code == 403


def test_list_payruns_is_company_scoped_filterable_and_paged(client, two_companies):
    a, b = two_companies
    first = create_payrun(client, a.admin).json()["id"]
    second = create_payrun(
        client, a.admin, period_start="2030-02-01", period_end="2030-02-28"
    ).json()["id"]
    create_payrun(client, b.admin)
    assert patch_status(client, a.admin, first, "approved").status_code == 200
    headers = auth_header(a.admin)

    assert [p["id"] for p in client.get("/api/payruns", headers=headers).json()] == [first, second]
    drafts = client.get("/api/payruns?status=draft", headers=headers).json()
    assert [p["id"] for p in drafts] == [second]
    page = client.get("/api/payruns?limit=1&offset=1", headers=headers).json()
    assert [p["id"] for p in page] == [second]
    assert client.get("/api/payruns?status=nope", headers=headers).status_code == 422


# --- pay calculation (tasks 21, 24) ---------------------------------------------------------


def test_pay_calculation_worked_example_1210_minutes_at_25_dollars(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    alice = add_worker(transactional_db, a.id, "alice", "25.00")
    add_entry(transactional_db, a.id, alice.id, minutes=1210)

    payrun_id = create_payrun(client, a.admin).json()["id"]

    line = lines_of(transactional_db, a.id, payrun_id)[alice.id]
    assert line.minutes_worked == 1210
    assert line.hourly_rate_cents == 2500
    assert line.amount_cents == 50417  # 1210 * 2500 / 60 = 50416.67 -> $504.17


def test_rounding_happens_once_on_the_period_total_not_per_entry(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    alice = add_worker(transactional_db, a.id, "alice", "25.00")
    # 605 min = 25208.33 cents each: per-entry rounding would total 50416.
    add_entry(transactional_db, a.id, alice.id, start=BASE, minutes=605)
    add_entry(transactional_db, a.id, alice.id, start=BASE + timedelta(days=1), minutes=605)

    payrun_id = create_payrun(client, a.admin).json()["id"]

    line = lines_of(transactional_db, a.id, payrun_id)[alice.id]
    assert (line.minutes_worked, line.amount_cents) == (1210, 50417)


@pytest.mark.parametrize(
    ("minutes", "rate_cents", "expected"),
    [
        (60, 2500, 2500),
        (0, 2500, 0),
        (1, 29, 0),  # 0.48 rounds down
        (1, 30, 1),  # 0.5 rounds up
        (1, 90, 2),  # 1.5 rounds up
        (1, 150, 3),  # 2.5 rounds up, where banker's rounding would give 2
        (1, 2500, 42),  # 41.67
        (10**9, 10**10, 166666666666666667),  # beyond float's exact integer range
    ],
)
def test_amount_cents_rounds_half_up_in_exact_integers(minutes, rate_cents, expected):
    result = service.amount_cents(minutes, rate_cents)
    assert result == expected and type(result) is int


@pytest.mark.parametrize(
    ("rate", "expected"),
    [("25.00", 2500), ("19.99", 1999), ("1.15", 115), ("0.07", 7), ("4.35", 435)],
)
def test_hourly_rate_converts_to_cents_without_float_error(rate, expected):
    result = rate_to_cents(Decimal(rate))
    assert result == expected and type(result) is int


def test_rate_read_back_from_the_database_converts_exactly(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "floaty", "19.99")
    add_entry(transactional_db, a.id, worker.id, minutes=60)
    payrun_id = create_payrun(client, a.admin).json()["id"]
    line = lines_of(transactional_db, a.id, payrun_id)[worker.id]
    assert (line.hourly_rate_cents, line.amount_cents) == (1999, 1999)


def test_minutes_are_whole_minutes_of_the_summed_seconds(client, two_companies, transactional_db):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "secs")
    add_entry(transactional_db, a.id, worker.id, start=BASE, seconds=90)
    add_entry(transactional_db, a.id, worker.id, start=BASE + timedelta(hours=2), seconds=45)
    payrun_id = create_payrun(client, a.admin).json()["id"]
    # 135 seconds in total: 2 whole minutes, not 1 + 0.
    assert lines_of(transactional_db, a.id, payrun_id)[worker.id].minutes_worked == 2


def test_entries_belong_to_the_period_of_their_clock_out(client, two_companies, transactional_db):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "edges")
    # Each entry is 60 minutes; only the ones marked count toward January.
    add_entry(transactional_db, a.id, worker.id, start=datetime(2030, 1, 1, 0, 0))  # first day
    add_entry(transactional_db, a.id, worker.id, start=datetime(2030, 1, 31, 22, 59))  # last day
    add_entry(transactional_db, a.id, worker.id, start=datetime(2029, 12, 31, 23, 30))  # crosses in
    add_entry(transactional_db, a.id, worker.id, start=datetime(2029, 12, 31, 22, 0))  # December
    add_entry(transactional_db, a.id, worker.id, start=datetime(2031, 1, 1))  # later
    add_entry(transactional_db, a.id, worker.id, start=datetime(2030, 1, 31, 23, 30))  # February
    add_entry(transactional_db, a.id, worker.id, start=BASE, open_ended=True)  # still clocked in

    payrun_id = create_payrun(client, a.admin).json()["id"]

    assert lines_of(transactional_db, a.id, payrun_id)[worker.id].minutes_worked == 180


def test_paylines_cover_each_worker_of_this_company_only(client, two_companies, transactional_db):
    a, b = two_companies
    alice = add_worker(transactional_db, a.id, "alice")
    bob = add_worker(transactional_db, a.id, "bob", "30.00")
    idle = add_worker(transactional_db, a.id, "idle")
    outsider = add_worker(transactional_db, b.id, "outsider")
    add_entry(transactional_db, a.id, alice.id, minutes=60)
    add_entry(transactional_db, a.id, bob.id, minutes=60)
    add_entry(transactional_db, b.id, outsider.id, minutes=60)

    payrun_id = create_payrun(client, a.admin).json()["id"]

    lines = lines_of(transactional_db, a.id, payrun_id)
    assert set(lines) == {alice.id, bob.id}
    assert idle.id not in lines
    assert lines[bob.id].amount_cents == 3000


def test_worker_without_a_rate_fails_the_run_instead_of_going_unpaid(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "norate")
    add_entry(transactional_db, a.id, worker.id)
    with tenant_context(transactional_db, a.id):
        transactional_db.execute(delete(WorkerProfile).where(WorkerProfile.user_id == worker.id))

    response = create_payrun(client, a.admin)

    assert response.status_code == 409
    assert client.get("/api/payruns", headers=auth_header(a.admin)).json() == []


# --- status transitions, audit and immutability (tasks 22-23, 27, 29) -----------------------


def test_payrun_draft_to_approved_writes_audit_log(client, two_companies, transactional_db):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]

    response = patch_status(client, a.admin, payrun_id, "approved")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert "updated_at" in response.json()
    rows = audit_rows(transactional_db, a.id, payrun_id)
    assert [(r.action, r.user_id, r.old_values, r.new_values) for r in rows] == [
        ("status_changed", a.admin.id, {"status": "draft"}, {"status": "approved"})
    ]
    assert rows[0].company_id == a.id


def test_payrun_approved_to_paid_writes_audit_log(client, two_companies, transactional_db):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    patch_status(client, a.admin, payrun_id, "approved")

    response = patch_status(client, a.admin, payrun_id, "paid")

    assert response.status_code == 200 and response.json()["status"] == "paid"
    rows = audit_rows(transactional_db, a.id, payrun_id)
    assert [(r.old_values, r.new_values) for r in rows] == [
        ({"status": "draft"}, {"status": "approved"}),
        ({"status": "approved"}, {"status": "paid"}),
    ]


@pytest.mark.parametrize(
    ("steps", "target", "detail"),
    [
        (["approved"], "approved", "Cannot edit an approved payrun"),
        (["approved", "paid"], "approved", "Cannot edit a paid payrun"),
        (["approved", "paid"], "paid", "Cannot edit a paid payrun"),
        ([], "paid", "Cannot transition from draft to paid"),
    ],
)
def test_approved_or_paid_payrun_patch_is_409_and_writes_no_audit(
    client, two_companies, transactional_db, steps, target, detail
):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    for step in steps:
        assert patch_status(client, a.admin, payrun_id, step).status_code == 200

    response = patch_status(client, a.admin, payrun_id, target)

    assert response.status_code == 409
    assert response.json()["detail"] == detail
    assert len(audit_rows(transactional_db, a.id, payrun_id)) == len(steps)


def test_patch_cannot_change_the_period_or_return_to_draft(client, two_companies):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    url = f"/api/payruns/{payrun_id}"
    headers = auth_header(a.admin)
    assert client.patch(url, json={"status": "draft"}, headers=headers).status_code == 422
    assert client.patch(
        url, json={"status": "approved", "period_end": "2030-03-01"}, headers=headers
    ).status_code == 422


def test_patch_of_missing_or_other_company_payrun_is_404(client, two_companies, transactional_db):
    a, b = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    assert patch_status(client, b.admin, payrun_id, "approved").status_code == 404
    assert patch_status(client, a.admin, 999_999, "approved").status_code == 404
    assert audit_rows(transactional_db, a.id, payrun_id) == []


def test_approval_picks_up_entries_clocked_out_while_the_run_was_draft(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "late")
    add_entry(transactional_db, a.id, worker.id, minutes=60)
    payrun_id = create_payrun(client, a.admin).json()["id"]
    add_entry(transactional_db, a.id, worker.id, start=BASE + timedelta(days=1), minutes=30)

    assert patch_status(client, a.admin, payrun_id, "approved").status_code == 200

    line = lines_of(transactional_db, a.id, payrun_id)[worker.id]
    assert (line.minutes_worked, line.amount_cents) == (90, 3750)


def test_paylines_are_frozen_once_approved(client, two_companies, transactional_db):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "frozen")
    add_entry(transactional_db, a.id, worker.id, minutes=60)
    payrun_id = create_payrun(client, a.admin).json()["id"]
    patch_status(client, a.admin, payrun_id, "approved")
    line_id = lines_of(transactional_db, a.id, payrun_id)[worker.id].id

    add_entry(transactional_db, a.id, worker.id, start=BASE + timedelta(days=1), minutes=30)
    with tenant_context(transactional_db, a.id):
        profile = transactional_db.scalars(
            select(WorkerProfile).where(WorkerProfile.user_id == worker.id)
        ).one()
        profile.hourly_rate = Decimal("99.00")
        transactional_db.flush()
    assert patch_status(client, a.admin, payrun_id, "paid").status_code == 200

    line = lines_of(transactional_db, a.id, payrun_id)[worker.id]
    assert (line.id, line.minutes_worked, line.hourly_rate_cents) == (line_id, 60, 2500)


def test_concurrent_approval_only_one_wins_with_one_audit_row(db_file_based, file_company):
    with db_file_based() as db:
        with tenant_context(db, file_company.id):
            payrun = PayRun(period_start=date(2030, 1, 1), period_end=date(2030, 1, 31))
            db.add(payrun)
            db.commit()
            payrun_id = payrun.id
    responses = td.fire(
        [
            ("PATCH", f"/api/payruns/{payrun_id}",
             {"json": {"status": "approved"}, "headers": auth_header(file_company.admin)})
        ] * 4
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]
    with db_file_based() as db:
        assert len(audit_rows(db, file_company.id, payrun_id)) == 1


def test_transition_refuses_a_status_read_before_a_concurrent_change(
    client, two_companies, transactional_db
):
    # SQLite serialises whole requests, so a real race never reaches the
    # conditional UPDATE. A stale object in the session reproduces what a
    # concurrent writer does on Postgres, deterministically.
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    with tenant_context(transactional_db, a.id):
        stale = transactional_db.get(PayRun, payrun_id)
        assert stale.status is PayRunStatus.draft
        transactional_db.execute(
            update(PayRun)
            .where(PayRun.id == payrun_id)
            .values(status=PayRunStatus.approved)
            .execution_options(synchronize_session=False)
        )
        with pytest.raises(dispatch.Conflict, match="changed concurrently"):
            service.transition_payrun(
                transactional_db, payrun_id, PayRunStatus.approved, a.admin.id
            )
    assert audit_rows(transactional_db, a.id, payrun_id) == []


def test_concurrent_overlapping_payruns_only_one_wins(db_file_based, file_company):
    responses = td.fire(
        [("POST", "/api/payruns", {"json": PERIOD, "headers": auth_header(file_company.admin)})] * 4
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


# --- worker visibility (tasks 28, 30) -------------------------------------------------------


def test_worker_sees_only_own_paylines_and_others_get_403(client, two_companies, transactional_db):
    a, b = two_companies
    alice = add_worker(transactional_db, a.id, "alice")
    bob = add_worker(transactional_db, a.id, "bob")
    no_line = add_worker(transactional_db, a.id, "noline")
    add_entry(transactional_db, a.id, alice.id, minutes=1210)
    add_entry(transactional_db, a.id, bob.id, minutes=60)
    payrun_id = create_payrun(client, a.admin).json()["id"]
    url = f"/api/payruns/{payrun_id}/paylines"

    response = client.get(url, headers=auth_header(alice))

    assert response.status_code == 200
    lines = response.json()
    assert [line["worker_id"] for line in lines] == [alice.id]
    assert set(lines[0]) == {
        "id", "payrun_id", "worker_id", "minutes_worked", "hourly_rate_cents", "amount_cents",
        "created_at",
    }
    assert (lines[0]["payrun_id"], lines[0]["amount_cents"]) == (payrun_id, 50417)
    assert client.get(url, headers=auth_header(no_line)).status_code == 403
    assert client.get(url, headers=auth_header(b.worker)).status_code == 404
    missing = client.get("/api/payruns/999999/paylines", headers=auth_header(alice))
    assert missing.status_code == 404


@pytest.mark.parametrize("role", ["admin", "dispatcher"])
def test_paylines_route_is_worker_only_per_the_prp(client, two_companies, role):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    url = f"/api/payruns/{payrun_id}/paylines"
    assert client.get(url, headers=auth_header(getattr(a, role))).status_code == 403


# --- CSV export (tasks 31-33) ---------------------------------------------------------------


def test_csv_export_has_exact_headers_and_one_sorted_row_per_payline(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    zed = add_worker(transactional_db, a.id, "zed", "30.00", name="Zed Young")
    alice = add_worker(transactional_db, a.id, "alice", "25.00", name="Alice Johnson")
    add_entry(transactional_db, a.id, zed.id, minutes=60)
    add_entry(transactional_db, a.id, alice.id, minutes=1210)
    payrun_id = create_payrun(client, a.admin).json()["id"]

    response = client.get(
        f"/api/payruns/{payrun_id}/export?format=csv", headers=auth_header(a.admin)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines() == [
        "worker_name,worker_email,period_start,period_end,minutes_worked,hourly_rate_cents,"
        "amount_cents",
        f"Alice Johnson,{alice.email},2030-01-01,2030-01-31,1210,2500,50417",
        f"Zed Young,{zed.email},2030-01-01,2030-01-31,60,3000,3000",
    ]


@pytest.mark.parametrize("status", ["draft", "approved", "paid"])
def test_csv_export_works_in_every_status(client, two_companies, status):
    a, _ = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    for step in {"draft": [], "approved": ["approved"], "paid": ["approved", "paid"]}[status]:
        patch_status(client, a.admin, payrun_id, step)
    response = client.get(f"/api/payruns/{payrun_id}/export", headers=auth_header(a.admin))
    assert response.status_code == 200
    assert len(export_rows(response)) == 1  # header only: nobody worked


def test_csv_export_breaks_name_ties_by_id_and_quotes_commas(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    first = add_worker(transactional_db, a.id, "first", name="Smith, Sam")
    second = add_worker(transactional_db, a.id, "second", name="Smith, Sam")
    add_entry(transactional_db, a.id, second.id)
    add_entry(transactional_db, a.id, first.id)
    payrun_id = create_payrun(client, a.admin).json()["id"]

    rows = export_rows(
        client.get(f"/api/payruns/{payrun_id}/export", headers=auth_header(a.admin))
    )

    assert [(r[0], r[1]) for r in rows[1:]] == [
        ("Smith, Sam", first.email),
        ("Smith, Sam", second.email),
    ]


@pytest.mark.parametrize("name", ["=HYPERLINK(1)", "+1+1", "-1+1", "@SUM(A1)", "\tTabbed"])
def test_csv_export_neutralises_spreadsheet_formulas(
    client, two_companies, transactional_db, name
):
    a, _ = two_companies
    worker = add_worker(transactional_db, a.id, "formula", name=name)
    add_entry(transactional_db, a.id, worker.id)
    payrun_id = create_payrun(client, a.admin).json()["id"]

    rows = export_rows(
        client.get(f"/api/payruns/{payrun_id}/export", headers=auth_header(a.admin))
    )

    assert rows[1][0] == f"'{name}"


def test_csv_export_scoping_and_format(client, two_companies):
    a, b = two_companies
    payrun_id = create_payrun(client, a.admin).json()["id"]
    url = f"/api/payruns/{payrun_id}/export"
    assert client.get(url, headers=auth_header(b.admin)).status_code == 404
    assert client.get("/api/payruns/999999/export", headers=auth_header(a.admin)).status_code == 404
    assert client.get(f"{url}?format=xml", headers=auth_header(a.admin)).status_code == 422
