from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models import ShiftStatus


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


class ShiftCreate(BaseModel):
    # extra="forbid": worker_id comes from the token and can't be smuggled.
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    notes: str | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _normalize(cls, value: datetime) -> datetime:
        return _to_naive_utc(value)


class ShiftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_id: int
    starts_at: datetime
    ends_at: datetime
    notes: str | None
    status: ShiftStatus
    created_at: datetime


class RosterShiftCreate(BaseModel):
    """Admin/dispatcher creating a shift on behalf of a worker."""

    model_config = ConfigDict(extra="forbid")

    worker_id: int
    starts_at: datetime
    ends_at: datetime
    notes: str | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _normalize(cls, value: datetime) -> datetime:
        return _to_naive_utc(value)


class RosterShiftUpdate(BaseModel):
    """Admin/dispatcher editing an existing shift. Every field is optional;
    only fields actually present in the payload (``exclude_unset=True``) are
    applied, so omitting a field never clears it."""

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    notes: str | None = None
    status: ShiftStatus | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _normalize(cls, value: datetime | None) -> datetime | None:
        return value if value is None else _to_naive_utc(value)


class TimeEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    worker_id: int
    started_at: datetime
    ended_at: datetime | None
