from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class ShiftCreate(BaseModel):
    # extra="forbid": worker_id comes from the token and can't be smuggled.
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _to_naive_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value


class ShiftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_id: int
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
