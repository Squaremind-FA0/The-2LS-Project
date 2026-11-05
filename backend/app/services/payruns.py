"""Payroll: aggregate TimeEntries into PayLines for a pay period.

Frozen pay rules:

* An entry belongs to the pay period containing its clock-out (``ended_at``,
  naive UTC). An entry still open is paid in whichever later period it ends.
* ``minutes_worked`` is whole minutes of the worker's summed time in the
  period: partial minutes are dropped once, on the total.
* ``amount_cents = minutes_worked * hourly_rate_cents / 60``, rounded half up
  once, on the period total. Exact integer arithmetic: no floats anywhere.
* Pay periods of one company can't overlap, so no entry is paid twice.
* PayLines are computed when the run is created and again on approval, then
  never change. The worker's rate is the one current at that moment.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Company,
    PayLine,
    PayRun,
    PayRunStatus,
    TimeEntry,
    User,
    WorkerProfile,
)
from app.services.dispatch import BadRequest, Conflict, Forbidden, NotFound, conditional_update

CSV_HEADER = (
    "worker_name",
    "worker_email",
    "period_start",
    "period_end",
    "minutes_worked",
    "hourly_rate_cents",
    "amount_cents",
)

_NEXT_STATUS = {
    PayRunStatus.draft: PayRunStatus.approved,
    PayRunStatus.approved: PayRunStatus.paid,
}
_EDIT_REFUSAL = {
    PayRunStatus.approved: "Cannot edit an approved payrun",
    PayRunStatus.paid: "Cannot edit a paid payrun",
}
# A leading one of these makes a spreadsheet evaluate the cell as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# --- pure money rules -----------------------------------------------------------------------


def rate_to_cents(hourly_rate: Decimal) -> int:
    return int((hourly_rate * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def amount_cents(minutes_worked: int, hourly_rate_cents: int) -> int:
    """``minutes * rate / 60`` rounded half up: floor(x + 1/2) over integers."""
    return (minutes_worked * hourly_rate_cents + 30) // 60


# --- pay runs -------------------------------------------------------------------------------


def _worked_by_worker(db: Session, period_start: date, period_end: date) -> dict[int, timedelta]:
    entries = db.scalars(
        select(TimeEntry).where(
            TimeEntry.ended_at >= datetime.combine(period_start, time.min),
            TimeEntry.ended_at < datetime.combine(period_end + timedelta(days=1), time.min),
        )
    )
    worked: dict[int, timedelta] = defaultdict(timedelta)
    for entry in entries:
        worked[entry.worker_id] += entry.ended_at - entry.started_at
    return worked


def compute_paylines(db: Session, payrun: PayRun) -> None:
    """Replace the run's lines with what its period's TimeEntries add up to."""
    db.execute(delete(PayLine).where(PayLine.payrun_id == payrun.id))
    worked = _worked_by_worker(db, payrun.period_start, payrun.period_end)
    rates = dict(
        db.execute(
            select(WorkerProfile.user_id, WorkerProfile.hourly_rate).where(
                WorkerProfile.user_id.in_(list(worked))
            )
        ).all()
    )
    for worker_id, duration in sorted(worked.items()):
        if worker_id not in rates:
            # Refuse the run rather than silently leaving someone unpaid.
            db.rollback()
            raise Conflict(f"Worker {worker_id} has no hourly rate")
        minutes = duration // timedelta(minutes=1)
        rate_cents = rate_to_cents(rates[worker_id])
        db.add(
            PayLine(
                payrun_id=payrun.id,
                worker_id=worker_id,
                minutes_worked=minutes,
                hourly_rate_cents=rate_cents,
                amount_cents=amount_cents(minutes, rate_cents),
            )
        )
    db.flush()


def create_payrun(db: Session, company_id: int, period_start: date, period_end: date) -> PayRun:
    if period_start > period_end:
        raise BadRequest("period_start must not be after period_end")
    # Serialise the company's run creation so the overlap check can't race
    # (Postgres row lock; SQLite already holds its write lock, see app.db).
    db.execute(select(Company.id).where(Company.id == company_id).with_for_update())
    overlapping = db.scalar(
        select(PayRun.id)
        .where(PayRun.period_start <= period_end, PayRun.period_end >= period_start)
        .limit(1)
    )
    if overlapping is not None:
        raise Conflict("Pay period overlaps an existing payrun")
    payrun = PayRun(period_start=period_start, period_end=period_end)
    db.add(payrun)
    db.flush()
    compute_paylines(db, payrun)
    db.commit()
    return payrun


def list_payruns(
    db: Session, status: PayRunStatus | None = None, limit: int = 50, offset: int = 0
) -> Sequence[PayRun]:
    stmt = select(PayRun).order_by(PayRun.id).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(PayRun.status == status)
    return db.scalars(stmt).all()


def _get_payrun(db: Session, payrun_id: int) -> PayRun:
    payrun = db.get(PayRun, payrun_id)
    if payrun is None:
        raise NotFound("Payrun not found")
    return payrun


def transition_payrun(db: Session, payrun_id: int, target: PayRunStatus, user_id: int) -> PayRun:
    """Move draft -> approved -> paid, one step at a time, with an audit row.

    Moving forward is the only change an approved run accepts; everything
    else about it, its PayLines included, is frozen.
    """
    payrun = _get_payrun(db, payrun_id)
    current = payrun.status
    if _NEXT_STATUS.get(current) is not target:
        raise Conflict(
            _EDIT_REFUSAL.get(current, f"Cannot transition from {current.value} to {target.value}")
        )
    claimed = conditional_update(
        db,
        update(PayRun)
        .where(PayRun.id == payrun_id, PayRun.status == current)
        .values(status=target),
    )
    if claimed != 1:
        db.rollback()
        raise Conflict("Payrun status changed concurrently")
    if target is PayRunStatus.approved:
        # Last chance for entries clocked out while the run was a draft.
        compute_paylines(db, payrun)
    db.add(
        AuditLog(
            entity_type="payrun",
            entity_id=payrun_id,
            action="status_changed",
            user_id=user_id,
            old_values={"status": current.value},
            new_values={"status": target.value},
        )
    )
    db.commit()
    db.refresh(payrun)
    return payrun


# --- reading pay lines ----------------------------------------------------------------------


def list_own_paylines(db: Session, payrun_id: int, worker_id: int) -> Sequence[PayLine]:
    _get_payrun(db, payrun_id)
    lines = db.scalars(
        select(PayLine).where(PayLine.payrun_id == payrun_id, PayLine.worker_id == worker_id)
    ).all()
    if not lines:
        raise Forbidden("No pay lines for this worker in this payrun")
    return lines


def _csv_safe(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def export_rows(db: Session, payrun_id: int) -> list[tuple]:
    """CSV rows for the run, by worker name, then id, for a stable order."""
    payrun = _get_payrun(db, payrun_id)
    rows = db.execute(
        select(PayLine, User.full_name, User.email)
        .join(User, User.id == PayLine.worker_id)
        .where(PayLine.payrun_id == payrun_id)
        .order_by(User.full_name, User.id)
    ).all()
    return [
        (
            _csv_safe(name),
            _csv_safe(email),
            payrun.period_start.isoformat(),
            payrun.period_end.isoformat(),
            line.minutes_worked,
            line.hourly_rate_cents,
            line.amount_cents,
        )
        for line, name, email in rows
    ]


def stream_csv(rows: Iterable[tuple]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in (CSV_HEADER, *rows):
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
