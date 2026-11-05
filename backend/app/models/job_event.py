from __future__ import annotations

from datetime import datetime

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import JobStatus, utcnow
from app.models.tenant import TenantScoped


def _status_column() -> Mapped[JobStatus]:
    return mapped_column(Enum(JobStatus, name="job_status", native_enum=False, length=20))


class JobEvent(TenantScoped, Base):
    """One row per job status change, written by ``app.domain.job_state.transition``."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: a job with history can't be deleted. Only drafts can, and they have none.
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), index=True)
    from_status: Mapped[JobStatus] = _status_column()
    to_status: Mapped[JobStatus] = _status_column()
    # NULL for a system move (the sweep), or once the acting user has been deleted.
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    at: Mapped[datetime] = mapped_column(default=utcnow)
