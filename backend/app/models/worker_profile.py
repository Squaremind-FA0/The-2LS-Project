from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import JSON, BigInteger, Float, ForeignKey, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.tenant import TenantScoped
from app.models.user import User


def rate_to_cents(hourly_rate: Decimal) -> int:
    return int((Decimal(str(hourly_rate)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class WorkerProfile(TenantScoped, Base):
    __tablename__ = "worker_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # JSON rather than a Postgres ARRAY keeps the schema portable to SQLite.
    trades: Mapped[list[str]] = mapped_column(JSON, default=list)
    hourly_rate_cents: Mapped[int] = mapped_column(BigInteger)
    # Employment flag, set by the company. is_available is the worker's own switch.
    active: Mapped[bool] = mapped_column(default=True, server_default=true())
    is_available: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Written only by dispatch.upsert_location, with worker_locations.
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lng: Mapped[float | None] = mapped_column(Float)
    last_seen_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(back_populates="worker_profile")

    @property
    def hourly_rate(self) -> Decimal:
        """The rate in dollars, the form the public API speaks."""
        return (Decimal(self.hourly_rate_cents) / 100).quantize(Decimal("0.01"))

    @hourly_rate.setter
    def hourly_rate(self, value: Decimal) -> None:
        self.hourly_rate_cents = rate_to_cents(value)
