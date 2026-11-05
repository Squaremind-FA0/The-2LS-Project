"""G4 — realtime offer push: event contract, ws auth/audit, notifier,
commit-then-notify, polling fallback and the socket itself.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient

import app.services.offer_notifier as offer_notifier
from app.auth.deps import ws_principal
from app.config import settings
from app.db import SQLITE_BUSY_TIMEOUT_S
from app.main import app as fastapi_app
from app.models import User
from app.schemas.jobs import OfferClosedEvent, OfferCreatedEvent
from app.security.tokens import create_access_token
from app.services import dispatch
from app.services.offer_notifier import OVERFLOW, InProcessOfferNotifier
from tests.factories import auth_header, tenant_context
from tests.test_dispatch import (
    add_job,
    add_worker,
    db_file_based,  # noqa: F401
    expire_offers_now,
    file_company,  # noqa: F401
    job_body,
    offers_for,
    start_offering,
)
from tests.test_dispatch import sweep as sweep_request
from tests.test_route_audit import api_routes

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


# --- Phase 5: polling fallback ----------------------------------------------------------------


def test_get_pending_matches_the_offer_created_shape(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offer = offers_for(transactional_db, a.id, job_id)[w.id]

    response = client.get("/api/offers/pending", headers=auth_header(w))
    assert response.status_code == 200
    [row] = response.json()
    assert row["offer_id"] == row["id"] == offer.id
    assert row["job_id"] == job_id
    assert row["job"]["id"] == job_id
    assert row["distance_km"] == pytest.approx(1.0, abs=0.01)
    assert "expires_at" in row


def test_get_pending_excludes_offers_past_ttl_before_any_sweep(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    expire_offers_now(transactional_db, a.id, job_id)

    response = client.get("/api/offers/pending", headers=auth_header(w))
    assert response.status_code == 200
    assert response.json() == []


def test_get_pending_excludes_taken_declined_and_other_workers_offers(
    client, two_companies, transactional_db, monkeypatch
):
    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 2)
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offers = offers_for(transactional_db, a.id, job_id)

    # w1 accepts: its own offer becomes accepted, w2's becomes expired (taken).
    assert client.post(
        f"/api/offers/{offers[w1.id].id}/accept", headers=auth_header(w1)
    ).status_code == 200
    assert client.get("/api/offers/pending", headers=auth_header(w1)).json() == []
    assert client.get("/api/offers/pending", headers=auth_header(w2)).json() == []

    # A second, independent job that its own offer declines.
    w3 = add_worker(transactional_db, a.id, "w3", 1.0, trades=("electrical",))
    job2 = add_job(transactional_db, a.id, trade="electrical")
    assert start_offering(client, a.dispatcher, job2).status_code == 200
    offer2 = offers_for(transactional_db, a.id, job2)[w3.id]
    assert client.post(
        f"/api/offers/{offer2.id}/decline", headers=auth_header(w3)
    ).status_code == 200
    assert client.get("/api/offers/pending", headers=auth_header(w3)).json() == []


def test_get_pending_requires_the_worker_role(client, two_companies):
    a, _ = two_companies
    assert client.get("/api/offers/pending", headers=auth_header(a.dispatcher)).status_code == 403
    assert client.get("/api/offers/pending", headers=auth_header(a.admin)).status_code == 403


# --- Phase 6: the socket -----------------------------------------------------------------------


def recv(ws, timeout=2.0):
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(ws.receive_json).result(timeout=timeout)


def token_for(user):
    return create_access_token(user.id, user.token_version)


@contextmanager
def worker_socket(client, user):
    with client.websocket_connect("/api/ws/worker") as ws:
        ws.send_json({"type": "auth", "token": token_for(user)})
        assert recv(ws) == {"type": "auth.ok"}
        yield ws


def assert_quiet(ws):
    ws.send_json({"type": "ping"})
    assert recv(ws) == {"type": "pong"}


def closed_with(ws):
    """Drain any ping/event frames first: the socket may legitimately send
    something before it closes."""
    with pytest.raises(WebSocketDisconnect) as exc:
        while True:
            recv(ws)
    return exc.value.code


def test_authenticated_worker_receives_offer_created_within_2s(
    client, two_companies, transactional_db
):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    with worker_socket(client, w) as ws:
        assert start_offering(client, a.dispatcher, job_id).status_code == 200
        event = recv(ws)
    assert event["type"] == "offer.created"
    assert event["job_id"] == job_id
    assert event["distance_km"] == pytest.approx(1.0, abs=0.01)


def test_unauthenticated_ws_connection_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_S", 0.1)
    with client.websocket_connect("/api/ws/worker") as ws:
        assert closed_with(ws) == 4401  # silence past the deadline


def test_worker_of_company_b_never_receives_company_a_events(
    client, two_companies, transactional_db
):
    a, b = two_companies
    add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    with worker_socket(client, b.worker) as ws:
        assert start_offering(client, a.dispatcher, job_id).status_code == 200
        assert_quiet(ws)


def test_reconnect_then_get_pending_returns_missed_offers(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    with worker_socket(client, w):
        pass
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    with worker_socket(client, w) as ws:
        assert_quiet(ws)
        r = client.get("/api/offers/pending", headers=auth_header(w))
    assert r.status_code == 200
    [row] = r.json()
    offer = offers_for(transactional_db, a.id, job_id)[w.id]
    assert row["offer_id"] == offer.id and row["job"]["id"] == job_id
    assert row["distance_km"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize(
    "send",
    [
        lambda ws: ws.send_text("not json"),
        lambda ws: ws.send_bytes(b"\x00\x01"),
        lambda ws: ws.send_json([1, 2, 3]),
        lambda ws: ws.send_json({"type": "wrong"}),
        lambda ws: ws.send_json({"type": "auth"}),
        lambda ws: ws.send_json({"type": "auth", "token": 12345}),
        lambda ws: ws.send_json({"type": "auth", "token": "not-a-real-jwt"}),
    ],
    ids=[
        "not-json",
        "binary",
        "not-an-object",
        "wrong-type",
        "no-token",
        "non-string-token",
        "bad-jwt",
    ],
)
def test_malformed_first_frame_is_rejected(client, send):
    with client.websocket_connect("/api/ws/worker") as ws:
        send(ws)
        assert closed_with(ws) == 4401


def test_non_worker_rejected_4403(client, two_companies):
    a, _ = two_companies
    with client.websocket_connect("/api/ws/worker") as ws:
        ws.send_json({"type": "auth", "token": token_for(a.dispatcher)})
        assert closed_with(ws) == 4403


def test_stale_token_version_rejected(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    token = token_for(w)
    with tenant_context(transactional_db, a.id):
        user = transactional_db.get(User, w.id)
        user.bump_token_version()
        transactional_db.flush()
    with client.websocket_connect("/api/ws/worker") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert closed_with(ws) == 4401


def test_taken_and_expired_routes(client, two_companies, transactional_db):
    a, _ = two_companies
    w1 = add_worker(transactional_db, a.id, "w1", 1.0)
    w2 = add_worker(transactional_db, a.id, "w2", 2.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offers = offers_for(transactional_db, a.id, job_id)
    with worker_socket(client, w2) as ws:
        assert client.post(
            f"/api/offers/{offers[w1.id].id}/accept", headers=auth_header(w1)
        ).status_code == 200
        event = recv(ws)
    assert event == {"type": "offer.taken", "offer_id": offers[w2.id].id, "job_id": job_id}

    job2_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job2_id).status_code == 200
    offer2 = offers_for(transactional_db, a.id, job2_id)[w2.id]
    with worker_socket(client, w2) as ws:
        response = client.patch(
            f"/api/jobs/{job2_id}",
            json={"status": "cancelled"},
            headers=auth_header(a.dispatcher),
        )
        assert response.status_code == 200
        event2 = recv(ws)
    assert event2 == {"type": "offer.expired", "offer_id": offer2.id, "job_id": job2_id}


def test_sweep_pushes_expired(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_id = add_job(transactional_db, a.id)
    assert start_offering(client, a.dispatcher, job_id).status_code == 200
    offer = offers_for(transactional_db, a.id, job_id)[w.id]
    expire_offers_now(transactional_db, a.id, job_id)
    with worker_socket(client, w) as ws:
        assert sweep_request(client, a.admin).status_code == 200
        event = recv(ws)
    assert event == {"type": "offer.expired", "offer_id": offer.id, "job_id": job_id}


def test_heartbeat_ping_and_revalidation_close(
    client, two_companies, transactional_db, monkeypatch
):
    monkeypatch.setattr(settings, "WS_HEARTBEAT_S", 0.2)
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    with client.websocket_connect("/api/ws/worker") as ws:
        ws.send_json({"type": "auth", "token": token_for(w)})
        assert recv(ws) == {"type": "auth.ok"}
        assert recv(ws) == {"type": "ping"}
        with tenant_context(transactional_db, a.id):
            user = transactional_db.get(User, w.id)
            user.bump_token_version()
            transactional_db.flush()
        ws.send_json({"type": "pong"})
        assert closed_with(ws) == 4401


def test_heartbeat_timeout_closes_4408(client, two_companies, transactional_db, monkeypatch):
    monkeypatch.setattr(settings, "WS_HEARTBEAT_S", 0.1)
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    with client.websocket_connect("/api/ws/worker") as ws:
        ws.send_json({"type": "auth", "token": token_for(w)})
        assert recv(ws) == {"type": "auth.ok"}
        assert closed_with(ws) == 4408


def test_overflow_closes_1013(client, two_companies, transactional_db, monkeypatch):
    monkeypatch.setattr(settings, "WS_QUEUE_MAX", 2)
    monkeypatch.setattr(settings, "DISPATCH_RING_SIZE", 1)
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    job_ids = [add_job(transactional_db, a.id) for _ in range(5)]
    for job_id in job_ids:
        assert start_offering(client, a.dispatcher, job_id).status_code == 200
        expire_offers_now(transactional_db, a.id, job_id)
    with worker_socket(client, w) as ws:
        assert sweep_request(client, a.admin).status_code == 200
        assert closed_with(ws) == 1013


def test_registry_empty_after_disconnect(client, two_companies, transactional_db):
    a, _ = two_companies
    w = add_worker(transactional_db, a.id, "w1", 1.0)
    with worker_socket(client, w):
        pass
    notifier = offer_notifier.get_offer_notifier()
    assert not notifier._connections.get((a.id, w.id))


def test_every_api_websocket_route_rejects_unauthenticated(client, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_S", 0.1)
    ws_routes = [r for r in api_routes(fastapi_app) if isinstance(r.original, APIWebSocketRoute)]
    assert ws_routes
    for route in ws_routes:
        with client.websocket_connect(route.path) as ws:
            assert closed_with(ws) == 4401


def test_open_socket_does_not_hold_the_sqlite_write_lock(db_file_based, file_company):  # noqa: F811
    """Real file database, real commits: every transaction is BEGIN IMMEDIATE,
    so a socket that kept its session open would block this write for the
    full busy timeout and then fail."""
    test_client = TestClient(fastapi_app)
    with worker_socket(test_client, file_company.worker):
        started = time.monotonic()
        response = test_client.post(
            "/api/jobs", json=job_body(), headers=auth_header(file_company.dispatcher)
        )
        elapsed = time.monotonic() - started
    assert response.status_code == 201
    assert elapsed < SQLITE_BUSY_TIMEOUT_S / 2
