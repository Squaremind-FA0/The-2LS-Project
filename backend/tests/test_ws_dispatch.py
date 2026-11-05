"""G4 — realtime offer push: event contract, ws auth/audit, notifier,
commit-then-notify, polling fallback and the socket itself.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

import app.services.offer_notifier as offer_notifier
from app.auth.deps import ws_principal
from app.config import settings
from app.schemas.jobs import OfferClosedEvent, OfferCreatedEvent
from app.security.tokens import create_access_token
from app.services import dispatch
from app.services.offer_notifier import OVERFLOW, InProcessOfferNotifier
from tests.factories import auth_header
from tests.test_dispatch import (
    add_job,
    add_worker,
    expire_offers_now,
    offers_for,
    start_offering,
)
from tests.test_dispatch import sweep as sweep_request

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


# --- Phase 4: dispatch publishes after commit -------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def connect(self, company_id, user_id):
        raise NotImplementedError

    def disconnect(self, connection):
        raise NotImplementedError

    def publish(self, company_id, worker_id, event):
        self.calls.append((company_id, worker_id, event))


@pytest.fixture
def pushed(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(offer_notifier, "_notifier", recorder)
    return recorder.calls


def test_offering_publishes_offer_created_with_distance(
    client, two_companies, transactional_db, pushed
):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    [(company_id, worker_id, event)] = pushed
    assert company_id == a.id
    assert worker_id == w.id
    assert event.type == "offer.created"
    assert event.job_id == job_id
    assert event.distance_km == pytest.approx(1.0, abs=0.01)


def test_cancel_publishes_offer_expired(client, two_companies, transactional_db, pushed):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    pushed.clear()
    response = client.patch(
        f"/api/jobs/{job_id}", json={"status": "cancelled"}, headers=auth_header(a.dispatcher)
    )
    assert response.status_code == 200
    [(company_id, worker_id, event)] = pushed
    assert company_id == a.id
    assert worker_id == w.id
    assert event.type == "offer.expired"
    assert event.job_id == job_id


def test_accept_publishes_offer_taken_to_losers_only(
    client, two_companies, transactional_db, pushed
):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offers = offers_for(transactional_db, a.id, job_id)
    pushed.clear()
    response = client.post(f"/api/offers/{offers[w1.id].id}/accept", headers=auth_header(w1))
    assert response.status_code == 200
    [(company_id, worker_id, event)] = pushed
    assert company_id == a.id
    assert worker_id == w2.id
    assert event.type == "offer.taken"
    assert event.job_id == job_id


def test_decline_publishes_offer_created_for_the_top_up_only(
    client, two_companies, transactional_db, pushed, monkeypatch
):
    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offer_id = offers_for(transactional_db, a.id, job_id)[w1.id].id
    pushed.clear()
    response = client.post(f"/api/offers/{offer_id}/decline", headers=auth_header(w1))
    assert response.status_code == 200
    [(company_id, worker_id, event)] = pushed
    assert company_id == a.id
    assert worker_id == w2.id
    assert event.type == "offer.created"


def test_sweep_publishes_expired_then_created_for_the_next_ring(
    client, two_companies, transactional_db, pushed, monkeypatch
):
    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    expire_offers_now(transactional_db, a.id, job_id)
    pushed.clear()
    response = sweep_request(client, a.admin)
    assert response.status_code == 200
    assert [event.type for _, _, event in pushed] == ["offer.expired", "offer.created"]
    assert pushed[0][1] == w1.id
    assert pushed[1][1] == w2.id


def test_lost_accept_race_publishes_nothing(client, two_companies, transactional_db, pushed):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offers = offers_for(transactional_db, a.id, job_id)
    assert client.post(
        f"/api/offers/{offers[w1.id].id}/accept", headers=auth_header(w1)
    ).status_code == 200
    pushed.clear()
    response = client.post(f"/api/offers/{offers[w2.id].id}/accept", headers=auth_header(w2))
    assert response.status_code in (409, 410)
    assert pushed == []


def test_notifier_failure_does_not_fail_the_request(
    client, two_companies, transactional_db, monkeypatch
):
    a, _ = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)

    class _Boom:
        def publish(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(offer_notifier, "_notifier", _Boom())
    response = start_offering(client, a.dispatcher, job_id)
    assert response.status_code == 200
    assert offers_for(transactional_db, a.id, job_id)


def test_no_publish_when_commit_fails(client, two_companies, transactional_db, monkeypatch):
    a, _ = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    calls = []
    monkeypatch.setattr(dispatch, "_publish", lambda *x: calls.append(x))
    from sqlalchemy.orm import Session

    monkeypatch.setattr(Session, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        start_offering(client, a.dispatcher, job_id)
    assert calls == []
