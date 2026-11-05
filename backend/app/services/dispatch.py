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

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from app.schemas.jobs import (
    JobCreateRequest,
    JobOfferOut,
    OfferClosedEvent,
    OfferCreatedEvent,
    OfferEvent,
    OfferJobSummary,
    PendingOfferOut,
)
from app.services.offer_notifier import get_offer_notifier

logger = logging.getLogger(__name__)

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


class BadRequest(DispatchError):
    pass


class Forbidden(DispatchError):
    pass


# --- offer views and events ---------------------------------------------------------------
#
# Commit-then-notify: events are BUILT before db.commit() (no query after the
# commit re-opens a transaction) and PUBLISHED only after it succeeds, so a
# rollback can never produce a phantom push.


@dataclass(frozen=True)
class ClosedOffer:
    offer_id: int
    worker_id: int
    job_id: int


def _offer_view(offer: JobOffer, job: Job, location: tuple[float, float] | None) -> dict:
    distance = None if location is None else haversine_km(job.lat, job.lng, *location)
    return {
        **JobOfferOut.model_validate(offer).model_dump(),
        "job": OfferJobSummary.model_validate(job),
        "distance_km": None if distance is None else round(distance, 3),
    }


def _worker_locations(db: Session, worker_ids: Iterable[int]) -> Mapping[int, tuple[float, float]]:
    rows = db.execute(
        select(WorkerLocation.worker_id, WorkerLocation.lat, WorkerLocation.lng).where(
            WorkerLocation.worker_id.in_(list(worker_ids))
        )
    ).all()
    return {worker_id: (lat, lng) for worker_id, lat, lng in rows}


def _created_events(
    db: Session, job: Job, offers: Sequence[JobOffer]
) -> list[tuple[int, OfferEvent]]:
    locations = _worker_locations(db, [o.worker_id for o in offers])
    return [
        (o.worker_id, OfferCreatedEvent(**_offer_view(o, job, locations.get(o.worker_id))))
        for o in offers
    ]


def _closed_events(kind: str, closed: Iterable[ClosedOffer]) -> list[tuple[int, OfferEvent]]:
    return [
        (c.worker_id, OfferClosedEvent(type=kind, offer_id=c.offer_id, job_id=c.job_id))
        for c in closed
    ]


def _publish(company_id: int, events: Iterable[tuple[int, OfferEvent]]) -> None:
    """Call only after db.commit() succeeded. The write is durable by now and
    GET /api/offers/pending recovers any lost push, so a notifier failure is
    logged and never fails the request."""
    notifier = get_offer_notifier()
    for worker_id, event in events:
        try:
            notifier.publish(company_id, worker_id, event)
        except Exception:
            logger.exception("offer push failed for worker %s", worker_id)


def list_pending_offers(
    db: Session, worker_id: int, *, limit: int = 200, now: datetime | None = None
) -> list[PendingOfferOut]:
    """Current truth for one worker. Offers stay ``pending`` in the database
    until a sweep runs, so ``expires_at`` must be checked here."""
    now = now or utcnow()
    rows = db.execute(
        select(JobOffer, Job)
        .join(Job, Job.id == JobOffer.job_id)
        .where(
            JobOffer.worker_id == worker_id,
            JobOffer.status == OfferStatus.pending,
            JobOffer.expires_at > now,
            Job.status == JobStatus.offering,
        )
        .order_by(JobOffer.expires_at, JobOffer.id)
        .limit(limit)
    ).all()
    location = _worker_locations(db, [worker_id]).get(worker_id)
    return [PendingOfferOut(**_offer_view(offer, job, location)) for offer, job in rows]


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

    company_id = job.company_id
    events: list[tuple[int, OfferEvent]] = []
    if target is JobStatus.offering:
        events = _created_events(db, job, create_offers(db, job, now=now))
    elif target is JobStatus.cancelled:
        events = _closed_events("offer.expired", _expire_pending(db, job_id))
    db.commit()
    _publish(company_id, events)
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


def _expire_returning(db: Session, stmt) -> list[ClosedOffer]:
    """Flip ``stmt``'s offers to expired and return exactly the rows flipped.
    RETURNING, not SELECT-then-UPDATE: a racing transition can't be misreported."""
    rows = db.execute(
        stmt.values(status=OfferStatus.expired)
        .returning(JobOffer.id, JobOffer.worker_id, JobOffer.job_id)
        .execution_options(synchronize_session=False)
    ).all()
    return [ClosedOffer(*row) for row in rows]


def _expire_pending(
    db: Session, job_id: int, *, except_offer_id: int | None = None
) -> list[ClosedOffer]:
    stmt = update(JobOffer).where(
        JobOffer.job_id == job_id, JobOffer.status == OfferStatus.pending
    )
    if except_offer_id is not None:
        stmt = stmt.where(JobOffer.id != except_offer_id)
    return _expire_returning(db, stmt)


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
    company_id = offer.company_id
    losers = _expire_pending(db, offer.job_id, except_offer_id=offer_id)
    try:
        db.commit()
    except IntegrityError:
        # uq_job_offers_one_accepted_per_job: the database's last word.
        db.rollback()
        raise Conflict("Job already assigned") from None
    _publish(company_id, _closed_events("offer.taken", losers))
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
    company_id = job.company_id
    events: list[tuple[int, OfferEvent]] = []
    if job.status is JobStatus.offering:
        offers = create_offers(
            db, job, settings.DISPATCH_RING_SIZE - pending_offer_count(db, job.id), now=now
        )
        events = _created_events(db, job, offers)
    db.commit()
    _publish(company_id, events)
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


# --- expiry + bounded sweeper ---------------------------------------------------------------


@dataclass(frozen=True)
class ExpirySweep:
    expired_count: int
    # Offering jobs left with no pending offer: candidates for re_offer_job.
    job_ids: list[int]
    closed: list[ClosedOffer]


@dataclass(frozen=True)
class SweepSummary:
    expired_count: int
    rejobs_count: int
    unfilled_count: int


def check_offer_expiry(
    db: Session, company_id: int, *, now: datetime | None = None
) -> ExpirySweep:
    """Mark the company's pending offers past ``expires_at`` expired and
    list its offering jobs that now have no pending offer."""
    now = now or utcnow()
    closed = _expire_returning(
        db,
        update(JobOffer).where(
            JobOffer.company_id == company_id,
            JobOffer.status == OfferStatus.pending,
            JobOffer.expires_at <= now,
        ),
    )
    has_pending = (
        select(JobOffer.id)
        .where(JobOffer.job_id == Job.id, JobOffer.status == OfferStatus.pending)
        .exists()
    )
    job_ids = db.scalars(
        select(Job.id)
        .where(Job.company_id == company_id, Job.status == JobStatus.offering, ~has_pending)
        .order_by(Job.id)
    ).all()
    return ExpirySweep(len(closed), list(job_ids), closed)


def _mark_unfilled(db: Session, job: Job) -> None:
    _conditional_update(
        db,
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.offering)
        .values(status=JobStatus.unfilled),
    )
    db.refresh(job)


def re_offer_job(
    db: Session,
    job: Job,
    ring_size: int | None = None,
    max_rounds: int | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Offer an offering job with no pending offers to the next ring of
    workers never offered it before, and count the round.

    Bounded: once ``offer_round`` reaches ``max_rounds``, or nobody new is in
    range, the job becomes ``unfilled`` (terminal) and False is returned.
    """
    if job.status is not JobStatus.offering or pending_offer_count(db, job.id):
        return False
    ring_size = settings.DISPATCH_RING_SIZE if ring_size is None else ring_size
    max_rounds = settings.DISPATCH_MAX_ROUNDS if max_rounds is None else max_rounds
    if job.offer_round >= max_rounds or not create_offers(db, job, ring_size, now=now):
        _mark_unfilled(db, job)
        return False
    job.offer_round += 1
    db.flush()
    return True


def sweep(db: Session, company_id: int, *, now: datetime | None = None) -> SweepSummary:
    """One bounded pass: expire, then re-offer or close each idle job once.

    Not a loop and not a daemon. Each job commits on its own, so one bad job
    is rolled back and logged without losing the rest of the pass.
    """
    now = now or utcnow()
    expiry = check_offer_expiry(db, company_id, now=now)
    db.commit()
    _publish(company_id, _closed_events("offer.expired", expiry.closed))
    rejobs = unfilled = 0
    for job_id in expiry.job_ids:
        try:
            job = db.get(Job, job_id)
            if job is None:
                continue
            events: list[tuple[int, OfferEvent]] = []
            if re_offer_job(db, job, now=now):
                rejobs += 1
                # re_offer_job only proceeds with zero pending offers, so
                # every pending offer of this job now is one it just created.
                new_offers = db.scalars(
                    select(JobOffer).where(
                        JobOffer.job_id == job_id, JobOffer.status == OfferStatus.pending
                    )
                ).all()
                events = _created_events(db, job, new_offers)
            elif job.status is JobStatus.unfilled:
                unfilled += 1
            db.commit()
            _publish(company_id, events)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("dispatch sweep: re-offer failed for job %s", job_id)
    return SweepSummary(expiry.expired_count, rejobs, unfilled)
