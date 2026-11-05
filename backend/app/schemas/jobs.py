from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
)

from app.models import JobStatus, OfferStatus

Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]


class JobCreateRequest(BaseModel):
    # extra="forbid": company_id / status / assigned_worker_id can't be smuggled.
    model_config = ConfigDict(extra="forbid")

    trade: Annotated[
        str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=50)
    ]
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    lat: Latitude
    lng: Longitude
    scheduled_for: datetime

    @field_validator("scheduled_for")
    @classmethod
    def _to_naive_utc(cls, value: datetime) -> datetime:
        # Stored as naive UTC, like every other dispatch timestamp.
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value


class JobStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["offering", "cancelled", "draft", "in_progress", "completed"]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade: str
    address: str
    lat: float
    lng: float
    scheduled_for: datetime
    status: JobStatus
    assigned_worker_id: int | None
    offer_round: int
    created_at: datetime


class JobOfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    worker_id: int
    status: OfferStatus
    expires_at: datetime
    created_at: datetime


class WorkerLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: Latitude
    lng: Longitude


class WorkerLocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    worker_id: int
    lat: float
    lng: float
    updated_at: datetime


class SweepResult(BaseModel):
    expired_count: int
    rejobs_count: int
    unfilled_count: int


class OfferJobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade: str
    address: str
    lat: float
    lng: float
    scheduled_for: datetime


class PendingOfferOut(JobOfferOut):
    """An open offer: GET /api/offers/pending items and the offer.created body."""

    job: OfferJobSummary
    # None only if the worker's location row is gone.
    distance_km: float | None

    @computed_field
    @property
    def offer_id(self) -> int:
        return self.id


class OfferCreatedEvent(PendingOfferOut):
    type: Literal["offer.created"] = "offer.created"


class OfferClosedEvent(BaseModel):
    type: Literal["offer.expired", "offer.taken"]
    offer_id: int
    job_id: int


OfferEvent = OfferCreatedEvent | OfferClosedEvent
