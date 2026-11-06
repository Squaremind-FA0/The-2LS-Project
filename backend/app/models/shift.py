from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class ShiftStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class Shift(TenantScoped, Base):
    """A worker's rostered window: half-open ``[starts_at, ends_at)``, naive UTC.

    Overlap is refused by ``app.services.shifts``/``app.roster.service``, not by
    the schema; the schema only guarantees ``ends_at > starts_at``.
    """

    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_company_worker_window", "company_id", "worker_id", "starts_at", "ends_at"),
        CheckConstraint("ends_at > starts_at", name="ck_shifts_ends_after_starts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    notes: Mapped[str | None] = mapped_column(String(500), default=None)
    status: Mapped[ShiftStatus] = mapped_column(
        Enum(ShiftStatus, name="shift_status", native_enum=False, length=20),
        default=ShiftStatus.scheduled,
        server_default=ShiftStatus.scheduled.value,
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
