from __future__ import annotations

from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Numeric, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.tenant import TenantScoped
from app.models.user import User


class WorkerProfile(TenantScoped, Base):
    __tablename__ = "worker_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    # JSON rather than a Postgres ARRAY keeps the schema portable to SQLite.
    trades: Mapped[list[str]] = mapped_column(JSON, default=list)
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(default=True, server_default=true())

    user: Mapped[User] = relationship(back_populates="worker_profile")
