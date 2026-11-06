"""Pure matching geometry: no database, no I/O.

``app.services.dispatch.find_nearby_workers`` is the impure caller: it builds
the SQL that fetches candidate rows (company, trade, availability, shift and
freshness filters pushed down where SQLite can index them) and hands the
results to ``rank_candidates`` here for the exact radius cut and ordering.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from app.models import User

EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius


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
