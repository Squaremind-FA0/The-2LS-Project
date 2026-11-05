"""G4 — realtime offer push: event contract, ws auth/audit, notifier,
commit-then-notify, polling fallback and the socket itself.
"""

from unittest.mock import MagicMock

import pytest

from app.auth.deps import ws_principal
from app.schemas.jobs import OfferClosedEvent, OfferCreatedEvent
from app.security.tokens import create_access_token

# --- Phase 1: contract ---------------------------------------------------------------------


def test_offer_created_event_json_contract():
    body = OfferCreatedEvent(
        id=7,
        job_id=3,
        worker_id=12,
        status="pending",
        expires_at="2026-11-05T10:00:30",
        created_at="2026-11-05T10:00:00",
        job={
            "id": 3,
            "trade": "plumbing",
            "address": "1 George St",
            "lat": -33.86,
            "lng": 151.2,
            "scheduled_for": "2026-12-01T09:00:00",
        },
        distance_km=1.001,
    ).model_dump()
    assert body["type"] == "offer.created"
    assert body["offer_id"] == body["id"] == 7
    assert body["job"]["id"] == 3
    assert body["distance_km"] == 1.001
    assert body["expires_at"] is not None

    for kind in ("offer.expired", "offer.taken"):
        OfferClosedEvent(type=kind, offer_id=7, job_id=3)
    with pytest.raises(ValueError):
        OfferClosedEvent(type="offer.created", offer_id=7, job_id=3)


# --- Phase 2: auth and route audit ----------------------------------------------------------


def test_ws_principal_always_closes_the_session():
    db = MagicMock()
    assert ws_principal(db, "not-a-real-token") is None
    db.close.assert_called_once()

    db2 = MagicMock()
    db2.scalars.side_effect = RuntimeError("boom")
    valid_token = create_access_token(999, 0)
    with pytest.raises(RuntimeError):
        ws_principal(db2, valid_token)
    db2.close.assert_called_once()
