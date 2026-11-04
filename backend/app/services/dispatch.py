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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, JobOffer, Role, User, WorkerLocation, WorkerProfile
from app.models.job import utcnow

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

