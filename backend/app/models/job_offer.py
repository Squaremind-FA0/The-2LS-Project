from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class OfferStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"


# Partial unique index: the database itself refuses a second accepted offer
# for the same job, whatever the application-level checks did.
_ONE_ACCEPTED = text("status = 'accepted'")


class JobOffer(TenantScoped, Base):
    __tablename__ = "job_offers"
    __table_args__ = (
        UniqueConstraint("job_id", "worker_id", name="uq_job_offers_job_worker"),
        Index("ix_job_offers_job_id", "job_id"),
        Index("ix_job_offers_worker_status", "worker_id", "status"),
        Index("ix_job_offers_status_expires_at", "status", "expires_at"),
        Index(
            "uq_job_offers_one_accepted_per_job",
            "job_id",
            unique=True,
            sqlite_where=_ONE_ACCEPTED,
            postgresql_where=_ONE_ACCEPTED,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus, name="offer_status", native_enum=False, length=20),
        default=OfferStatus.pending,
        server_default=OfferStatus.pending.value,
    )
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
