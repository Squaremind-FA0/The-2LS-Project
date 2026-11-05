from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class Shift(TenantScoped, Base):
    """A worker's rostered window: half-open ``[starts_at, ends_at)``, naive UTC.

    Overlap is refused by ``app.services.shifts``, not by the schema.
    """

    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_company_worker_window", "company_id", "worker_id", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
