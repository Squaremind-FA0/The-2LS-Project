"""Read-only roster queries, independent of the dispatch matcher.

``find_nearby_workers`` (``app/services/dispatch.py``) has its own inline
on-shift ``EXISTS`` filter; ``is_on_shift`` is not wired into it yet (a
planned follow-up once M4 settles), but shares its half-open window
semantics (``starts_at <= at < ends_at``) so swapping one for the other
later is behavior-preserving.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Shift, ShiftStatus


def is_on_shift(db: Session, worker_id: int, at: datetime) -> bool:
    """Whether ``worker_id`` has a scheduled shift covering ``at``.

    Call with a tenant-scoped session (``get_tenant_db``): the company
    boundary comes from ``app.tenancy``'s session-level scoping, same as
    every other query here.
    """
    return (
        db.scalar(
            select(Shift.id)
            .where(
                Shift.worker_id == worker_id,
                Shift.status == ShiftStatus.scheduled,
                Shift.starts_at <= at,
                Shift.ends_at > at,
            )
            .limit(1)
        )
        is not None
    )
