from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.job import utcnow
from app.models.tenant import TenantScoped


class WorkerLocation(TenantScoped, Base):
    """A worker's latest reported position (one row per worker, upserted)."""

    __tablename__ = "worker_locations"
    __table_args__ = (
        UniqueConstraint("company_id", "worker_id", name="uq_worker_locations_company_worker"),
        Index("ix_worker_locations_worker_id", "worker_id"),
        # SQLite has no spatial index; the matcher prefilters with a
        # lat/lng bounding box on this index, then refines with haversine.
        Index("ix_worker_locations_lat_lng", "lat", "lng"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
