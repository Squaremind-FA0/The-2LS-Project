from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.models import JobStatus, OfferStatus

Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Trade = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=50)
]
Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Version = Annotated[int, Field(ge=1)]


def _to_naive_utc(value: datetime | None) -> datetime | None:
    # Stored as naive UTC, like every other dispatch timestamp.
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


class JobCreateRequest(BaseModel):
    # extra="forbid": company_id / status / assigned_worker_id can't be smuggled.
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description = ""
    trade: Trade
    address: Address
    lat: Latitude
    lng: Longitude
    scheduled_for: datetime | None = None

    _naive_scheduled_for = field_validator("scheduled_for")(_to_naive_utc)


class JobUpdateRequest(BaseModel):
    """Either a status change or a details edit, never both: they have different
    rules, and half-applying one would be worse than refusing the pair."""

    model_config = ConfigDict(extra="forbid")

    version: Version
    status: Literal["dispatching", "cancelled", "in_progress", "completed"] | None = None
    title: Title | None = None
    description: Description | None = None
    trade: Trade | None = None
    address: Address | None = None
    lat: Latitude | None = None
    lng: Longitude | None = None
    scheduled_for: datetime | None = None

    _naive_scheduled_for = field_validator("scheduled_for")(_to_naive_utc)

    def details(self) -> dict:
        return self.model_dump(include=self.model_fields_set - {"version", "status"})

    @model_validator(mode="after")
    def _one_kind_of_change(self) -> Self:
        details = self.details()
        if "status" in self.model_fields_set:
            if self.status is None:
                raise ValueError("status cannot be null")
            if details:
                raise ValueError("send a status change or a details edit, not both")
        elif not details:
            raise ValueError("nothing to update")
        # Only scheduled_for may be cleared; the rest are required on a job.
        nulled = [k for k, v in details.items() if v is None and k != "scheduled_for"]
        if nulled:
            raise ValueError(f"cannot be null: {', '.join(sorted(nulled))}")
        return self


class JobCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Version


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    trade: str
    address: str
    lat: float
    lng: float
    scheduled_for: datetime | None
    status: JobStatus
    assigned_worker_id: int | None
    offer_round: int
    created_by: int | None
    version: int
    created_at: datetime
    updated_at: datetime


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


class WorkerAvailabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_available: bool


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
    title: str
    trade: str
    address: str
    lat: float
    lng: float
    scheduled_for: datetime | None


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


class JobUpdatedEvent(BaseModel):
    """Pushed on the dispatcher-facing company:{id} channel (WS /api/ws/company)
    whenever a job's status changes: dispatched, cancelled, assigned, or
    left unfilled."""

    type: Literal["job.updated"] = "job.updated"
    job: JobOut


CompanyEvent = JobUpdatedEvent
