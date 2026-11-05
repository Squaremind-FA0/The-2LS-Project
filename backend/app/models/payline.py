from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class PayLine(TenantScoped, Base):
    """A worker's pay for one PayRun. Money is integer cents, never a float."""

    __tablename__ = "paylines"
    __table_args__ = (
        UniqueConstraint("payrun_id", "worker_id", name="uq_paylines_payrun_worker"),
        Index("ix_paylines_company_payrun_worker", "company_id", "payrun_id", "worker_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payrun_id: Mapped[int] = mapped_column(ForeignKey("payruns.id", ondelete="RESTRICT"))
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    minutes_worked: Mapped[int]
    hourly_rate_cents: Mapped[int] = mapped_column(BigInteger)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
