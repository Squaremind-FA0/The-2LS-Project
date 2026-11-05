from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models import PayRunStatus


class PayRunCreate(BaseModel):
    # extra="forbid": company_id and status can't be smuggled.
    model_config = ConfigDict(extra="forbid")

    period_start: date
    period_end: date


class PayRunUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["approved", "paid"]


class PayRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    period_start: date
    period_end: date
    status: PayRunStatus
    created_at: datetime
    updated_at: datetime


class PayLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payrun_id: int
    worker_id: int
    minutes_worked: int
    hourly_rate_cents: int
    amount_cents: int
    created_at: datetime
