from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import Role


class WorkerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trades: list[str]
    hourly_rate: Decimal
    hourly_rate_cents: int
    active: bool
    is_available: bool
    last_lat: float | None
    last_lng: float | None
    last_seen_at: datetime | None


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: Role
    company_id: int
    company_name: str
    worker_profile: WorkerProfileOut | None


TradeName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class WorkerCreateRequest(BaseModel):
    # extra="forbid": a smuggled company_id / role is a 422, never honoured.
    # company_id always comes from the caller's tenant context.
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    initial_password: str = Field(min_length=12, max_length=1024)
    trades: list[TradeName] = Field(default_factory=list, max_length=20)
    hourly_rate: Decimal = Field(ge=0, max_digits=10, decimal_places=2)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        local, sep, domain = value.partition("@")
        if not sep or not local or not domain or "@" in domain or " " in value:
            raise ValueError("must be an email address")
        return value


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: Literal["worker"]
    company_id: int
    is_active: bool
    worker_profile: WorkerProfileOut
