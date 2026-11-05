from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.tenant import TenantScoped


def utcnow() -> datetime:
    """Naive UTC 'now'. Every dispatch timestamp is naive UTC so SQLite and
    Postgres ``timestamp without time zone`` compare consistently."""
    return datetime.now(UTC).replace(tzinfo=None)


class JobStatus(str, enum.Enum):
    draft = "draft"
    dispatching = "dispatching"
    assigned = "assigned"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    unfilled = "unfilled"


class Job(TenantScoped, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_company_status", "company_id", "status"),
        Index("ix_jobs_company_scheduled_for", "company_id", "scheduled_for"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    trade: Mapped[str] = mapped_column(String(50))
    address: Mapped[str] = mapped_column(String(500))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    scheduled_for: Mapped[datetime | None]
    # Change only through app.domain.job_state.transition, which audits it.
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=False, length=20),
        default=JobStatus.draft,
        server_default=JobStatus.draft.value,
    )
    assigned_worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    offer_round: Mapped[int] = mapped_column(default=0, server_default="0")
    # NULL for jobs older than this column, or once the author has been deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Optimistic lock: bumped by every status change and detail edit.
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
