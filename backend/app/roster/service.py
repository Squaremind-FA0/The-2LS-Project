"""Admin/dispatcher roster CRUD, separate from the worker self-service path
in ``app.services.shifts``. Shares the same overlap-guard shape (row lock on
the worker, half-open interval check excluding cancelled shifts)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, ShiftStatus, User
from app.services.dispatch import BadRequest, Conflict, NotFound


def _assert_window_ok(starts_at: datetime, ends_at: datetime) -> None:
    if starts_at >= ends_at:
        raise BadRequest("starts_at must be before ends_at")


def _assert_overlap_free(
    db: Session, worker_id: int, starts_at: datetime, ends_at: datetime, *, exclude_id: int | None
) -> None:
    # Same row-lock-then-check shape as app.services.shifts.create_shift.
    db.execute(select(User.id).where(User.id == worker_id).with_for_update())
    stmt = select(Shift.id).where(
        Shift.worker_id == worker_id,
        Shift.status == ShiftStatus.scheduled,
        starts_at < Shift.ends_at,
        ends_at > Shift.starts_at,
    )
    if exclude_id is not None:
        stmt = stmt.where(Shift.id != exclude_id)
    if db.scalar(stmt.limit(1)) is not None:
        raise Conflict(
            "Shift overlaps existing shift for this worker "
            "(new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at)"
        )


def create_shift(
    db: Session, worker_id: int, starts_at: datetime, ends_at: datetime, notes: str | None = None
) -> Shift:
    if db.get(User, worker_id) is None:
        raise NotFound("Worker not found")
    _assert_window_ok(starts_at, ends_at)
    _assert_overlap_free(db, worker_id, starts_at, ends_at, exclude_id=None)
    shift = Shift(worker_id=worker_id, starts_at=starts_at, ends_at=ends_at, notes=notes)
    db.add(shift)
    db.commit()
    return shift


def get_shift(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise NotFound("Shift not found")
    return shift


def update_shift(db: Session, shift_id: int, **fields: object) -> Shift:
    shift = get_shift(db, shift_id)
    starts_at = fields.get("starts_at", shift.starts_at)
    ends_at = fields.get("ends_at", shift.ends_at)
    new_status = fields.get("status", shift.status)
    if "starts_at" in fields or "ends_at" in fields:
        _assert_window_ok(starts_at, ends_at)
    if new_status is ShiftStatus.scheduled:
        _assert_overlap_free(db, shift.worker_id, starts_at, ends_at, exclude_id=shift.id)

    shift.starts_at = starts_at
    shift.ends_at = ends_at
    shift.status = new_status
    if "notes" in fields:
        shift.notes = fields["notes"]
    db.commit()
    return shift


def delete_shift(db: Session, shift_id: int) -> None:
    shift = get_shift(db, shift_id)
    db.delete(shift)
    db.commit()


def list_week(
    db: Session, from_dt: datetime, to_dt: datetime, worker_id: int | None = None
) -> Sequence[Shift]:
    """Shifts whose window overlaps the half-open range ``[from_dt, to_dt)``."""
    stmt = select(Shift).where(Shift.starts_at < to_dt, Shift.ends_at > from_dt)
    if worker_id is not None:
        stmt = stmt.where(Shift.worker_id == worker_id)
    return db.scalars(stmt.order_by(Shift.starts_at, Shift.id)).all()
