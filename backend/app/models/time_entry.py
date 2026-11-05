from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class TimeEntry(TenantScoped, Base):
    """Time worked on one job. ``ended_at`` stays NULL until clock-out."""

    __tablename__ = "time_entries"
    __table_args__ = (
        # DB-level guard: a job has at most one entry, whatever a race did.
        UniqueConstraint("job_id", name="uq_time_entries_job"),
        Index("ix_time_entries_company_job", "company_id", "job_id"),
        Index("ix_time_entries_company_worker", "company_id", "worker_id"),
        # Payroll attributes an entry to a pay period by its clock-out time.
        Index("ix_time_entries_company_ended_at", "company_id", "ended_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: pay history must not vanish with the job or worker it records.
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
