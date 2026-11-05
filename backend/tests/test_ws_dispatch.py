"""G4 — realtime offer push: event contract, ws auth/audit, notifier,
commit-then-notify, polling fallback and the socket itself.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.auth.deps import ws_principal
from app.schemas.jobs import OfferClosedEvent, OfferCreatedEvent
from app.security.tokens import create_access_token
from app.services.offer_notifier import OVERFLOW, InProcessOfferNotifier

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


# --- Phase 3: notifier -----------------------------------------------------------------------


def test_publish_reaches_only_the_matching_company_and_user():
    async def run():
        notifier = InProcessOfferNotifier()
        same_company = notifier.connect(1, 10)
        other_company = notifier.connect(2, 10)
        event = OfferClosedEvent(type="offer.taken", offer_id=1, job_id=1)
        notifier.publish(1, 10, event)
        await asyncio.sleep(0)
        assert same_company.queue.get_nowait() is event
        assert other_company.queue.empty()

    asyncio.run(run())


def test_publish_from_a_worker_thread_reaches_the_queue():
    async def run():
        notifier = InProcessOfferNotifier()
        connection = notifier.connect(1, 10)
        event = OfferClosedEvent(type="offer.taken", offer_id=2, job_id=2)
        await asyncio.to_thread(notifier.publish, 1, 10, event)
        await asyncio.sleep(0)
        assert connection.queue.get_nowait() is event

    asyncio.run(run())


def test_every_connection_of_a_worker_receives_the_event():
    async def run():
        notifier = InProcessOfferNotifier()
        first = notifier.connect(1, 10)
        second = notifier.connect(1, 10)
        event = OfferClosedEvent(type="offer.expired", offer_id=3, job_id=3)
        notifier.publish(1, 10, event)
        await asyncio.sleep(0)
        assert first.queue.get_nowait() is event
        assert second.queue.get_nowait() is event

    asyncio.run(run())


def test_queue_overflow_replaces_the_backlog_with_the_overflow_signal():
    async def run():
        notifier = InProcessOfferNotifier()
        connection = notifier.connect(1, 10)
        for i in range(connection.queue.maxsize):
            connection.enqueue(i)
        connection.enqueue("one-too-many")
        assert connection.queue.qsize() == 1
        assert connection.queue.get_nowait() is OVERFLOW

    asyncio.run(run())


def test_publish_after_the_loop_closed_drops_the_connection():
    notifier = InProcessOfferNotifier()
    loop = asyncio.new_event_loop()
    try:
        connection = loop.run_until_complete(asyncio.ensure_future(_connect(notifier), loop=loop))
    finally:
        loop.close()
    event = OfferClosedEvent(type="offer.expired", offer_id=4, job_id=4)
    notifier.publish(1, 10, event)
    assert not notifier._connections.get((1, 10))
    assert connection not in notifier._connections.get((1, 10), set())


async def _connect(notifier):
    return notifier.connect(1, 10)


def test_disconnect_empties_the_registry_key():
    async def run():
        notifier = InProcessOfferNotifier()
        connection = notifier.connect(1, 10)
        assert notifier._connections[(1, 10)]
        notifier.disconnect(connection)
        assert (1, 10) not in notifier._connections

    asyncio.run(run())
