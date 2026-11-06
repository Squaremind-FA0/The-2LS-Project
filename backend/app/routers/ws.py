"""Realtime WebSocket routes.

``WS /api/ws/worker`` — offer events for one authenticated worker (G4).
``WS /api/ws/company`` — ``job.updated`` for a company's dispatchers/admins (M4).

Protocol (JSON text frames), shared by both:
  client -> {"type":"auth","token":"<jwt>"}   first frame, within WS_AUTH_TIMEOUT_S
  server -> {"type":"auth.ok"}                after the socket is registered
  server -> offer.created | offer.expired | offer.taken      (worker socket)
  server -> job.updated                                      (company socket)
  server -> {"type":"ping"}   client -> {"type":"pong"}   every WS_HEARTBEAT_S
  client -> {"type":"ping"}   server -> {"type":"pong"}
Close codes: 4401 not authenticated, 4403 wrong role, 4408 heartbeat timeout,
1013 outbound queue overflow (reconnect and re-read the matching GET fallback:
/api/offers/pending for the worker socket; the company socket has none, since
GET /api/jobs already reflects current truth on every poll).
"""

import json
import time
from collections.abc import Awaitable
from typing import Protocol

import anyio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.auth.deps import WS_CLOSE_UNAUTHENTICATED, WsPrincipal, require_ws_role, ws_principal
from app.config import settings
from app.db import get_db
from app.models import Role
from app.realtime.hub import OVERFLOW as HUB_OVERFLOW
from app.realtime.hub import get_hub
from app.services.offer_notifier import OVERFLOW, get_offer_notifier

router = APIRouter(tags=["realtime"])

WS_CLOSE_HEARTBEAT_TIMEOUT = 4408
WS_CLOSE_OVERLOADED = 1013

_ws_worker = require_ws_role(Role.worker)
_ws_company = require_ws_role(Role.dispatcher, Role.company_admin)


class _QueuedConnection(Protocol):
    """What _sender/_receiver/_heartbeat need: OfferConnection and
    HubConnection both satisfy this structurally."""

    queue: object

    def enqueue(self, item: object) -> None: ...


async def _sender(websocket: WebSocket, connection: _QueuedConnection) -> int | None:
    """The only task that sends: everything outbound goes through the queue,
    so frames stay in order and a pong proves earlier events were delivered."""
    while True:
        item = await connection.queue.get()
        if item is OVERFLOW or item is HUB_OVERFLOW:
            return WS_CLOSE_OVERLOADED
        text = json.dumps(item) if isinstance(item, dict) else item.model_dump_json()
        try:
            await websocket.send_text(text)
        except WebSocketDisconnect:
            return None


async def _receiver(
    websocket: WebSocket, connection: _QueuedConnection, seen: list[float]
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        seen[0] = time.monotonic()
        try:
            body = json.loads(message.get("text") or "null")
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("type") == "ping":
            connection.enqueue({"type": "pong"})


async def _heartbeat(
    db: Session, principal: WsPrincipal, connection: _QueuedConnection, seen: list[float]
) -> int:
    """Ping; drop silent peers; re-validate the token, so expiry, deactivation
    and token_version bumps reach a long-lived socket (app/security/tokens.py)."""
    interval = settings.WS_HEARTBEAT_S
    while True:
        await anyio.sleep(interval)
        if time.monotonic() - seen[0] > 2 * interval:
            return WS_CLOSE_HEARTBEAT_TIMEOUT
        if await run_in_threadpool(ws_principal, db, principal.token) is None:
            return WS_CLOSE_UNAUTHENTICATED
        connection.enqueue({"type": "ping"})


async def _serve(
    websocket: WebSocket, connection: _QueuedConnection, principal: WsPrincipal, db: Session
) -> int | None:
    """Run the three tasks of one socket's session; whichever finishes first
    (sender hitting overflow, receiver seeing a disconnect, or heartbeat
    timing out/failing re-validation) ends the others and is the close code."""
    close_code: int | None = None
    seen = [time.monotonic()]
    # After connect, so nothing committed from here on can be missed.
    connection.enqueue({"type": "auth.ok"})
    async with anyio.create_task_group() as group:

        async def run(step: Awaitable[int | None]) -> None:
            nonlocal close_code
            close_code = await step
            group.cancel_scope.cancel()

        group.start_soon(run, _sender(websocket, connection))
        group.start_soon(run, _receiver(websocket, connection, seen))
        group.start_soon(run, _heartbeat(db, principal, connection, seen))
    return close_code


@router.websocket("/api/ws/worker")
async def worker_socket(
    websocket: WebSocket,
    principal: WsPrincipal = Depends(_ws_worker),
    db: Session = Depends(get_db),
) -> None:
    notifier = get_offer_notifier()
    connection = notifier.connect(principal.company_id, principal.user_id)
    try:
        close_code = await _serve(websocket, connection, principal, db)
    finally:
        notifier.disconnect(connection)
    if close_code is not None:
        await websocket.close(code=close_code)


@router.websocket("/api/ws/company")
async def company_socket(
    websocket: WebSocket,
    principal: WsPrincipal = Depends(_ws_company),
    db: Session = Depends(get_db),
) -> None:
    hub = get_hub()
    connection = hub.connect(f"company:{principal.company_id}")
    try:
        close_code = await _serve(websocket, connection, principal, db)
    finally:
        hub.disconnect(connection)
    if close_code is not None:
        await websocket.close(code=close_code)
