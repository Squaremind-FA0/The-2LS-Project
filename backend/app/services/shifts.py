"""Roster shifts. A shift is the half-open interval ``[starts_at, ends_at)``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, User
from app.services.dispatch import BadRequest, Conflict


def create_shift(db: Session, worker_id: int, starts_at: datetime, ends_at: datetime) -> Shift:
    if starts_at >= ends_at:
        raise BadRequest("starts_at must be before ends_at")

    # Serialise this worker's roster writes so the check below can't race
    # (Postgres row lock). SQLite ignores FOR UPDATE, but there BEGIN IMMEDIATE
    # already holds the single write lock for the whole request.
    db.execute(select(User.id).where(User.id == worker_id).with_for_update())
    overlapping = db.scalar(
        select(Shift.id)
        .where(Shift.worker_id == worker_id, starts_at < Shift.ends_at, ends_at > Shift.starts_at)
        .limit(1)
    )
    if overlapping is not None:
        raise Conflict(
            "Shift overlaps existing shift for this worker "
            "(new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at)"
        )
    shift = Shift(worker_id=worker_id, starts_at=starts_at, ends_at=ends_at)
    db.add(shift)
    db.commit()
    return shift


def list_shifts(db: Session, worker_id: int, limit: int = 50, offset: int = 0) -> Sequence[Shift]:
    return db.scalars(
        select(Shift)
        .where(Shift.worker_id == worker_id)
        .order_by(Shift.starts_at, Shift.id)
        .limit(limit)
        .offset(offset)
    ).all()
