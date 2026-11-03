from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models import Role


class WorkerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trades: list[str]
    hourly_rate: Decimal
    active: bool


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: Role
    company_id: int
    company_name: str
    worker_profile: WorkerProfileOut | None
