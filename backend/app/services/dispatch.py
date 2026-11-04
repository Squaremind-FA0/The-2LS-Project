"""Dispatch engine: match workers to a job, offer, accept, decline, expire.

Every function takes a tenant-scoped session (``get_tenant_db``). The
geometry helpers at the top are pure; the DB functions only add queries
around them.

Race safety comes from the database, not from read-then-check code:

* accept: a conditional ``UPDATE jobs ... WHERE status='offering' AND
  assigned_worker_id IS NULL`` lets exactly one transaction claim the job,
  and the partial unique index ``uq_job_offers_one_accepted_per_job``
  rejects a second accepted offer for a job outright;
* offers: ``uq_job_offers_job_worker`` stops a worker being offered the same
  job twice;
* every pending-offer transition is a conditional ``UPDATE ... WHERE
  status='pending'`` checked by rowcount.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Job,
    JobOffer,
    JobStatus,
    OfferStatus,
    Role,
    User,
    WorkerLocation,
    WorkerProfile,
)
from app.models.job import utcnow
from app.schemas.jobs import JobCreateRequest

EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius


# --- pure geometry ------------------------------------------------------------------------


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    max_lat: float
    # None when the box would wrap the antimeridian or cover a pole: the
    # longitude prefilter is skipped and haversine alone decides.
    min_lng: float | None
    max_lng: float | None


def bounding_box(lat: float, lng: float, radius_km: float) -> BoundingBox:
    """A lat/lng box that fully contains the circle of ``radius_km``.

    Coarse on purpose: it only drives an indexed prefilter; haversine makes
    the exact cut. A small margin absorbs floating-point error at the edge.
    """
    d_lat = math.degrees(radius_km / EARTH_RADIUS_KM) * 1.01
    min_lat, max_lat = max(-90.0, lat - d_lat), min(90.0, lat + d_lat)
    widest = max(abs(min_lat), abs(max_lat))
    if widest >= 89.9:
        return BoundingBox(min_lat, max_lat, None, None)
    d_lng = d_lat / math.cos(math.radians(widest))
    min_lng, max_lng = lng - d_lng, lng + d_lng
    if min_lng < -180.0 or max_lng > 180.0:
        return BoundingBox(min_lat, max_lat, None, None)
    return BoundingBox(min_lat, max_lat, min_lng, max_lng)


def has_trade(trades: Iterable[str], trade: str) -> bool:
    wanted = trade.strip().casefold()
    return any(t.strip().casefold() == wanted for t in trades or ())


@dataclass(frozen=True)
class Candidate:
    user: User
    distance_km: float


def rank_candidates(
    lat: float,
    lng: float,
    rows: Iterable[tuple[User, float, float]],
    max_distance_km: float,
) -> list[Candidate]:
    """Exact radius cut + nearest-first sort (ties broken by user id)."""
    ranked = []
    for user, w_lat, w_lng in rows:
        distance = haversine_km(lat, lng, w_lat, w_lng)
        if distance <= max_distance_km:
            ranked.append(Candidate(user, distance))
    ranked.sort(key=lambda c: (c.distance_km, c.user.id))
    return ranked


# --- matching -----------------------------------------------------------------------------


def find_nearby_workers(
    db: Session,
    job: Job,
    max_distance_km: float | None = None,
    max_staleness_minutes: float | None = None,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[User]:
    """Active workers of the job's company with the job's trade, a location
    fresher than ``max_staleness_minutes`` and within ``max_distance_km``,
    nearest first. Workers already offered this job (any status) are
    excluded, so re-offering always reaches new workers."""
    if max_distance_km is None:
        max_distance_km = settings.DISPATCH_RADIUS_KM
    if max_staleness_minutes is None:
        max_staleness_minutes = settings.DISPATCH_LOCATION_MAX_AGE_MIN
    cutoff = (now or utcnow()) - timedelta(minutes=max_staleness_minutes)
    box = bounding_box(job.lat, job.lng, max_distance_km)

    already_offered = select(JobOffer.worker_id).where(JobOffer.job_id == job.id)
    stmt = (
        select(User, WorkerProfile.trades, WorkerLocation.lat, WorkerLocation.lng)
        .join(WorkerProfile, WorkerProfile.user_id == User.id)
        .join(WorkerLocation, WorkerLocation.worker_id == User.id)
        # The tenant listener already scopes every entity; these make the
        # company boundary explicit in the query itself too.
        .where(User.company_id == job.company_id)
        .where(WorkerLocation.company_id == job.company_id)
        .where(User.role == Role.worker)
        .where(User.is_active.is_(True))
        .where(WorkerProfile.active.is_(True))
        .where(WorkerLocation.updated_at >= cutoff)
        .where(WorkerLocation.lat.between(box.min_lat, box.max_lat))
        .where(User.id.not_in(already_offered))
    )
    if box.min_lng is not None:
        stmt = stmt.where(WorkerLocation.lng.between(box.min_lng, box.max_lng))

    rows = (
        (user, w_lat, w_lng)
        for user, trades, w_lat, w_lng in db.execute(stmt).all()
        if has_trade(trades, job.trade)
    )
    ranked = rank_candidates(job.lat, job.lng, rows, max_distance_km)
    if limit is not None:
        ranked = ranked[:limit]
    return [c.user for c in ranked]



# --- errors -------------------------------------------------------------------------------


class DispatchError(Exception):
    detail = "Dispatch error"

    def __init__(self, detail: str | None = None):
        super().__init__(detail or self.detail)
        self.detail = detail or self.detail


class NotFound(DispatchError):
    pass


class InvalidTransition(DispatchError):
    detail = "Invalid status transition"


class Conflict(DispatchError):
    pass


class Gone(DispatchError):
    detail = "Offer expired"


# --- jobs ---------------------------------------------------------------------------------

# Dispatcher-driven transitions (PATCH). offering -> assigned happens only via
# accept_offer, offering -> unfilled only via the sweeper.
MANUAL_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.draft: frozenset({JobStatus.offering, JobStatus.cancelled}),
    JobStatus.offering: frozenset({JobStatus.cancelled}),
}


def create_job(db: Session, payload: JobCreateRequest) -> Job:
    job = Job(**payload.model_dump(), status=JobStatus.draft)
    db.add(job)
    db.commit()
    return job


def list_jobs(
    db: Session, status: JobStatus | None = None, limit: int = 50, offset: int = 0
) -> Sequence[Job]:
    stmt = select(Job).order_by(Job.id).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return db.scalars(stmt).all()


def _conditional_update(db: Session, stmt) -> int:
    """Run an ORM UPDATE (tenant-scoped by the listener) and return rowcount."""
    return db.execute(stmt.execution_options(synchronize_session=False)).rowcount


def transition_job(
    db: Session, job_id: int, target: JobStatus, *, now: datetime | None = None
) -> Job:
    now = now or utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("Job not found")
    current = job.status
    if target not in MANUAL_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"Cannot change job status from {current.value} to {target.value}")

    values: dict = {"status": target}
    if target is JobStatus.offering:
        values["offer_round"] = 1
    # Conditional on the status we validated against: a concurrent accept or
    # second PATCH that got there first leaves rowcount 0.
    claimed = _conditional_update(
        db, update(Job).where(Job.id == job_id, Job.status == current).values(**values)
    )
    if claimed != 1:
        db.rollback()
        raise Conflict("Job status changed concurrently")
    db.refresh(job)

    if target is JobStatus.offering:
        create_offers(db, job, now=now)
    elif target is JobStatus.cancelled:
        _expire_pending(db, job_id)
    db.commit()
    db.refresh(job)
    return job


# --- offers -------------------------------------------------------------------------------


def create_offers(
    db: Session, job: Job, limit: int | None = None, *, now: datetime | None = None
) -> list[JobOffer]:
    """Offer ``job`` to the next ``limit`` (default: ring size) nearest
    workers not yet offered it. No-op unless the job is offering."""
    if job.status is not JobStatus.offering:
        return []
    now = now or utcnow()
    limit = settings.DISPATCH_RING_SIZE if limit is None else limit
    if limit <= 0:
        return []
    expires_at = now + timedelta(seconds=settings.DISPATCH_OFFER_TTL_S)
    offers = [
        JobOffer(job_id=job.id, worker_id=worker.id, expires_at=expires_at)
        for worker in find_nearby_workers(db, job, limit=limit, now=now)
    ]
    db.add_all(offers)
    db.flush()
    return offers


def pending_offer_count(db: Session, job_id: int) -> int:
    return db.scalar(
        select(func.count(JobOffer.id)).where(
            JobOffer.job_id == job_id, JobOffer.status == OfferStatus.pending
        )
    )


def _expire_pending(db: Session, job_id: int, *, except_offer_id: int | None = None) -> int:
    stmt = update(JobOffer).where(
        JobOffer.job_id == job_id, JobOffer.status == OfferStatus.pending
    )
    if except_offer_id is not None:
        stmt = stmt.where(JobOffer.id != except_offer_id)
    return _conditional_update(db, stmt.values(status=OfferStatus.expired))


def _own_offer(db: Session, offer_id: int, worker_id: int) -> JobOffer:
    offer = db.get(JobOffer, offer_id)
    # Another worker's (or company's) offer is indistinguishable from a
    # missing one: no existence oracle.
    if offer is None or offer.worker_id != worker_id:
        raise NotFound("Offer not found")
    return offer


def _refuse_unavailable(offer: JobOffer, job: Job, now: datetime) -> None:
    """Explain why ``offer`` can't be acted on, or return if it still can."""
    if offer.status is OfferStatus.accepted:
        raise Conflict("Offer already accepted")
    if job.assigned_worker_id is not None:
        raise Conflict("Job already assigned")
    if offer.status is OfferStatus.declined:
        raise Conflict("Offer already declined")
    if offer.status is OfferStatus.expired or offer.expires_at <= now:
        raise Gone("Offer expired")
    if job.status is not JobStatus.offering:
        raise Conflict("Job is no longer offering")


def accept_offer(
    db: Session, offer_id: int, worker_id: int, *, now: datetime | None = None
) -> JobOffer:
    now = now or utcnow()
    offer = _own_offer(db, offer_id, worker_id)
    job = db.get(Job, offer.job_id)
    _refuse_unavailable(offer, job, now)

    # The race guard. Two workers holding distinct pending offers for this
    # job both get here; the database applies these UPDATEs one at a time and
    # re-checks the WHERE, so only the first claims the job.
    claimed = _conditional_update(
        db,
        update(Job)
        .where(
            Job.id == offer.job_id,
            Job.status == JobStatus.offering,
            Job.assigned_worker_id.is_(None),
        )
        .values(status=JobStatus.assigned, assigned_worker_id=worker_id),
    )
    if claimed != 1:
        db.rollback()
        raise Conflict("Job already assigned")
    accepted = _conditional_update(
        db,
        update(JobOffer)
        .where(
            JobOffer.id == offer_id,
            JobOffer.worker_id == worker_id,
            JobOffer.status == OfferStatus.pending,
            JobOffer.expires_at > now,
        )
        .values(status=OfferStatus.accepted),
    )
    if accepted != 1:
        # Expired or declined between our read and the claim: undo the claim.
        db.rollback()
        raise Gone("Offer expired")
    _expire_pending(db, offer.job_id, except_offer_id=offer_id)
    try:
        db.commit()
    except IntegrityError:
        # uq_job_offers_one_accepted_per_job: the database's last word.
        db.rollback()
        raise Conflict("Job already assigned") from None
    db.refresh(offer)
    return offer


def decline_offer(
    db: Session, offer_id: int, worker_id: int, *, now: datetime | None = None
) -> JobOffer:
    """Decline, then top the job's ring back up with the next nearest
    worker(s) not yet offered it."""
    now = now or utcnow()
    offer = _own_offer(db, offer_id, worker_id)
    job = db.get(Job, offer.job_id)
    if offer.status is OfferStatus.accepted:
        raise Conflict("Offer already accepted")
    if offer.status is OfferStatus.declined:
        raise Conflict("Offer already declined")
    if offer.status is OfferStatus.expired or offer.expires_at <= now:
        raise Gone("Offer expired")

    declined = _conditional_update(
        db,
        update(JobOffer)
        .where(
            JobOffer.id == offer_id,
            JobOffer.worker_id == worker_id,
            JobOffer.status == OfferStatus.pending,
            JobOffer.expires_at > now,
        )
        .values(status=OfferStatus.declined),
    )
    if declined != 1:
        db.rollback()
        raise Conflict("Offer is no longer pending")
    db.refresh(job)
    if job.status is JobStatus.offering:
        create_offers(
            db, job, settings.DISPATCH_RING_SIZE - pending_offer_count(db, job.id), now=now
        )
    db.commit()
    db.refresh(offer)
    return offer


# --- locations ----------------------------------------------------------------------------


def upsert_location(db: Session, worker_id: int, lat: float, lng: float) -> WorkerLocation:
    def _apply() -> WorkerLocation:
        location = db.scalars(
            select(WorkerLocation).where(WorkerLocation.worker_id == worker_id)
        ).one_or_none()
        if location is None:
            location = WorkerLocation(worker_id=worker_id, lat=lat, lng=lng)
            db.add(location)
        else:
            location.lat, location.lng = lat, lng
            location.updated_at = utcnow()
        db.commit()
        return location

    try:
        return _apply()
    except IntegrityError:
        # Two first-ever updates raced on uq_worker_locations_company_worker;
        # the row exists now, so the retry updates it.
        db.rollback()
        return _apply()
