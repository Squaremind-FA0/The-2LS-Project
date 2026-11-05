# PRP: G4 — Realtime offer push

## Goal

A worker holding an open `WS /api/ws/worker` connection, authenticated by JWT in its first frame, is pushed `offer.created`, `offer.expired` and `offer.taken` as soon as the dispatch transaction that caused them commits. A new `GET /api/offers/pending` returns the worker's current open offers, so a worker who was offline or reconnecting never loses an offer.

## Non-goals

- **Native mobile push (FCM/APNs)** — follow-up.
- **Multi-instance Redis adapter** — documented below (see "Multi-instance follow-up"), **not built**. Do not add a Redis client or a dependency.
- **A background expiry daemon.** Expiry stays the explicit `POST /api/dispatch/sweep` from G3 (`backend/app/routers/dispatch.py:60-65`). `offer.expired` is therefore pushed when a sweep or a cancel commits, not at the instant `expires_at` passes. Clients run their own countdown from `expires_at`.
- **Frontend changes.** No client is built here, and `npm run build` is not a gate for this PRP. `frontend/vite.config.js:6` proxies `/api` without `ws: true`, so a browser in Vite dev mode cannot reach the socket until the client-work follow-up adds it.
- **A `taken`/`superseded` offer status or any schema change** (see "Known limitation: taken and expired are the same database state").
- **Events for a worker's own action.** Accept and decline answer over HTTP. A second device of the same worker learns of them on its next `GET /api/offers/pending`.
- **Connection-count limits or rate limiting** on the socket (see Risks).

## Context

### Existing code this builds on

- **G3 deferred this work:** `PRPs/G3-jobs-dispatch.md:9` lists realtime push as a non-goal for "G4+".
- **Services own their commits.** `transition_job` (`backend/app/services/dispatch.py:257`), `accept_offer` (`:367`), `decline_offer` (`:410`) and `sweep` (`:531`, and per job at `:542`) each call `db.commit()` themselves; the routers only translate errors (`backend/app/routers/dispatch.py:38-57`, `backend/app/routers/jobs.py:35-43`). So commit-then-notify has to happen inside the service.
- **`create_offers` runs before the commit and must never publish.** It only flushes (`dispatch.py:281-282`). Its callers are `transition_job` (`:254`), `decline_offer` (`:407`) and `re_offer_job` (`:515`).
- **Creating a job makes no offers.** `POST /api/jobs` creates a `draft` (`backend/app/routers/jobs.py:20-21`, `dispatch.py:208-212`). The first offers appear on `PATCH /api/jobs/{id}` → `transition_job` (`jobs.py:35-43`, `dispatch.py:253-254`). This refines the mission brief's "job creation in `jobs.py`" call site: `POST /api/jobs` needs no publish.
- **Expiry paths return only counts today.** `_expire_pending` returns a rowcount (`dispatch.py:294-300`) and `check_offer_expiry` likewise (`:464-473`, `:484`), so neither says which workers to notify. `_expire_pending`'s return value is unused at both call sites (`:256`, `:365`).
- **Distance is not stored.** `JobOffer` has no distance column (`backend/app/models/job_offer.py:42-52`) and `find_nearby_workers` discards the distance when it returns users (`dispatch.py:167`). `haversine_km` is at `dispatch.py:53-59`.
- **Offers stay `pending` past their TTL until a sweep runs.** `check_offer_expiry` is the only code that flips them on time (`dispatch.py:464-473`), and there is no background loop (`routers/dispatch.py:64-65`). `GET /api/offers/pending` must filter on `expires_at` itself.
- **Token validation** lives in `get_current_user` (`backend/app/auth/deps.py:44-58`), using `decode_access_token` (`backend/app/security/tokens.py:30-38`) and the tenant-bypass lookup `get_user_for_token` (`backend/app/auth/service.py:55-62`). It cannot be used as-is on a socket: `HTTPBearer` needs a header (`deps.py:25`, `:36-39`), and `get_tenant_db` depends on it (`deps.py:80-82`).
- **Role and deactivation changes take effect immediately** on HTTP because they are read from the database on every request (`backend/app/security/tokens.py:3-6`). A long-lived socket has to re-validate to keep that promise.
- **Single process in production:** the image runs one `uvicorn` with no `--workers` (`Dockerfile:24`), so an in-process registry reaches every socket today.
- **No new dependency.** `uvicorn[standard]>=0.29` is already pinned (`backend/requirements.txt:2`) and brings WebSocket support. `anyio` comes with FastAPI/Starlette. Do not change `requirements.txt`.
- **The settings pattern** to extend is `backend/app/config.py:13-19`; tests override values with `monkeypatch.setattr(settings, ...)` (`backend/tests/test_dispatch.py:394`).
- **Patterns to mirror:** the `require_role` factory and its marker (`deps.py:61-73`), the worker route shape (`routers/dispatch.py:13`, `:38-46`), the `limit` query parameter (`routers/jobs.py:27`), and the router block in `main.py` (`backend/app/main.py:22-30`).
- **The SPA catch-all is HTTP-only** (`@app.get`, `backend/app/main.py:45-48`), so it does not shadow a WebSocket route.
- **Test helpers to reuse:** `add_worker`, `add_job`, `offers_for`, `start_offering`, `expire_offers_now`, `job_body` and the file-backed `db_file_based`/`file_company` fixtures (`backend/tests/test_dispatch.py:45`, `:64`, `:327`, `:334`, `:656`, `:309`, `:85-118`), plus `client` and `two_companies` (`backend/tests/conftest.py:29-60`) and `auth_header` (`backend/tests/factories.py:107`).

### Resolved: the route audit and the WebSocket route

`audit_routes` flags anything under `/api` that is not an `APIRoute`, and does so before it looks at the allowlist (`backend/tests/test_route_audit.py:84-90`). Registering the socket today fails `test_every_api_route_is_guarded_and_tenant_scoped` (`:99-100`) with exactly:

```
/api/ws/worker: non-APIRoute under /api (APIWebSocketRoute)
```

- The class is **`fastapi.routing.APIWebSocketRoute`** (a subclass of Starlette's `WebSocketRoute`), not `WebSocketRoute` itself. Observed on FastAPI 0.162.0.
- **`effective_routes` needs no change.** `iter_route_contexts` already yields the route with `ctx.methods is None` and a real `dependant`, both for `@app.websocket` and through `include_router` (`test_route_audit.py:45-55`). Only `audit_routes` changes.
- **Do not add `/api/ws/worker` to `PUBLIC_API_PATHS`** (`backend/app/auth/deps.py:21`). That list means "no auth required", which is false here; it would not help because the type check runs first; and `test_public_allowlist_is_exactly_health_and_login` (`test_route_audit.py:103-106`) would fail.

**Mechanism:** auth becomes a dependency factory, `require_ws_role(*roles)`, carrying its own marker `REQUIRE_WS_ROLE_MARKER`, exactly as `require_role` does for HTTP. `audit_routes` gets a dedicated branch, before the `APIRoute` check and the allowlist, requiring that marker in the route's dependency graph (via the existing `_walk`, `test_route_audit.py:67-70`). A `Mount` under `/api` stays a violation (`:207-210`). Because the marker sits on the function that performs the auth, it cannot be a label detached from the check. A behavioural test then connects to every WebSocket route the audit can see and asserts close code 4401.

The HTTP rule against bare `get_db` (`test_route_audit.py:94-95`) does not apply to the WebSocket branch: the handler depends on `get_db` for one reason, the pre-auth user lookup that `get_user_for_token` already exempts from tenancy, and it never reads tenant data. `backend/app/routers/ws.py` must not query a tenant table.

### Resolved: the socket must not hold a database session

`_emit_begin` issues `BEGIN IMMEDIATE` for **every** transaction, reads included (`backend/app/db.py:46-48`), and a request's session lives until the dependency unwinds (`db.py:63-68`). A socket that performs a read and leaves its session open therefore holds SQLite's single write lock for the life of the connection, and every write in the process stalls until the 10s busy timeout (`db.py:11`, `:21`) and then fails.

**Mechanism:** `ws_principal(db, token)` always calls `db.close()` in a `finally`, and is the only database access the socket makes, at auth and on each heartbeat, always in the threadpool.

Use `Depends(get_db)`, **not** a direct `SessionLocal()`. A direct session under the `client` fixture, which shares one connection in savepoint mode (`backend/tests/conftest.py:37-46`), fails with `sqlite3.OperationalError: cannot start a transaction within a transaction`. `get_db` also keeps `app.dependency_overrides` working.

### Resolved: sync-to-async bridge

Dispatch runs on a threadpool thread with a sync `Session`, and the socket runs on an event loop. `asyncio.Queue` is not thread-safe, so:

1. Each connection owns an `asyncio.Queue`.
2. `InProcessOfferNotifier.connect()` runs on the socket's loop and captures `asyncio.get_running_loop()`.
3. `publish()`, called from sync code, does `connection.loop.call_soon_threadsafe(connection.enqueue, event)`.

`enqueue` is a thin wrapper over `queue.put_nowait`, which differs from the brief's literal `call_soon_threadsafe(queue.put_nowait, event)`. A bare `put_nowait` in that callback sends `QueueFull` to the loop's exception handler: the event is dropped, the socket stays open, and the client never learns it missed anything. `enqueue` instead replaces the backlog with an `OVERFLOW` signal, the sender closes with **1013**, and the client reconnects and re-reads `GET /api/offers/pending`, which keeps "missed pushes are never lost" true. Overflow needs 256 undelivered events for one socket, which is not normal traffic.

A registry `threading.Lock` guards the dict, because `publish` reads it from threadpool threads. A worker may have several sockets (several devices), so each key maps to a *set* of connections.

### Resolved: commit-then-notify

Inside each service function: **build events before `db.commit()`, publish after it succeeds.** Building first means no query runs after the commit (a query would open a new `BEGIN IMMEDIATE` transaction). Publishing after means a rollback can never produce a phantom push, and a client that reacts to a push by calling `GET /api/offers/pending` can never get ahead of an uncommitted row. `_publish` catches and logs notifier exceptions: the write is already durable and polling recovers a lost push, so a notifier failure must not turn a committed request into a 500.

| Call site | Trigger | Built before commit | Published after |
|---|---|---|---|
| `transition_job` (`dispatch.py:253-257`) | `PATCH /api/jobs/{id}` → `offering` | `create_offers` result | `offer.created` to each offered worker |
| `transition_job` (`dispatch.py:255-257`) | `PATCH /api/jobs/{id}` → `cancelled` | `_expire_pending` rows | `offer.expired` to each affected worker |
| `accept_offer` (`dispatch.py:365-371`) | `POST /api/offers/{id}/accept` | `_expire_pending` rows | `offer.taken` to each losing worker, after the `IntegrityError` branch |
| `decline_offer` (`dispatch.py:406-410`) | `POST /api/offers/{id}/decline` | top-up `create_offers` result | `offer.created` to newly offered workers |
| `sweep` (`dispatch.py:530-531`) | `POST /api/dispatch/sweep` | `check_offer_expiry` rows | `offer.expired` to each worker |
| `sweep` (`dispatch.py:538-542`) | same, per job | offers `re_offer_job` created | `offer.created`, per job, so a job's rollback (`:543-545`) publishes nothing for it |

Failure paths (`dispatch.py:248-250`, `:347-349`, `:361-364`, `:368-371`, `:402-404`) raise before publishing. `POST /api/jobs` publishes nothing because it creates only a draft. `re_offer_job` keeps its `bool` return (`:497-520`): it only proceeds when the job has zero pending offers (`:511`), so in `sweep` every pending offer of that job right after it returns `True` is one it just created.

Expired and taken rows come from `UPDATE ... RETURNING` (`.returning(JobOffer.id, JobOffer.worker_id, JobOffer.job_id)`), not a prior `SELECT`: the rows reported are exactly the rows flipped, even if another transaction moved one first. Checked: `RETURNING` stays tenant-filtered under the `do_orm_execute` listener (`backend/app/tenancy.py:83-106`). It needs SQLite ≥ 3.35; 3.53.1 was used for the check.

### Known limitation: taken and expired are the same database state

`OfferStatus` has only `pending/accepted/declined/expired` (`backend/app/models/job_offer.py:14-18`). `accept_offer` flips every other pending offer of the job to `expired` through `_expire_pending` (`dispatch.py:365`, `:294-300`), the same state a timed-out offer reaches in a sweep (`:464-473`). So "someone else took it" and "it timed out" **cannot be told apart in the database**; they differ only in which code path pushed the event. A cancelled job's offers are also `expired`, and are pushed as `offer.expired` because the event schema has no cancel event.

**This is accepted, not a gap, and needs no migration.** A worker who was offline when an offer closed gets no retroactive `offer.taken`: the offer just stops appearing in `GET /api/offers/pending`, which only has to report current truth. If that worker still tries to accept, `_refuse_unavailable` answers `409 Job already assigned` or `410 Offer expired` (`dispatch.py:316-321`).

### Evidence from a planning prototype

The whole design below was built in a throwaway copy of `backend/` outside the repo (nothing from it is committed; the code blocks in this PRP are taken from it). Versions: FastAPI 0.162.0, Starlette 1.10.0, uvicorn 0.54.0, websockets 17.4, SQLAlchemy 2.1.8, SQLite 3.53.1, resolved from today's `backend/requirements.txt` floors.

- **pytest:** 239 passed (the baseline's 213 plus 26 new), and five consecutive full runs of the suite were stable before the last test was added. `ruff check .` clean.
- **Mutation checks, each caught by the intended test:** removing `db.close()` from `ws_principal` fails `test_open_socket_does_not_hold_the_sqlite_write_lock`. Publishing before commit fails `test_no_publish_when_commit_fails`. Ignoring `company_id` in the registry fails `test_worker_of_company_b_never_receives_company_a_events`. Removing the audit branch fails `test_every_api_route_is_guarded_and_tenant_scoped`.
- **One run against live uvicorn with a file SQLite DB and a real `websockets` client:** close codes 4401 (no frame, bad token, binary frame) and 4403 (dispatcher) arrived; `PATCH` with a socket open took 10ms; `offer.created` arrived 10ms after the `PATCH` began; server shutdown closed the socket with 1012; the server log had no tracebacks. This is one local run, not a substitute for the `dynamic_tester` follow-up.
- **Not prototyped as written:** `test_offer_created_event_json_contract` (Task 1), `test_ws_principal_always_closes_the_session` (Task 3), all Task 5 tests, the Task 7 tests other than `test_no_publish_when_commit_fails`, and all Task 9 tests. Most of their subjects were exercised through the socket tests, but these exact tests are new and have never run. Some of their cases were not exercised at all, for example a closed event loop (Task 5), `test_lost_accept_race_publishes_nothing` and `test_notifier_failure_does_not_fail_the_request` (Task 7), and the declined and other-worker filters (Task 9).

Lessons those runs produced, all reflected below:

- **Use an anyio task group for the session, not `asyncio.create_task` + `gather`.** The hand-rolled version made `TestClient` session teardown raise `CancelledError`.
- **Accept before auth.** Closing before accept is an HTTP-level rejection on a real server and carries no 4401 code.
- **Read the first frame with `receive()`, not `receive_text()`.** A binary frame makes `receive_text()` raise `KeyError('text')`.
- **Register a `WebSocketDisconnect` handler.** Without it, a client leaving during auth logs an ASGI traceback on uvicorn, because closing a gone socket raises. `TestClient` does not show this.
- **`TestClient` has no receive timeout.** `receive_json()` blocks forever; see `recv` and `assert_quiet` in Task 11.
- **Heartbeat tests must answer pings.** A silent test client is closed 4408 before a token re-check can close it 4401.

## Data model / API contracts

No database schema change and no migration. New settings, schemas, one HTTP route and one WebSocket route.

### Settings (`backend/app/config.py`, after line 19)

```python
    # Realtime offer push (G4): first-message auth deadline, server heartbeat
    # period (a peer silent for two periods is dropped) and per-connection
    # outbound queue bound.
    WS_AUTH_TIMEOUT_S: float = 5.0
    WS_HEARTBEAT_S: float = 30.0
    WS_QUEUE_MAX: int = 256
```

### Schemas (`backend/app/schemas/jobs.py`)

Extends `JobOfferOut` (`backend/app/schemas/jobs.py:54-62`). `id` and `offer_id` are both present and equal: `id` keeps the shape `accept`/`decline` return, and `offer_id` is the name the event contract specifies. `pydantic.computed_field` joins the existing pydantic import. Timestamps are naive UTC like every other dispatch timestamp (`backend/app/models/job.py:13-16`), so clients must read them as UTC.

```python
class OfferJobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade: str
    address: str
    lat: float
    lng: float
    scheduled_for: datetime


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
```

### HTTP

```
GET /api/offers/pending?limit=200          (1 <= limit <= 200)
  dependencies: require_role(Role.worker), get_tenant_db
  200: list[PendingOfferOut], soonest-expiring first
  401: no/invalid token    403: not a worker
  Filter: worker_id == caller, status == pending, expires_at > now, job.status == offering
```

A row looks like this, and `offer.created` is the same body plus `type`, so a client handles both with one code path:

```json
{
  "id": 7, "offer_id": 7, "job_id": 3, "worker_id": 12,
  "status": "pending",
  "expires_at": "2026-11-05T10:00:30", "created_at": "2026-11-05T10:00:00",
  "job": {"id": 3, "trade": "plumbing", "address": "1 George St", "lat": -33.86, "lng": 151.2,
          "scheduled_for": "2026-12-01T09:00:00"},
  "distance_km": 1.001
}
```

`distance_km` is computed from the worker's current `WorkerLocation` by one helper used for both the push and the poll, instead of threading `Candidate.distance_km` through `find_nearby_workers`/`create_offers`. That keeps both payloads identical and leaves the signatures existing tests use unchanged. On a poll it reflects where the worker is now, which is the more useful number. It is `null` only if the location row is gone.

### WebSocket protocol: `WS /api/ws/worker`

JSON text frames.

| Direction | Frame | When |
|---|---|---|
| client → server | `{"type":"auth","token":"<jwt>"}` | First frame, within `WS_AUTH_TIMEOUT_S`. Never a query parameter: URLs leak into access logs and proxies. |
| server → client | `{"type":"auth.ok"}` | After the socket is registered |
| server → client | `offer.created` body above with `"type":"offer.created"` | Offer committed |
| server → client | `{"type":"offer.expired","offer_id":7,"job_id":3}` | Swept or job cancelled |
| server → client | `{"type":"offer.taken","offer_id":7,"job_id":3}` | Another worker accepted the job |
| server → client | `{"type":"ping"}` | Every `WS_HEARTBEAT_S`; reply with any frame, conventionally `{"type":"pong"}` |
| client → server | `{"type":"ping"}` | Optional; answered `{"type":"pong"}` through the same queue as events |

Application-level pings are needed because browser JavaScript cannot send protocol pings, and they let `TestClient` exercise the heartbeat. Unknown client frames after auth are ignored.

| Close code | Meaning | Client action |
|---|---|---|
| 4401 | Missing, malformed, invalid or expired token; deactivated user; bumped `token_version`; auth deadline passed | Re-login, then reconnect |
| 4403 | Valid token, not a worker | Do not retry |
| 4408 | No client frame for 2 × `WS_HEARTBEAT_S` | Reconnect |
| 1013 | Outbound queue overflow | Reconnect, then re-read pending |
| 1012 | Server restarting (sent by uvicorn on shutdown) | Reconnect, then re-read pending |

**Client reconnect contract (no missed offers):** connect → send `auth` → wait for `auth.ok` → `GET /api/offers/pending` → merge by `offer_id`. The order matters: `auth.ok` is sent only after registration, so anything committed after it is pushed, and anything committed before it is in the poll. An offer can arrive both ways, so handling must be idempotent on `offer_id`. If an `offer.expired`/`offer.taken` for an offer arrives before the poll response that still lists it, the client keeps the closure. In any remaining race the server's accept answer (409/410) is authoritative.

### `OfferNotifier` (`backend/app/services/offer_notifier.py`, new)

`publish` takes `company_id` and the registry key is `(company_id, user_id)`: a publish for company A cannot reach a socket registered under company B, even with an identical user id. That is what makes the isolation test meaningful.

```python
"""Push offer events to connected workers.

``OfferNotifier`` is the seam a multi-instance adapter (Redis pub/sub, not
built) implements. ``InProcessOfferNotifier`` only reaches sockets on this
process. ``publish`` is called from sync code on threadpool threads; each
connection's queue is only touched on its own event loop, hence
``call_soon_threadsafe``.
"""

import asyncio
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings
from app.schemas.jobs import OfferEvent

logger = logging.getLogger(__name__)

# Queued instead of an event when a connection overflows: tell the sender to
# close so the client reconnects and re-reads GET /api/offers/pending.
OVERFLOW = object()


@dataclass(eq=False)
class OfferConnection:
    company_id: int
    user_id: int
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def enqueue(self, item: object) -> None:
        """Runs on ``loop``. A bare ``queue.put_nowait`` here would lose
        QueueFull to the loop's exception handler, and the client would never
        know it missed an event."""
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("offer queue full for user %s; closing socket", self.user_id)
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(OVERFLOW)


class OfferNotifier(Protocol):
    def connect(self, company_id: int, user_id: int) -> OfferConnection: ...

    def disconnect(self, connection: OfferConnection) -> None: ...

    def publish(self, company_id: int, worker_id: int, event: OfferEvent) -> None: ...


class InProcessOfferNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()  # publish runs on threadpool threads
        self._connections: dict[tuple[int, int], set[OfferConnection]] = defaultdict(set)

    def connect(self, company_id: int, user_id: int) -> OfferConnection:
        """Must run on the socket's event loop: it captures the running loop."""
        connection = OfferConnection(
            company_id,
            user_id,
            asyncio.get_running_loop(),
            asyncio.Queue(maxsize=settings.WS_QUEUE_MAX),
        )
        with self._lock:
            self._connections[(company_id, user_id)].add(connection)
        return connection

    def disconnect(self, connection: OfferConnection) -> None:
        key = (connection.company_id, connection.user_id)
        with self._lock:
            connections = self._connections.get(key)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    del self._connections[key]

    def publish(self, company_id: int, worker_id: int, event: OfferEvent) -> None:
        with self._lock:
            targets = list(self._connections.get((company_id, worker_id), ()))
        for connection in targets:
            try:
                connection.loop.call_soon_threadsafe(connection.enqueue, event)
            except RuntimeError:  # the socket's loop has closed
                self.disconnect(connection)


_notifier: OfferNotifier = InProcessOfferNotifier()


def get_offer_notifier() -> OfferNotifier:
    return _notifier
```

### Auth (`backend/app/auth/deps.py`)

Extract `user_for_token` from `get_current_user` so there is exactly one token check. Do not reimplement validation. `get_current_user`'s behaviour is unchanged and pinned by the existing auth tests.

```python
def user_for_token(db: Session, token: str) -> User | None:
    """The active user ``token`` names, or None. The one token check: HTTP and
    WebSocket auth both call it. Role and company come from the database row,
    never from the token."""
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
        token_version = claims["tv"]
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(token_version, int) or isinstance(token_version, bool):
        return None

    user = get_user_for_token(db, user_id)
    if user is None or not user.is_active or user.token_version != token_version:
        return None
    return user


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to an active user, or raise 401."""
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized()
    user = user_for_token(db, creds.credentials)
    if user is None:
        raise _unauthorized()
    return user
```

New constants beside `REQUIRE_ROLE_MARKER` (`deps.py:23`): `REQUIRE_WS_ROLE_MARKER = "__require_ws_role__"`, `WS_CLOSE_UNAUTHENTICATED = 4401`, `WS_CLOSE_FORBIDDEN = 4403`. New imports: `asyncio`, `json`, `dataclasses.dataclass`, `WebSocket`, `WebSocketDisconnect`, `WebSocketException` from `fastapi`, `run_in_threadpool` from `fastapi.concurrency`, and `settings` from `app.config`. New code at the end of the file:

```python
@dataclass(frozen=True)
class WsPrincipal:
    user_id: int
    company_id: int
    role: Role
    token: str  # kept to re-validate on every heartbeat


def ws_principal(db: Session, token: str) -> WsPrincipal | None:
    """Sync: run in the threadpool. Always closes ``db``: on SQLite every
    transaction is BEGIN IMMEDIATE (app/db.py), so a session left open would
    hold the database's single write lock for as long as the socket lives."""
    try:
        user = user_for_token(db, token)
        return None if user is None else WsPrincipal(user.id, user.company_id, user.role, token)
    finally:
        db.close()


async def _first_message_token(websocket: WebSocket) -> str | None:
    """The token from a ``{"type":"auth","token":...}`` text frame, else None.
    ``receive()``, not ``receive_text()``: a binary frame raises KeyError there."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))  # see app/main.py
    text = message.get("text")
    if text is None:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("type") != "auth":
        return None
    token = body.get("token")
    return token if isinstance(token, str) and token else None


def require_ws_role(*roles: Role) -> Callable[..., object]:
    """WebSocket counterpart of ``require_role``: accept, then first-message auth.

    Accept comes first: closing before accept is an HTTP 403 with no close code
    on a real server. ``tests/test_route_audit.py`` requires this on every
    WebSocket route under /api.
    """
    if not roles:
        raise ValueError("require_ws_role() needs at least one role")
    allowed = frozenset(roles)

    async def _require_ws_role(websocket: WebSocket, db: Session = Depends(get_db)) -> WsPrincipal:
        await websocket.accept()
        try:
            token = await asyncio.wait_for(
                _first_message_token(websocket), settings.WS_AUTH_TIMEOUT_S
            )
        except TimeoutError:
            token = None
        principal = await run_in_threadpool(ws_principal, db, token) if token else None
        if principal is None:
            raise WebSocketException(WS_CLOSE_UNAUTHENTICATED, "Not authenticated")
        if principal.role not in allowed:
            raise WebSocketException(WS_CLOSE_FORBIDDEN, "Insufficient role")
        return principal

    setattr(_require_ws_role, REQUIRE_WS_ROLE_MARKER, tuple(roles))
    return _require_ws_role
```

### WebSocket router (`backend/app/routers/ws.py`, new)

```python
"""WS /api/ws/worker: realtime offer events for one authenticated worker.

Protocol (JSON text frames):
  client -> {"type":"auth","token":"<jwt>"}   first frame, within WS_AUTH_TIMEOUT_S
  server -> {"type":"auth.ok"}                after the socket is registered
  server -> offer.created | offer.expired | offer.taken
  server -> {"type":"ping"}   client -> {"type":"pong"}   every WS_HEARTBEAT_S
  client -> {"type":"ping"}   server -> {"type":"pong"}
Close codes: 4401 not authenticated, 4403 not a worker, 4408 heartbeat timeout,
1013 outbound queue overflow (reconnect and re-read GET /api/offers/pending).
"""

import json
import time
from collections.abc import Awaitable

import anyio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.auth.deps import WS_CLOSE_UNAUTHENTICATED, WsPrincipal, require_ws_role, ws_principal
from app.config import settings
from app.db import get_db
from app.models import Role
from app.services.offer_notifier import OVERFLOW, OfferConnection, get_offer_notifier

router = APIRouter(tags=["realtime"])

WS_CLOSE_HEARTBEAT_TIMEOUT = 4408
WS_CLOSE_OVERLOADED = 1013

_ws_worker = require_ws_role(Role.worker)


async def _sender(websocket: WebSocket, connection: OfferConnection) -> int | None:
    """The only task that sends: everything outbound goes through the queue,
    so frames stay in order and a pong proves earlier events were delivered."""
    while True:
        item = await connection.queue.get()
        if item is OVERFLOW:
            return WS_CLOSE_OVERLOADED
        text = json.dumps(item) if isinstance(item, dict) else item.model_dump_json()
        try:
            await websocket.send_text(text)
        except WebSocketDisconnect:
            return None


async def _receiver(websocket: WebSocket, connection: OfferConnection, seen: list[float]) -> None:
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
    db: Session, principal: WsPrincipal, connection: OfferConnection, seen: list[float]
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


@router.websocket("/api/ws/worker")
async def worker_socket(
    websocket: WebSocket,
    principal: WsPrincipal = Depends(_ws_worker),
    db: Session = Depends(get_db),
) -> None:
    notifier = get_offer_notifier()
    connection = notifier.connect(principal.company_id, principal.user_id)
    seen = [time.monotonic()]
    close_code: int | None = None
    # After connect, so nothing committed from here on can be missed.
    connection.enqueue({"type": "auth.ok"})
    try:
        # An anyio task group, not asyncio.create_task: whichever task finishes
        # first ends the session, and server/TestClient cancellation is honoured.
        async with anyio.create_task_group() as group:

            async def run(step: Awaitable[int | None]) -> None:
                nonlocal close_code
                close_code = await step
                group.cancel_scope.cancel()

            group.start_soon(run, _sender(websocket, connection))
            group.start_soon(run, _receiver(websocket, connection, seen))
            group.start_soon(run, _heartbeat(db, principal, connection, seen))
    finally:
        notifier.disconnect(connection)
    if close_code is not None:
        await websocket.close(code=close_code)
```

### Dispatch service (`backend/app/services/dispatch.py`)

New section, before `# --- jobs ---` (`dispatch.py:198`):

```python
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
```

New imports: `Mapping` from `collections.abc`; `JobOfferOut`, `OfferClosedEvent`, `OfferCreatedEvent`, `OfferEvent`, `OfferJobSummary`, `PendingOfferOut` from `app.schemas.jobs`; `get_offer_notifier` from `app.services.offer_notifier`. Changes to existing functions:

```python
# transition_job: replaces dispatch.py:253-259
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

# _expire_pending: replaces dispatch.py:294-300
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

# accept_offer: replaces dispatch.py:365-372
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

# decline_offer: replaces dispatch.py:405-411
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

# ExpirySweep: add a third field after job_ids (dispatch.py:448)
    closed: list[ClosedOffer]

# check_offer_expiry: replaces dispatch.py:464-473
    closed = _expire_returning(
        db,
        update(JobOffer).where(
            JobOffer.company_id == company_id,
            JobOffer.status == OfferStatus.pending,
            JobOffer.expires_at <= now,
        ),
    )

# check_offer_expiry: replaces dispatch.py:484
    return ExpirySweep(len(closed), list(job_ids), closed)

# sweep: after the commit at dispatch.py:531
    _publish(company_id, _closed_events("offer.expired", expiry.closed))

# sweep: replaces dispatch.py:538-542 (inside the existing try)
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
```

### Route audit branch (`backend/tests/test_route_audit.py`, first statement inside the loop at line 83)

```python
        if isinstance(route.original, APIWebSocketRoute):
            # Checked before the allowlist: a socket is never public. No
            # get_tenant_db here: it needs a bearer header, and a socket must
            # not hold a session open. WS handlers never read tenant data.
            if route.dependant is None or not any(
                hasattr(dep.call, REQUIRE_WS_ROLE_MARKER) for dep in _walk(route.dependant)
            ):
                violations.append(f"WS {route.path}: missing require_ws_role(...)")
            continue
```

### `backend/app/main.py`

Import `ws` at line 9, add `app.include_router(ws.router)` after line 30, mention `require_ws_role` in the comment at lines 22-25, and add, above `/api/health`:

```python
@app.exception_handler(WebSocketDisconnect)
async def _client_left(websocket: WebSocket, exc: WebSocketDisconnect) -> None:
    """A client leaving is not a server error: without this, leaving during
    auth (or closing a socket that has just gone) logs an ASGI traceback."""
```

### Multi-instance follow-up (document only, do not build)

`InProcessOfferNotifier` reaches only sockets on its own process, which is correct for today's single `uvicorn` (`Dockerfile:24`). Running `--workers N` or more than one replica would silently miss pushes to sockets on other processes; polling would still recover them, so it would be degraded rather than broken. The adapter would implement `OfferNotifier` by having `publish` do a Redis `PUBLISH offers:{company_id}:{worker_id}` and having each process subscribe and hand messages to its local in-process registry. `connect` and `disconnect` are unchanged, as are the `dispatch.py` call sites. Only `_notifier` in `offer_notifier.py` is swapped.

## Task list

**How to commit this list:** each RED task is paired with the GREEN task after it. `prp-implement` only commits on green gates (`.claude/commands/prp-implement.md:37-49`), so confirm RED for the stated reason, leave it uncommitted, and commit the pair together when the GREEN task passes. Task order is load-bearing: the audit branch (Task 4) lands before the route (Task 12), which would otherwise fail the audit.

All new tests go in `backend/tests/test_ws_dispatch.py` unless a task says otherwise. Its header imports the helpers listed in Context from `tests.test_dispatch`, including the fixtures; the one test using `db_file_based`/`file_company` needs `# noqa: F811` on its `def` line and their import needs `# noqa: F401`, or `ruff check .` fails. Add header imports as each phase needs them, so that a RED task fails for its stated reason and earlier tasks stay importable.

### Phase 1: Contract

1. **RED — `backend/tests/test_ws_dispatch.py`:** `test_offer_created_event_json_contract` asserts `OfferCreatedEvent(...).model_dump()` contains `type == "offer.created"`, `offer_id == id`, `job`, `distance_km`, `expires_at`; and that `OfferClosedEvent` accepts `offer.expired`/`offer.taken` and rejects anything else with `pydantic.ValidationError`. Fails on import.
2. **GREEN — `backend/app/schemas/jobs.py`, `backend/app/config.py`:** add the schemas and settings above.

### Phase 2: Auth and route audit

3. **RED — `backend/tests/test_route_audit.py`:** add `test_audit_flags_a_websocket_route_without_ws_auth`, `test_audit_accepts_a_websocket_route_with_ws_auth_through_a_router` and `test_public_allowlist_cannot_exempt_a_websocket_route` (bodies below). **`backend/tests/test_ws_dispatch.py`:** add `test_ws_principal_always_closes_the_session`: with a `MagicMock` session, assert `close` was called once after an invalid token, and once after `get_user_for_token` raises. Fails on import of `require_ws_role`.

    ```python
    def test_audit_flags_a_websocket_route_without_ws_auth():
        leaky = FastAPI()

        @leaky.websocket("/api/ws/leak")
        async def leak(websocket: WebSocket):
            await websocket.accept()

        assert audit_routes(leaky) == ["WS /api/ws/leak: missing require_ws_role(...)"]


    def test_audit_accepts_a_websocket_route_with_ws_auth_through_a_router():
        ok = FastAPI()
        router = APIRouter()

        @router.websocket("/api/ws/fine")
        async def fine(websocket: WebSocket, principal=Depends(require_ws_role(*ALL_ROLES))):
            pass

        ok.include_router(router)
        assert audit_routes(ok) == []


    def test_public_allowlist_cannot_exempt_a_websocket_route():
        leaky = FastAPI()

        @leaky.websocket("/api/ws/leak")
        async def leak(websocket: WebSocket):
            await websocket.accept()

        allowlist = frozenset({"/api/ws/leak"})
        assert audit_routes(leaky, public=allowlist) == [
            "WS /api/ws/leak: missing require_ws_role(...)"
        ]
    ```

4. **GREEN — `backend/app/auth/deps.py`, `backend/tests/test_route_audit.py`:** extract `user_for_token`; add `WsPrincipal`, `ws_principal`, `_first_message_token`, `require_ws_role`; add the `audit_routes` branch and the `APIWebSocketRoute` and `REQUIRE_WS_ROLE_MARKER` imports. Do not touch `PUBLIC_API_PATHS`. Existing `test_auth_login.py`, `test_me.py` and `test_tokens.py` must pass unmodified.

### Phase 3: Notifier

5. **RED — `backend/tests/test_ws_dispatch.py`:** unit tests inside `asyncio.run(...)`: `test_publish_reaches_only_the_matching_company_and_user`, `test_publish_from_a_worker_thread_reaches_the_queue` (publish from `asyncio.to_thread`), `test_every_connection_of_a_worker_receives_the_event`, `test_queue_overflow_replaces_the_backlog_with_the_overflow_signal`, `test_publish_after_the_loop_closed_drops_the_connection`, `test_disconnect_empties_the_registry_key`.
6. **GREEN — `backend/app/services/offer_notifier.py` (new):** as above.

### Phase 4: Dispatch publishes after commit

7. **RED — `backend/tests/test_ws_dispatch.py`:** a `pushed` fixture swaps `offer_notifier._notifier` (`monkeypatch.setattr`) for a recorder whose `publish` appends `(company_id, worker_id, event)`. Tests: `test_offering_publishes_offer_created_with_distance`, `test_cancel_publishes_offer_expired`, `test_accept_publishes_offer_taken_to_losers_only`, `test_decline_publishes_offer_created_for_the_top_up_only`, `test_sweep_publishes_expired_then_created_for_the_next_ring`, `test_lost_accept_race_publishes_nothing`, `test_notifier_failure_does_not_fail_the_request` (recorder raises; HTTP 200 and the database write stands), and `test_no_publish_when_commit_fails`:

    ```python
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
    ```

8. **GREEN — `backend/app/services/dispatch.py`:** add the event section; change `_expire_pending` and `check_offer_expiry` to `RETURNING`; publish at every call site in the table. `test_check_offer_expiry_reports_jobs_without_pending_offers` (`backend/tests/test_dispatch.py:820-836`) must pass unmodified, so keep `expired_count` and `job_ids`. `dispatch.py` importing `offer_notifier` is safe: `offer_notifier` imports only config and schemas.

### Phase 5: Polling fallback

9. **RED — `backend/tests/test_ws_dispatch.py`:** `test_get_pending_matches_the_offer_created_shape`, `test_get_pending_excludes_offers_past_ttl_before_any_sweep`, `test_get_pending_excludes_taken_declined_and_other_workers_offers`, `test_get_pending_requires_the_worker_role`. **`backend/tests/test_route_audit.py`:** add `"/api/offers/pending"` to `test_audit_actually_sees_the_api_routes` (lines 114-121) and `("GET", "/api/offers/pending"): {"worker"}` to `test_dispatch_routes_require_the_prp_roles` (lines 133-141).
10. **GREEN — `backend/app/routers/dispatch.py`:** add the route, with `list_pending_offers` from the Task 8 section already available. It is `GET`, so it does not collide with `POST /api/offers/{offer_id}/accept`; no route-ordering concern.

    ```python
    @router.get("/api/offers/pending", response_model=list[PendingOfferOut])
    def pending_offers(
        limit: int = Query(200, ge=1, le=200),
        user: User = Depends(_worker),
        db: Session = Depends(get_tenant_db),
    ) -> list[PendingOfferOut]:
        """The caller's open offers. Call after every (re)connect's auth.ok."""
        return service.list_pending_offers(db, user.id, limit=limit)
    ```

### Phase 6: The socket

11. **RED — `backend/tests/test_ws_dispatch.py`:** the four named tests below, plus: `test_malformed_first_frame_is_rejected` (parametrized: not JSON, binary, not an object, wrong `type`, no token, non-string token, bad JWT; all 4401), `test_non_worker_rejected_4403`, `test_stale_token_version_rejected`, `test_taken_and_expired_routes`, `test_sweep_pushes_expired`, `test_heartbeat_ping_and_revalidation_close`, `test_heartbeat_timeout_closes_4408`, `test_overflow_closes_1013`, `test_registry_empty_after_disconnect`, `test_every_api_websocket_route_rejects_unauthenticated`, and `test_open_socket_does_not_hold_the_sqlite_write_lock`. **`backend/tests/test_route_audit.py`:** add `"/api/ws/worker"` to `test_audit_actually_sees_the_api_routes`, and add `test_ws_worker_route_requires_the_worker_role` (`test_dispatch_routes_require_the_prp_roles` cannot cover it because WS routes have no methods; see `:142-147`).

    Named test ids, exact:

    - `backend/tests/test_ws_dispatch.py::test_authenticated_worker_receives_offer_created_within_2s`
    - `backend/tests/test_ws_dispatch.py::test_unauthenticated_ws_connection_is_rejected`
    - `backend/tests/test_ws_dispatch.py::test_worker_of_company_b_never_receives_company_a_events`
    - `backend/tests/test_ws_dispatch.py::test_reconnect_then_get_pending_returns_missed_offers`

    ```python
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
        with pytest.raises(WebSocketDisconnect) as exc:
            recv(ws)
        return exc.value.code


    def test_authenticated_worker_receives_offer_created_within_2s(
        client, two_companies, transactional_db


    def test_unauthenticated_ws_connection_is_rejected(client, monkeypatch):
        monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_S", 0.1)
        with client.websocket_connect("/api/ws/worker") as ws:
            assert closed_with(ws) == 4401  # silence past the deadline


    def test_worker_of_company_b_never_receives_company_a_events(
        client, two_companies, transactional_db


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


    def test_open_socket_does_not_hold_the_sqlite_write_lock(db_file_based, file_company):  # noqa: F811
        """Real file database, real commits: every transaction is BEGIN IMMEDIATE,
        so a socket that kept its session open would block this write for the
        full busy timeout and then fail."""
        client = TestClient(app)
        with worker_socket(client, file_company.worker):
            started = time.monotonic()
            response = client.post(
                "/api/jobs", json=job_body(), headers=auth_header(file_company.dispatcher)
            )
            elapsed = time.monotonic() - started
        assert response.status_code == 201
        assert elapsed < SQLITE_BUSY_TIMEOUT_S / 2
    ```

    **Test gotchas.** `recv` is required: without a timeout a missing event hangs the suite instead of failing it. `assert_quiet` is the sentinel for negative assertions: the pong travels through the same FIFO queue as events, so a pong arriving first proves nothing was queued ahead of it. Under the `client` fixture all sessions share one SQLite connection (`conftest.py:37-46`), so a socket test must not drive HTTP during a heartbeat tick; keep `WS_HEARTBEAT_S` at its 30s default except in the heartbeat tests, which make no concurrent HTTP calls. In `test_heartbeat_ping_and_revalidation_close` use `WS_HEARTBEAT_S = 0.2`, bump `token_version`, send `{"type":"pong"}`, then expect 4401.

12. **GREEN — `backend/app/routers/ws.py` (new), `backend/app/main.py`:** as above. Then confirm `git diff --stat main -- backend/requirements.txt backend/migrations` is empty.

### Phase 7: Final check

13. **No new files.** Run every gate once more against the full branch and confirm the four named ids pass by running them explicitly. Add no gate to `CLAUDE.md`, since this PRP adds none.

## Validation gates

```bash
cd backend && pytest -q
cd backend && ruff check .
```

From root `CLAUDE.md`. No `alembic` gate, because there is no schema change (see "Known limitation"). No `npm` gate, because no frontend file changes. Baseline at planning time: `pytest -q` 213 passed, `ruff check .` clean.

Also confirm the named tests explicitly at the end:

```bash
cd backend && pytest -q tests/test_ws_dispatch.py::test_authenticated_worker_receives_offer_created_within_2s tests/test_ws_dispatch.py::test_unauthenticated_ws_connection_is_rejected tests/test_ws_dispatch.py::test_worker_of_company_b_never_receives_company_a_events tests/test_ws_dispatch.py::test_reconnect_then_get_pending_returns_missed_offers
```

## Acceptance criteria

1. The four named tests exist with those exact ids and pass.
2. `cd backend && pytest -q` and `cd backend && ruff check .` pass, and no pre-existing test was weakened.
3. A worker socket authenticates by first frame only: no query-parameter token, and missing, malformed, invalid or stale tokens all close 4401 after accept; a non-worker closes 4403; silence past `WS_AUTH_TIMEOUT_S` closes 4401.
4. `user_for_token` is the only token check; `get_current_user` and `ws_principal` both call it.
5. `audit_routes` has a dedicated `APIWebSocketRoute` branch requiring `require_ws_role`; `PUBLIC_API_PATHS` still equals `{"/api/health", "/api/auth/login"}`; removing the guard from a WS route fails the audit (`test_audit_flags_a_websocket_route_without_ws_auth`).
6. Registry key is `(company_id, user_id)` and `publish()` takes `company_id`.
7. `publish()` is called from sync code via `loop.call_soon_threadsafe`, onto a per-connection `asyncio.Queue`, on the loop captured at connect.
8. Every `_publish` call follows a successful `db.commit()`; `create_offers` never publishes; `test_no_publish_when_commit_fails` and `test_lost_accept_race_publishes_nothing` pass.
9. An open socket holds no database session: `test_open_socket_does_not_hold_the_sqlite_write_lock` passes.
10. `offer.created` carries `offer_id`, a job summary, `distance_km` and `expires_at`; `offer.expired` and `offer.taken` carry `offer_id` and `job_id`; all are defined in `backend/app/schemas/jobs.py`.
11. `GET /api/offers/pending` requires the worker role, reads through `get_tenant_db`, returns the `offer.created` shape, and excludes past-TTL offers before any sweep.
12. Heartbeat works: server pings every `WS_HEARTBEAT_S`, a silent peer closes 4408, and a re-validation failure closes 4401. A disconnect removes the connection from the registry.
13. No change to `backend/requirements.txt`, `backend/migrations/`, `frontend/`, or `OfferStatus`.
14. `git diff main --stat` lists only: `backend/app/auth/deps.py`, `backend/app/config.py`, `backend/app/main.py`, `backend/app/routers/dispatch.py`, `backend/app/routers/ws.py`, `backend/app/schemas/jobs.py`, `backend/app/services/dispatch.py`, `backend/app/services/offer_notifier.py`, `backend/tests/test_route_audit.py`, `backend/tests/test_ws_dispatch.py`, and this PRP.

## Risks

1. **The "within 2s" test does not prove real latency.** `TestClient.websocket_connect` runs the app in-process through an anyio portal, so `test_authenticated_worker_receives_offer_created_within_2s` proves logical ordering and cross-thread delivery, not wall-clock latency against a real ASGI server, real sockets or a proxy. **Follow-up (required before relying on the latency figure):** a `dynamic_tester` mission against a live `uvicorn` process covering latency, close codes on the wire, shutdown behaviour and socket survival through any deployed proxy. The planning prototype's single live run looked healthy, but it is not this check and did not cover a proxy.
2. **A held session stalls every write in the process.** See "the socket must not hold a database session". Anyone adding a query to the socket path can reintroduce it. Guarded by `test_open_socket_does_not_hold_the_sqlite_write_lock` (real file DB; mutation-checked) and `test_ws_principal_always_closes_the_session`.
3. **Heartbeat re-validation adds load.** Each open socket takes one `BEGIN IMMEDIATE` read per `WS_HEARTBEAT_S` for `user_for_token`. At 30s this is small for current scale, but it does serialize with writers on SQLite. If socket counts grow large, raise `WS_HEARTBEAT_S` or move to Postgres, not to skipping re-validation, which would break the `tokens.py:3-6` promise. Deactivation reaches a socket within one heartbeat, not instantly.
4. **`offer.expired` is only as timely as the sweep.** Nothing calls `POST /api/dispatch/sweep` automatically (`routers/dispatch.py:64-65`). If nothing schedules it, workers see no `offer.expired` and rely on their `expires_at` countdown; the server stays correct because accept answers 410 and polling filters on `expires_at`. Scheduling the sweep is an operational follow-up.
5. **Multi-process deployment drops pushes silently.** Adding `--workers` or a second replica makes the in-process registry miss sockets on other processes. Polling still recovers, but realtime would degrade with no error. See "Multi-instance follow-up"; do not scale out before the adapter exists.
6. **A proxy may idle-close sockets or strip upgrades.** The 30s heartbeat keeps traffic flowing, but no proxy was tested, and the Vite dev proxy lacks `ws: true` (`frontend/vite.config.js:6`). Clients must reconnect with backoff.
7. **Unauthenticated sockets consume resources.** Each is accepted and held for up to `WS_AUTH_TIMEOUT_S`, with no connection-count limit, and uvicorn's default maximum frame size applies. `CORSMiddleware` (`main.py:12-14`) does not apply to WebSockets, but cross-site socket hijacking does not apply either, because auth needs a token in the first frame and no cookie is involved. Add limits at the proxy or as a follow-up. A `security` review should look at this.
8. **`>=` floors can move framework internals.** `effective_routes` already tolerates FastAPI's route-listing changes (`test_route_audit.py:39-44`). `APIWebSocketRoute` and the task-group/`TestClient` behaviour were observed only on the versions listed under "Evidence". `test_audit_actually_sees_the_api_routes` fails loudly if the route becomes invisible.
9. **The token stays in server memory for the life of the socket,** held in `WsPrincipal` for re-validation. Never log it or include it in a close reason. It expires on its own schedule (`ACCESS_TOKEN_TTL_MIN`, `config.py:10`), at which point the next heartbeat closes the socket 4401, and clients must re-login and reconnect, so sockets do not outlive a token by more than one heartbeat. There is no token refresh over the socket.
10. **Global registry in tests.** `_notifier` is module-level by design, since the fixtures run `TestClient(app)` without lifespan (`conftest.py:48`). The savepoint fixture rolls back between tests, so user ids can repeat, and a socket a test failed to close could receive another test's events. Tests open sockets only in `with` blocks, and `test_registry_empty_after_disconnect` pins the cleanup.

## Rollback

No migration, so there is no schema step.

1. **Before merge:** abandon the branch. Deleting it (`git branch -D prp/G4`) is destructive and **requires human approval**.
2. **After merge, full undo:** revert the merge commit. Nothing persists outside code, because no table or column is added. Connected clients are disconnected by the deploy, and workers fall back to whatever polling they had.
3. **Disable the socket only:** remove `app.include_router(ws.router)` from `backend/app/main.py`. Then no connection can register, every `publish` is a no-op against an empty registry, `GET /api/offers/pending` keeps working, and clients can poll. The audit branch can stay.
4. **Disable pushes only:** make `_publish` in `dispatch.py` return immediately. Dispatch behaviour is otherwise unchanged, because publishing follows the commit and has no effect on the transaction.
