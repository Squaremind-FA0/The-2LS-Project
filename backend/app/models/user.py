from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, String, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.company import Company
from app.models.tenant import Role, TenantScoped

if TYPE_CHECKING:
    from app.models.worker_profile import WorkerProfile


class User(TenantScoped, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored lower-cased; globally unique in v1 (no company slug at login).
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", native_enum=False, length=20)
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Embedded in access tokens as ``tv``; bumping it revokes every token.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    company: Mapped[Company] = relationship()
    worker_profile: Mapped[WorkerProfile | None] = relationship(
        back_populates="user", uselist=False
    )

    def bump_token_version(self) -> None:
        self.token_version = (self.token_version or 0) + 1
