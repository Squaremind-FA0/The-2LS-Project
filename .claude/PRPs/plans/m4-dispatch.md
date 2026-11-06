# Plan: M4 Dispatch engine + realtime job offers

## Summary

`main` @ `94f83c8` already has a full dispatch engine and realtime offer push
built under PRPs G3 (`PRPs/G3-jobs-dispatch.md`) and G4
(`PRPs/G4-realtime-offers.md`): `Job`/`JobOffer`/`WorkerLocation` models, a
haversine+bounding-box matcher (`backend/app/services/dispatch.py:70-198`),
a race-safe first-wins accept (`:535-583`), a bounded expiry sweep
(`:712-773`), and an authenticated WebSocket hub for workers
(`backend/app/routers/ws.py`, `backend/app/services/offer_notifier.py`). M4
does **not** rebuild any of this — it closes the gaps between that code and
the M4 brief's specific acceptance criteria.

## Gaps vs. the brief (verified against the code before writing this plan)

| Brief requirement | Current state | Gap |
|---|---|---|
| "Accept and decline are idempotent: repeating the same response returns the same result" | `accept_offer` raises `Conflict("Offer already accepted")` on repeat (`dispatch.py:523`); `decline_offer` likewise (`:595`). Pinned by `test_accepting_twice_is_409` and `test_decline_when_offer_already_declined_is_409`. | Real behavior change needed. |
| "The losers' offers become superseded" | `OfferStatus` has only `pending/accepted/declined/expired` (`models/job_offer.py:14-18`); losers go to `expired`, documented in G4 as "accepted, not a gap" (`PRPs/G4-realtime-offers.md:90-94`). | New enum value `superseded`, used only for accept-race losers (cancel/timeout stay `expired`). |
| "Run expiry in a bounded asyncio background task started in the FastAPI lifespan, plus a lazy check on read" | No lifespan exists (`app/main.py:1-68`, plain `FastAPI(title="API")`). Sweep is only `POST /api/dispatch/sweep` (admin-triggered), explicitly "not a daemon" by G4 design (`PRPs/G4-realtime-offers.md:11`). Lazy check is **already done**: `list_pending_offers` filters `expires_at > now` (`dispatch.py:303-317`) and `_refuse_unavailable`/`decline_offer` both check `expires_at <= now` (`:529`, `:597`). | Add the background task only; lazy check needs no change. |
| "Channels: worker:{id} gets offer.created/offer.revoked; company:{id} gets job.updated" | `offer_notifier.py` registers `(company_id, worker_id)` and pushes to workers only; no company/dispatcher channel exists. | New `company:{id}` channel + `job.updated` event. Event name kept as `offer.taken`/`offer.expired` (not `offer.revoked`) — see Risks. |
| Ring default K=3, `backend/app/dispatch/matcher.py` as pure functions, `test_matcher.py` | `DISPATCH_RING_SIZE` defaults to 10 (`config.py:17`); the matcher's pure functions (`haversine_km`, `bounding_box`, `has_trade`, `rank_candidates`) live inline in `services/dispatch.py:70-131`; their tests live in `test_dispatch.py`. | Flip default to 3; extract to `app/dispatch/matcher.py`; move/extend tests into `test_matcher.py`. |
| `POST /api/jobs/{id}/dispatch`, `GET /api/me/offers` | Equivalent behavior exists as `PATCH /api/jobs/{id}` (`status: "dispatching"`) and `GET /api/offers/pending` (`routers/dispatch.py:46-54`), already consumed by `frontend/src/api/client.js:109`, `:115` and `OfferProvider.jsx`. | Add as thin aliases; keep the existing routes (frontend depends on them). |
| `/api/ws?token=<jwt>`, 4401 on bad token | G4 deliberately chose first-frame auth over a query-param token (tokens leak into logs/proxies), already closes 4401 (`routers/ws.py`, `auth/deps.py:require_ws_role`). `OfferProvider.jsx` already speaks this protocol. | **Not a gap** — documented divergence, no change. |
| `test_matcher.py` / `test_ws.py` acceptance test filenames | Equivalent coverage lives in `test_dispatch.py` / `test_ws_dispatch.py`. | `test_matcher.py` is new (task below). `test_ws_dispatch.py` is kept as-is (renaming risks nothing functional and the file is large and actively maintained); new company-channel/job.updated tests are added there. Documented divergence. |

## Non-goals

- Rebuilding or rewriting the existing matcher, accept race guard, or worker
  WebSocket path — they are race-safe and already covered by
  `test_dispatch.py`/`test_ws_dispatch.py`.
- A Redis/multi-instance adapter — `app/realtime/hub.py`'s interface is
  designed for one, per G4's documented follow-up
  (`PRPs/G4-realtime-offers.md:756-758`), but it is not built.
- Renaming `offer.taken`/`offer.expired` to `offer.revoked`, or the WS auth
  transport (`/api/ws?token=`) — both would break `frontend/src/offers/OfferProvider.jsx`
  (:113-124) for no behavioral gain. Documented divergence, not a regression.
- Frontend changes. No task below touches `frontend/`; `npm run build`/`npm test`
  are run only to confirm nothing regressed.
- A migration. Every change here is either a new `Settings` field, a new
  Python enum member on an already-`native_enum=False` VARCHAR(20) column
  with no DB-level CHECK constraint (`migrations/versions/0003_jobs_dispatch.py:85-89`
  has none), or new runtime code. Confirmed no schema change is needed.

## Branch

`feat/m4-dispatch` off `main` (no remote exists; local squash-merge at the end,
per M3's precedent).

## Patterns to mirror

- Conditional UPDATE + rowcount, refresh-after-rollback-to-check-idempotency:
  `dispatch.py:406-411` (`_apply` in `job_state.py`), extended below.
- Commit-then-notify: build events before `db.commit()`, publish after
  (`dispatch.py:238-241`).
- Stale-read guard tests, not just concurrent-thread tests, per project
  lesson: `test_accept_guard_holds_without_application_prechecks`
  (`test_dispatch.py:568-587`) monkeypatches the precheck away to force the
  DB guard deterministically. The same pattern is used below for the new
  idempotent-race branch.
- `db_file_based`/`file_company` fixtures (`test_dispatch.py:105-138`) for
  anything needing a second, independent session/connection — reused for the
  background-sweep orchestration test instead of the shared-connection
  `client`/`transactional_db` fixtures, which cannot host a second open
  transaction on the same SQLite connection (see Risks).

## Data model / API contracts

### `OfferStatus` (`backend/app/models/job_offer.py`)

Add `superseded = "superseded"`. No migration: the column is
`Enum(OfferStatus, name="offer_status", native_enum=False, length=20)`, a plain
`VARCHAR(20)` with no CHECK constraint. `"superseded"` is 10 characters.

### `Settings` (`backend/app/config.py`)

```python
DISPATCH_RING_SIZE: int = 3            # was 10
DISPATCH_SWEEP_INTERVAL_S: float = 10.0  # new: background sweep cadence
```

### New schemas (`backend/app/schemas/jobs.py`)

```python
class JobUpdatedEvent(BaseModel):
    type: Literal["job.updated"] = "job.updated"
    job: JobOut

CompanyEvent = JobUpdatedEvent
```

### New module `backend/app/dispatch/__init__.py`, `backend/app/dispatch/matcher.py`

Pure functions moved verbatim from `services/dispatch.py:70-131`:
`EARTH_RADIUS_KM`, `haversine_km`, `BoundingBox`, `bounding_box`, `has_trade`,
`Candidate`, `rank_candidates`. `services/dispatch.py` imports them from here;
no behavior change.

### New module `backend/app/realtime/__init__.py`, `backend/app/realtime/hub.py`

A second, generic, channel-keyed notifier — the "small interface" the brief
asks for — used only for the new company/dispatcher channel. The existing
`offer_notifier.py` (keyed `(company_id, worker_id)`) is left untouched: it
already is this interface for workers, proven by `test_ws_dispatch.py`, and
unifying the two keying schemes is not worth the regression risk in this PRP.

```python
@dataclass(eq=False)
class HubConnection:
    channel: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def enqueue(self, item: object) -> None: ...  # identical overflow handling to OfferConnection

class Hub(Protocol):
    def connect(self, channel: str) -> HubConnection: ...
    def disconnect(self, connection: HubConnection) -> None: ...
    def publish(self, channel: str, event: object) -> None: ...

class InProcessHub:
    ...  # same shape as InProcessOfferNotifier, keyed by a single string

def get_hub() -> Hub: ...
```

`routers/ws.py`'s `_sender`/`_receiver`/`_heartbeat` are reused unchanged for
the new socket: they only touch `connection.queue`/`connection.enqueue(...)`,
so `HubConnection` is structurally compatible.

### New route: `WS /api/ws/company`

`require_ws_role(Role.dispatcher, Role.company_admin)`. Same protocol as
`/api/ws/worker` (first-frame `{"type":"auth","token":...}`, `auth.ok`,
heartbeat, 4401/4403/4408/1013). Channel `f"company:{principal.company_id}"`.
Pushes `job.updated` after `transition_job`, `accept_offer`, and the sweep's
unfilled branch commit.

### New routes (thin aliases, additive — existing routes are unchanged)

```
POST /api/jobs/{job_id}/dispatch
  dependencies: require_role(Role.dispatcher, Role.company_admin), get_tenant_db
  Same effect as PATCH /api/jobs/{job_id} {"status": "dispatching"}, no version
  check (the brief's contract has none). response: JobOut.

GET /api/me/offers
  dependencies: require_role(Role.worker), get_tenant_db
  Same as GET /api/offers/pending. response: list[PendingOfferOut].
```

### Idempotent accept/decline — exact new behavior

`accept_offer(db, offer_id, worker_id, now)`:
1. If `offer.status is OfferStatus.accepted`: return the offer as-is (200,
   same body) — the only worker who can reach `accepted` on their own offer
   is this one, by `uq_job_offers_job_worker`.
2. Otherwise, run the existing guards (`job.assigned_worker_id is not None`
   → 409; `declined`/`superseded` → 409; `expired`/past `expires_at` → 410;
   job not `dispatching` → 409).
3. Attempt the conditional `transition(... guards=(Job.assigned_worker_id.is_(None),) ...)`.
   On `StaleJob`/`IllegalTransition`: `db.rollback()`, `db.refresh(job)`. If
   `job.assigned_worker_id == worker_id` (a concurrent duplicate request from
   the *same* worker already won), `db.refresh(offer)` and return it if now
   `accepted` (200, idempotent even under true concurrency). Otherwise 409.
4. On success, supersede (not expire) the other pending offers for the job.

`decline_offer(db, offer_id, worker_id, now)`: symmetric — `declined` repeat
returns 200 as-is; a conditional-update rowcount of 0 is followed by a
refresh-and-check for the same-worker race before raising 409.

## Task list (test-first, one commit each)

1. **RED — `backend/tests/test_dispatch.py`**: add
   `test_accept_is_idempotent_on_repeat` (sequential: accept, then accept
   again, expect `200` both times with identical `status`/`worker_id`/`id`),
   `test_decline_is_idempotent_on_repeat` (symmetric), and
   `test_accept_idempotent_under_a_forced_stale_read` (deterministic:
   manually set `job.assigned_worker_id = w1.id` and the offer's status to
   `accepted` between read and write — mirror
   `test_accept_guard_holds_without_application_prechecks`'s monkeypatch
   style — then call `accept_offer` again for `w1` and assert `200`, per the
   project lesson that concurrent-thread tests alone don't reach this
   branch on SQLite's `BEGIN IMMEDIATE`). Update
   `test_concurrent_same_offer_double_accept` to expect `[200, 200, 200, 200]`
   with every response body equal. Update `test_accepting_twice_is_409` →
   rename `test_accepting_twice_is_idempotent`, assert `200` not `409`.
   Update `test_decline_when_offer_already_declined_is_409` → rename
   `test_declining_twice_is_idempotent`, assert `200`.
2. **GREEN — `backend/app/services/dispatch.py`**: implement the idempotent
   accept/decline logic above.
3. **RED — `backend/tests/test_dispatch.py`**: change the four accept-race-loser
   assertions from `OfferStatus.expired` to `OfferStatus.superseded`
   (`test_accept_assigns_job_and_expires_rest_of_ring`,
   `test_accept_guard_holds_without_application_prechecks`,
   `test_concurrent_ring_double_accept_one_wins`, `test_e2e_job_dispatch_flow`).
   Leave `test_cancel_offering_job_expires_pending_offers` and the sweep
   timeout tests (`test_sweep_expires_offers_and_re_offers_next_ring`, etc.)
   asserting `expired` — they are not accept races.
4. **GREEN — `backend/app/models/job_offer.py`, `backend/app/services/dispatch.py`**:
   add `OfferStatus.superseded`; add `_supersede_pending` (same shape as
   `_expire_pending`, `status=OfferStatus.superseded`); `accept_offer` calls
   it instead of `_expire_pending` for the losing ring; guard checks
   (`_refuse_unavailable`, `decline_offer`) treat `superseded` like `expired`
   for 409 purposes.
5. **RED — `backend/tests/test_matcher.py` (new file)**: move
   `test_haversine_matches_known_distance`, `test_bounding_box_contains_radius`,
   `test_bounding_box_near_antimeridian_skips_lng_filter`,
   `test_radius_boundary_9_9km_matched_10_1km_not`,
   `test_cross_company_worker_never_matched`, `test_trade_must_match`,
   `test_trade_match_is_case_insensitive`, `test_stale_location_excluded`,
   `test_results_sorted_nearest_first`, `test_limit_caps_results`,
   `test_inactive_users_and_profiles_excluded`,
   `test_workers_already_offered_this_job_excluded` out of `test_dispatch.py`
   (same bodies, importing `app.dispatch.matcher` plus the `add_worker`/`add_job`/
   `match_ids` helpers re-exported from `tests.test_dispatch`, `# noqa: F401`
   per the G4 cross-import pattern). Add
   `test_availability_filter_excludes_unavailable_worker` (new: set
   `WorkerProfile.is_available = False` directly, as `expire_offers_now`
   mutates rows, and assert the worker is excluded — no existing test covers
   this flag distinctly from `active`/`is_active`). Add two genuinely pure
   unit tests with no DB: `test_has_trade_is_case_insensitive_on_bare_strings`
   and `test_rank_candidates_sorts_by_distance_then_id` constructed from plain
   `User`-shaped tuples, not ORM rows. Fails on import (module doesn't exist yet).
6. **GREEN — `backend/app/dispatch/__init__.py`, `backend/app/dispatch/matcher.py`,
   `backend/app/services/dispatch.py`**: extract the pure functions; re-import
   in `services/dispatch.py`; delete the moved tests from `test_dispatch.py`.
7. **RED — `backend/tests/test_dispatch.py`**: add
   `test_dispatch_ring_size_defaults_to_three` (no monkeypatch; 4 workers at
   increasing distance; only the nearest 3 are offered).
8. **GREEN — `backend/app/config.py`**: `DISPATCH_RING_SIZE: int = 3`.
9. **RED — `backend/tests/test_dispatch.py`**: add
   `sweep_all_companies`-level test `test_sweep_all_companies_sweeps_every_company_independently`
   using `db_file_based`/`file_company`-style fixtures (two companies, each
   with one expired offer); call the new orchestration function directly
   with a custom `db_factory`; assert both companies' jobs get re-offered and
   one company's failure (monkeypatch `dispatch.sweep` to raise for company
   B only) doesn't block company A's sweep.
10. **GREEN — `backend/app/services/dispatch.py`**: add
    `sweep_all_companies(db_factory: Callable[[], Session] = SessionLocal) -> dict[int, SweepSummary]`:
    lists `Company.id` (untenanted — `Company` isn't `TenantScoped`), and for
    each opens a session via `db_factory()`, sets
    `db.info[TENANT_INFO_KEY] = company_id` (the exact mechanism
    `get_tenant_db` uses, `auth/deps.py:110`), calls `sweep(db, company_id)`,
    closes the session; a per-company exception is logged and skipped like
    the existing per-job loop in `sweep` (`dispatch.py:769-771`).
11. **RED — `backend/tests/test_dispatch.py`**: add
    `test_lifespan_starts_and_cancels_the_sweep_loop` using
    `with TestClient(app):` (the one way to trigger FastAPI's lifespan in
    this Starlette version — `client`/`transactional_db` never do, by design,
    per `PRPs/G4-realtime-offers.md` risk #10) against the plain in-memory
    test engine with zero companies seeded, monkeypatching
    `settings.DISPATCH_SWEEP_INTERVAL_S` to `0.05`; assert the app starts and
    stops cleanly and the loop ran at least once (spy via monkeypatching
    `dispatch.sweep_all_companies`).
12. **GREEN — `backend/app/main.py`**: add an `asynccontextmanager` lifespan
    that starts `asyncio.create_task` running a loop
    (`run_in_threadpool(dispatch.sweep_all_companies)`, sleep
    `settings.DISPATCH_SWEEP_INTERVAL_S`, catch and log any exception so one
    bad tick never kills the loop) and cancels it on shutdown.
13. **RED — `backend/tests/test_ws_dispatch.py`**: add
    `test_company_channel_receives_job_updated_on_dispatch`,
    `test_company_channel_receives_job_updated_on_accept`,
    `test_company_channel_receives_job_updated_on_unfilled`,
    `test_company_socket_rejects_a_worker_4403`,
    `test_company_channel_is_isolated_per_company`. **`test_route_audit.py`**:
    add `"/api/ws/company"` to `test_audit_actually_sees_the_api_routes` and
    add `test_ws_company_route_requires_dispatcher_or_admin_role` (mirroring
    `test_ws_worker_route_requires_the_worker_role`).
14. **GREEN — `backend/app/realtime/__init__.py`, `backend/app/realtime/hub.py`
    (new), `backend/app/routers/ws.py`, `backend/app/services/dispatch.py`,
    `backend/app/schemas/jobs.py`**: add the hub, the `/api/ws/company` route
    reusing `_sender`/`_receiver`/`_heartbeat`, `JobUpdatedEvent`, and a
    `_publish_job_updated(company_id, job)` call after commit in
    `transition_job` (on `dispatching` and `cancelled`), `accept_offer`, and
    `sweep`'s unfilled branch.
15. **RED — `backend/tests/test_dispatch.py`**: add
    `test_post_jobs_dispatch_alias_matches_patch_status_dispatching` and
    `test_get_me_offers_alias_matches_offers_pending`. **`test_route_audit.py`**:
    add both new paths to `test_audit_actually_sees_the_api_routes` and
    `("POST", "/api/jobs/{job_id}/dispatch"): {"dispatcher", "company_admin"}`,
    `("GET", "/api/me/offers"): {"worker"}` to
    `test_dispatch_routes_require_the_prp_roles`.
16. **GREEN — `backend/app/routers/jobs.py`, `backend/app/routers/dispatch.py`**:
    add the two alias routes.
17. **Final check**: re-run every validation gate against the full branch;
    confirm every acceptance criterion below explicitly.

## Validation gates

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head      # needs JWT_SECRET; use a scratch DATABASE_URL
cd backend && alembic downgrade base    # then upgrade head again to prove the round trip
cd frontend && npm run build
cd frontend && npm test
```

From root `CLAUDE.md`. No new revision is expected in the alembic step —
confirms the "no migration" non-goal held. `npm run build`/`npm test` confirm
no frontend regression; no frontend file is touched by this PRP.

## Acceptance criteria

- [ ] `test_matcher.py`: radius boundary (9.9 km in / 10.1 km out), trade
      filter, availability filter, cross-company exclusion, distance
      ordering — all pass.
- [ ] `test_dispatch.py`: dispatch creates K(=3 by default) offers;
      concurrent double accept across two workers leaves exactly one 200 and
      one 409; repeating an accept or decline (sequentially, and under a
      forced same-worker race) is idempotent (200, identical body); expiry
      escalates to the next batch; exhaustion leads to `unfilled`.
- [ ] `test_ws_dispatch.py`: a worker receives `offer.created` after
      dispatch (existing, unchanged); a bad token closes 4401 (existing,
      unchanged); a dispatcher/admin receives `job.updated` on the new
      `company:{id}` channel on dispatch, accept, and unfilled.
  - Documented divergence: the acceptance brief says `test_ws.py`; this repo
    keeps the existing, actively-maintained `test_ws_dispatch.py` instead of
    a rename, per G4 precedent and to avoid churn with no behavior change.
- [ ] A bounded background task started in the FastAPI lifespan sweeps every
      company on `DISPATCH_SWEEP_INTERVAL_S`; a restart cannot leave an
      offer stuck past its TTL, because `GET /api/offers/pending` and the
      accept/decline guards already filter on `expires_at` independent of
      any sweep having run.
- [ ] `POST /api/jobs/{id}/dispatch` and `GET /api/me/offers` exist and are
      behaviorally identical to their existing counterparts.
- [ ] No new Alembic migration; `backend/requirements.txt` unchanged.
- [ ] Squash-merged to `main`.

## Risks

| Risk | Mitigation |
|---|---|
| The same-worker concurrent-duplicate-accept fix (task 1-2) depends on refreshing after rollback seeing the winner's already-committed row — only true because every SQLite transaction here is `BEGIN IMMEDIATE` (`db.py:46-48`), so a blocked second writer's retry is guaranteed to run after the first's commit. A Postgres port would need the same conditional-UPDATE contract re-verified (it holds: `READ COMMITTED` re-reads row state after lock release) but is untested here. | `test_accept_idempotent_under_a_forced_stale_read` pins the logic deterministically, independent of timing; `test_concurrent_same_offer_double_accept` is the belt-and-suspenders real-thread version. |
| A second `SessionLocal()`-based session opened directly against the shared `:memory:`/`StaticPool` test engine, on the same underlying DBAPI connection the `client`/`transactional_db` fixtures are mid-transaction on, raises "cannot start a transaction within a transaction" (G4's documented failure mode). | `sweep_all_companies` takes an injectable `db_factory`; its test uses `db_file_based`/`file_company` (a wholly separate file-backed engine), never the shared connection. The lifespan smoke test uses `with TestClient(app):` with no `transactional_db` fixture active, so there is no competing open transaction. |
| Flipping `DISPATCH_RING_SIZE`'s default from 10 to 3 could silently change behavior relied on by a test that doesn't monkeypatch it. | Verified by grep: every ring-size-sensitive test in `test_dispatch.py`/`test_ws_dispatch.py` already monkeypatches `DISPATCH_RING_SIZE` explicitly; none depends on the bare default. |
| Two parallel notifier implementations (`offer_notifier.py` for workers, `realtime/hub.py` for the company channel) is some duplication. | Accepted explicitly (see Non-goals) — unifying them risks regressing the proven, heavily-tested worker path for a cosmetic gain. Both share the same `_sender`/`_receiver`/`_heartbeat` functions in `ws.py`, which is the part worth not duplicating. |
| `job.updated` on every `transition_job`/`accept_offer`/unfilled call adds a socket push on the hot path; if the company has no connected dispatcher it's a no-op lookup. | Same `_publish` catch-and-log pattern as the existing offer events (`dispatch.py:286-295`) — a push failure never fails the request. |

## Rollback

1. **Before merge:** abandon `feat/m4-dispatch`. Branch deletion is
   destructive and **requires human approval**.
2. **After merge:** revert the merge commit. No migration exists, so no
   `alembic downgrade` step is needed.
3. **Disable only the background sweep:** remove the lifespan's
   `asyncio.create_task` call in `app/main.py`; `POST /api/dispatch/sweep`
   and the lazy `expires_at` checks keep the system correct, just not
   self-healing without an explicit call.
4. **Disable only the company channel:** remove
   `app.include_router`'s effect for `/api/ws/company` (or don't register
   it); `_publish_job_updated` becomes a no-op against an empty registry.
5. **Revert idempotency/superseded behavior only:** these are the riskiest
   semantic changes (several existing tests change their asserted status
   code). If they must be rolled back independently of the rest, revert
   commits from tasks 1-4 specifically; nothing else in this PRP depends on
   them.
