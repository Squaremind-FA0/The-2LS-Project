from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Enum, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class PayRunStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    paid = "paid"


class PayRun(TenantScoped, Base):
    """One pay period; ``period_start`` and ``period_end`` are both inclusive."""

    __tablename__ = "payruns"
    __table_args__ = (Index("ix_payruns_company_status", "company_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date]
    period_end: Mapped[date]
    status: Mapped[PayRunStatus] = mapped_column(
        Enum(PayRunStatus, name="payrun_status", native_enum=False, length=20),
        default=PayRunStatus.draft,
        server_default=PayRunStatus.draft.value,
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
