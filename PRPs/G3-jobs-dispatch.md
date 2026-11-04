# PRP: G3 — Jobs and dispatch engine

## Goal

Build a job matching and dispatching system where companies create jobs (location, trade), workers report their location, and the system automatically offers the nearest available workers in order. Workers accept or decline offers within a TTL; expiry triggers re-offering to the next ring of workers. Offers cannot be double-accepted under concurrent writes.

## Non-goals

- **Realtime transport / WebSocket push** — workers will learn about offers via polling or external push (G4+); this PRP does not handle realtime updates.
- **Customer-facing booking UI** — this PRP is API-only; UI integration is out of scope.
- **Advanced matching** — no scoring, no preference lists, no rejection reasons; strictly radius + trade + freshness.

## Context

The code follows a three-layer pattern: models (SQLAlchemy ORM with TenantScoped isolation), services (business logic), and routers (HTTP contracts). Every database table must inherit `TenantScoped` to enforce company isolation at the ORM layer (app/tenancy.py:51-56). Every route outside PUBLIC_API_PATHS must depend on `require_role(...)` and read tenant data through `get_tenant_db` (app/auth/deps.py:61-92). Services take a tenant-scoped session and perform domain logic (app/services/workers.py:19-54); routers translate HTTP to service calls (app/routers/workers.py:19-45).

The Worker model is split across two tables: `users` (User, app/models/user.py) holds auth; `worker_profiles` (WorkerProfile, app/models/worker_profile.py) holds trades and hourly_rate and inherits TenantScoped (line 13). WorkerProfile is a concrete example of the tenancy pattern: company_id is stamped by the before_flush listener, and every ORM read is filtered to the session's company automatically (app/tenancy.py:109-132).

**SQLite concurrency gap**: app/db.py's `_build_engine` (lines 12-36) initializes SQLite with `check_same_thread=False` and SAVEPOINT support for tests, but omits `busy_timeout` and WAL journal mode. Under concurrent writers (e.g., 10+ parallel POST requests), SQLite raises uncaught `sqlite3.OperationalError("database is locked")`, which FastAPI surfaces as 500 instead of the 409 that a race-safe accept/decline flow requires. This gap blocks the acceptance criteria (test 3 below) and must be fixed early in the task list.

## Data model / API contracts

### New tables

```
Job (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  trade: str (e.g., "plumber", "electrician")
  address: str (street address)
  lat: float (latitude, required)
  lng: float (longitude, required)
  scheduled_for: datetime (when the job starts)
  status: str (draft | offering | assigned | in_progress | completed | cancelled | unfilled)
  assigned_worker_id: int, FK users.id, nullable (links job to accepted worker; set on first accept)
  offer_round: int, default=0 (tracks re-offer rounds for bounded sweeper)
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()

WorkerLocation (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  worker_id: int, FK users.id, unique per (company_id, worker_id) [compound unique]
  lat: float (latitude, current)
  lng: float (longitude, current)
  updated_at: datetime, onupdate=now()

JobOffer (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  job_id: int, FK jobs.id
  worker_id: int, FK users.id
  status: str (pending | accepted | declined | expired)
  expires_at: datetime (TTL, default 30s from creation)
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()
```

### Indexes (performance critical for dispatch matching)

```
jobs(company_id, status)         -- filter by company and status (offering, assigned, etc.)
jobs(company_id, scheduled_for)  -- for future scheduling queries
worker_locations(worker_id)      -- lookup by worker (for location refresh)
worker_locations(lat, lng)       -- **spatial query **; see note below
job_offers(job_id)               -- find all offers for a job (count accepted)
job_offers(worker_id, status)    -- find worker's pending offers
job_offers(status, expires_at)   -- for sweeper to find expired offers efficiently
UNIQUE job_offers(job_id, worker_id) -- prevent duplicate offers to same worker for same job
```

**Spatial note**: SQLite has no spatial index (no R-tree unless compiled in). The dispatch service will use a bounding-box prefilter (fast, indexed, coarse) followed by haversine distance in application code (precise, fine-grained sort). The bounding-box query relies on simple `lat BETWEEN ? AND ?` and `lng BETWEEN ? AND ?` on indexed columns.

### Routes / API contracts

```
POST /api/jobs
  dependencies: [Depends(require_role(Role.dispatcher))]
  request: { trade: str, address: str, lat: float, lng: float, scheduled_for: datetime }
  response: { id, trade, address, lat, lng, scheduled_for, status: "draft", created_at }

GET /api/jobs
  dependencies: [Depends(require_role(Role.dispatcher, Role.company_admin))]
  query params: ?status=<status>&limit=50&offset=0
  response: [{ id, trade, address, lat, lng, scheduled_for, status, created_at }]

PATCH /api/jobs/{job_id}
  dependencies: [Depends(require_role(Role.dispatcher))]
  request: { status: "offering" | "cancelled" | "draft" }
  response: { ..., status: <new_status> }

PUT /api/workers/me/location
  dependencies: [Depends(require_role(Role.worker))]
  request: { lat: float, lng: float }
  response: { worker_id, lat, lng, updated_at }

POST /api/offers/{offer_id}/accept
  dependencies: [Depends(require_role(Role.worker))]
  response: 200 { id, job_id, worker_id, status: "accepted", ... } on success
           404 { detail: "Offer not found" } if offer does not exist or belongs to another worker
           409 { detail: "Job already assigned" } if another worker accepted the job first
           410 { detail: "Offer expired" } if past expires_at

POST /api/offers/{offer_id}/decline
  dependencies: [Depends(require_role(Role.worker))]
  response: 200 { id, job_id, worker_id, status: "declined", ... }
```

## Task list

### Phase 1: Foundation — Database schema + SQLite concurrency fix

1. **tests/test_dispatch.py** — write failing tests for the SQLite concurrency fix, using a file-based SQLite fixture (not StaticPool):
   - Create a new fixture `db_file_based` that uses `sqlite:////tmp/<random>.db` with WAL and real commits (not SAVEPOINT), yielding sessions that see parallel writes.
   - Test "SQLite: concurrent writes under load" — fire ~10 parallel POST /api/workers requests (worker creation writes); verify none raise 500 (i.e., no uncaught OperationalError), and all return 200/409.
   - Test "Location update race: no missed updates" — ~5 concurrent POST /api/workers requests creating workers with the same email; verify exactly one succeeds (200) and the rest fail (409 conflict), and the database reflects only one worker.

2. **backend/app/db.py** — fix SQLite concurrency gap:
   - Add `timeout=10` to connect_args on line 17 (allows SQLite to wait for lock release).
   - Add a connect listener (after line 30) to emit `PRAGMA journal_mode=WAL` (write-ahead logging reduces contention).
   - Consider wrapping write operations with `BEGIN IMMEDIATE` or catching `sqlite3.OperationalError("database is locked")` in accept/decline handlers and returning 409.
   - Verify the tests from (1) pass (all requests return 200/409, never 500).

3. **backend/migrations/versions/0003_jobs_dispatch.py** — create migration:
   - Create tables: jobs, worker_locations, job_offers (all inherit TenantScoped; see schema above, including Job.assigned_worker_id FK and Job.offer_round).
   - Create indexes: jobs(company_id, status), jobs(company_id, scheduled_for), worker_locations(worker_id), worker_locations(lat, lng), job_offers(job_id), job_offers(worker_id, status), job_offers(status, expires_at).
   - Create UNIQUE constraint: job_offers(job_id, worker_id).
   - Verify alembic upgrade head && alembic downgrade 0002_auth_tenancy && alembic upgrade head round-trips cleanly.

### Phase 2: Models + schemas

4. **backend/app/models/job.py** — define Job (TenantScoped, fields above).

5. **backend/app/models/worker_location.py** — define WorkerLocation (TenantScoped, unique compound on company_id + worker_id).

6. **backend/app/models/job_offer.py** — define JobOffer (TenantScoped, includes expires_at).

7. **backend/app/models/__init__.py** — export Job, WorkerLocation, JobOffer.

8. **backend/app/schemas/jobs.py** (new file) — define Pydantic schemas:
   - JobCreateRequest, JobOut, JobOfferOut, WorkerLocationOut.

### Phase 3: Matching service

9. **tests/test_dispatch.py** — write failing tests for distance matching:
   - Test "Radius boundary: 9.9km matched, 10.1km not" — create two workers at 9.9km and 10.1km from a job (same company, same trade, fresh location); verify only the 9.9km worker is returned by the matcher.
   - Test "Cross-company isolation" — create worker in company B at 1km from job in company A; verify matcher returns no results.
   - Test "Trade filtering" — create worker with trade X; create job with trade Y; verify no match.
   - Test "Stale location excluded" — set location.updated_at to 1 hour ago; verify not returned (freshness cutoff TBD per config, suggest 5 minutes).

10. **backend/app/services/dispatch.py** (new file) — implement matching service:
    - `find_nearby_workers(db: Session, job: Job, max_distance_km: float = 10.0, max_staleness_minutes: float = 5.0) -> list[User]` — returns active workers in job's company, with trade matching job's trade, location fresher than max_staleness_minutes (5 min default), and distance ≤ max_distance_km, sorted by distance ascending.
    - Exclude inactive workers (User.is_active == True, WorkerProfile.active == True) and workers already offered this job.
    - Use SQLAlchemy to join Job, WorkerLocation, WorkerProfile, User.
    - Prefilter with bounding-box `lat BETWEEN ? AND ?` and `lng BETWEEN ? AND ?` (computed from max_distance_km and job's center).
    - Calculate haversine distance in Python per returned row, verify ≤ max_distance_km, sort.

### Phase 4: Offer flow

11. **tests/test_dispatch.py** — write failing test for double-accept race across a ring:
    - Test "Concurrent ring double-accept: one wins, one gets 409" — create a job, dispatch offers to workers 1 and 2 (both pending); fire 2 parallel POST /api/offers/{offer_for_worker_1}/accept and POST /api/offers/{offer_for_worker_2}/accept simultaneously (different offers, different workers); verify one returns 200 and one returns 409 (never both 200, never 500). Verify job.status='assigned', job.assigned_worker_id is set to the winner's id, and the loser's offer is marked 'expired'. Verify that the job can no longer be offered to other workers (it is assigned).

12. **backend/app/routers/jobs.py** (new file) — define job CRUD routes:
    - `POST /api/jobs` (dispatcher only) — create and insert a Job in draft status; return JobOut.
    - `GET /api/jobs` (dispatcher or admin) — list jobs in company, filter by ?status=<status>, paginate.
    - `PATCH /api/jobs/{job_id}` (dispatcher only) — transition status (draft → offering, offering → cancelled, etc.); validate state machine (see state-transition table below); on transition to "offering", call dispatch service to create offers.

13. **backend/app/routers/dispatch.py** (new file) — define offer + location routes:
    - `PUT /api/workers/me/location` (worker only) — upsert WorkerLocation for current user; return WorkerLocationOut.
    - `POST /api/offers/{offer_id}/accept` (worker only) — conditionally update offer.status from pending → accepted and job.status → assigned using a race-safe UPDATE with WHERE clause (see race safety note below).
    - `POST /api/offers/{offer_id}/decline` (worker only) — update offer.status pending → declined; trigger sweeper to offer to next worker.

14. **Race-safety in accept**: Use SQLAlchemy's `update()` with explicit WHERE conditions on BOTH JobOffer and Job, check `result.rowcount`:
    ```python
    # Step 1: Check ownership + expiry (not in WHERE to avoid IDOR on nonexistent offers)
    offer = db.query(JobOffer).filter(JobOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if offer.worker_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)  # IDOR: hide existence
    if offer.expires_at < func.now():
        raise HTTPException(status.HTTP_410_GONE, detail="Offer expired")
    
    # Step 2: Atomic job-level conditional UPDATE (race guard)
    job_stmt = (
        update(Job)
        .where(Job.id == offer.job_id)
        .where(Job.status == "offering")
        .where(Job.assigned_worker_id == None)
        .values(status="assigned", assigned_worker_id=current_user.id)
    )
    job_result = db.execute(job_stmt)
    
    # Step 3: If job update succeeded, mark offer as accepted and expire other offers
    if job_result.rowcount == 1:
        offer_stmt = (
            update(JobOffer)
            .where(JobOffer.id == offer_id)
            .values(status="accepted")
        )
        db.execute(offer_stmt)
        # Expire other pending offers for this job
        expire_stmt = (
            update(JobOffer)
            .where(JobOffer.job_id == offer.job_id)
            .where(JobOffer.id != offer_id)
            .where(JobOffer.status == "pending")
            .values(status="expired")
        )
        db.execute(expire_stmt)
        db.commit()
        return offer  # 200 OK
    else:
        # Another worker already accepted this job
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Job already assigned")
    ```
    This ensures exactly one worker sets assigned_worker_id; all others see 409.

### Phase 5: Expiry + sweeper

15. **tests/test_dispatch.py** — write failing tests for expiry and sweeper:
    - Test "Expired offer cannot be accepted" — create offer with expires_at in the past; attempt POST /api/offers/{id}/accept; verify 410 Offer expired (matches API contract on line 97).
    - Test "Decline triggers next offer" — create job in company with 3 workers at increasing distance; dispatch offers (ring size=3); decline the nearest worker's offer; verify a new offer is created for the second-nearest worker and job status is still "offering".

16. **backend/app/services/dispatch.py** — add sweeper helpers:
    - `check_offer_expiry(db: Session, company_id: int) -> list[int]` — for the given company, mark all JobOffers past expires_at as expired; return the list of job_ids that now have no pending offers (used to trigger re-offer).
    - `re_offer_job(db: Session, job: Job, ring_size: int = 10, max_rounds: int = 5) -> bool` — for a job with no pending offers, find the next ring of workers (workers not yet offered in this job at any round), create new offers, increment job.offer_round. If offer_round >= max_rounds or no new workers found, mark job "unfilled" and return False. Return True if offers were created.

17. **backend/app/routers/dispatch.py** — add a bounded sweeper task:
    - Define endpoint `POST /api/dispatch/sweep` (admin only, scoped to current company via get_tenant_db).
    - Call check_offer_expiry(db, company_id) to mark all expired offers.
    - For each job returned by check_offer_expiry, call re_offer_job(db, job) to create next ring of offers.
    - re_offer_job respects max_rounds: if job.offer_round >= max_rounds or no new workers found, mark job "unfilled".
    - Return summary: { expired_count, rejobs_count, unfilled_count }.
    - This is **NOT** a background daemon — it's a bounded, explicit step triggered by a cron or admin request (to ensure it's not an unbounded retry loop).

### Phase 6: Router integration + validation

18. **backend/app/main.py** — register the new routers (in the include_router block around line 26-28):
    - `app.include_router(jobs_router)` (after workers_router).
    - `app.include_router(dispatch_router)` (after jobs_router).

19. **tests/test_route_audit.py** (existing) — verify new routes:
    - Ensure POST/GET/PATCH /api/jobs/* require role.
    - Ensure PUT /api/workers/me/location requires role.
    - Ensure POST /api/offers/* require role.
    - The existing route_audit test enforces these (already extends on all /api routes), so this is a verification step, not new code.

20. **tests/test_dispatch.py** — integration tests:
    - "E2E job dispatch flow": create company, 2 workers at different distances, a job; verify offers created to both; worker 1 accepts (200); worker 2 attempts accept (409); verify job status='assigned', job.assigned_worker_id=worker_1.id, worker_2's offer status='expired'.
    - All 5 acceptance criteria tests (see Acceptance Criteria section) — each maps to one or more tasks above.

**Note on task ordering**: Tasks are listed in dependency order. Task 3 (migration) comes before Tasks 4-6 (models) so that alembic autogenerate can generate from the models. If writing the migration by hand, models can be created first or in parallel.

## Validation gates

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))') DATABASE_URL=sqlite:////tmp/test.db alembic upgrade head
cd backend && JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))') DATABASE_URL=sqlite:////tmp/test.db alembic downgrade 0002_auth_tenancy
cd backend && JWT_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))') DATABASE_URL=sqlite:////tmp/test.db alembic upgrade head
cd frontend && npm run build
cd frontend && npm test
```

These are based on CLAUDE.md and represent the project-wide validation standard. The alembic gates require JWT_SECRET (32+ bytes) and a scratch DATABASE_URL. Run these after every task.

## Acceptance criteria

1. ✅ **SQLite concurrency fix in place**: app/db.py includes `timeout=10` in connect_args and a connect listener that emits `PRAGMA journal_mode=WAL`. The regression test (tests/test_dispatch.py) fires 10+ concurrent writes and verifies 0 raise 500.

2. ✅ **Job model created**: Job table (TenantScoped) with fields trade, address, lat, lng, scheduled_for, status (state machine enforced at router level), created_at, updated_at. Alembic migration round-trips cleanly.

3. ✅ **WorkerLocation table created**: new table (not a column on WorkerProfile), TenantScoped, with compound unique on company_id + worker_id. PUT /api/workers/me/location upserts and returns the new location.

4. ✅ **Matching service**: services/dispatch.py implements find_nearby_workers with bounding-box prefilter and haversine sort. Radius boundary test passes: 9.9km workers matched, 10.1km excluded.

5. ✅ **Cross-company isolation**: dispatch matcher never returns workers from a different company, even if geographically closer. Test passes.

6. ✅ **Double-accept race safety**: concurrent POST /api/offers/{id}/accept on the same offer yields exactly one 200 and one 409 (never both 200, never 500). Verified by regression test firing 2+ parallel requests simultaneously.

7. ✅ **Offer state machine**: JobOffer status transitions pending → accepted | declined | expired. Accept across a ring of offers is race-safe: exactly one worker wins (200) and others get 409 with the loser's offer marked expired. Decline marks as declined and triggers next offer.

8. ✅ **Expiry enforcement**: offers past expires_at cannot be accepted, returning 410 (not 409). Sweeper marks them expired and re-offers to next ring.

9. ✅ **Bounded sweeper**: re_offer_job stops after max_rounds (default 5); if no workers left, job marked "unfilled". No unbounded retry loop. The sweeper endpoint is explicit (POST /api/dispatch/sweep) and not a background daemon.

10. ✅ **State-transition table**: valid transitions are documented below; router enforces them (reject invalid PATCH requests with 400).

11. ✅ **All 5 named tests pass**: radius boundary, cross-company isolation, concurrent double-accept, expiry, decline-triggers-next-offer.

12. ✅ **Validation gates pass**: pytest, ruff, alembic round-trip, npm build all succeed.

## State-transition table

| From | To | Trigger | Valid |
|------|----|---------| ------|
| draft | offering | PATCH status=offering (dispatcher) | Yes |
| draft | cancelled | PATCH status=cancelled (dispatcher) | Yes |
| offering | assigned | accept offer (automatic) | Yes |
| offering | unfilled | sweeper after max_rounds | Yes |
| offering | cancelled | PATCH status=cancelled (dispatcher) | Yes |
| assigned | in_progress | (future: G4+; not in scope) | — |
| assigned | cancelled | (future: cancellation flow) | — |
| in_progress | completed | (future: completion webhook or manual) | — |
| completed | — | (terminal) | — |
| unfilled | — | (terminal) | — |
| cancelled | — | (terminal) | — |

## Risks

1. **Spatial performance**: Without an R-tree index, bounding-box queries on 100k+ locations may be slow. Mitigation: keep location data pruned (e.g., delete stale entries older than 24h in a background task). If perf becomes a bottleneck, post-G3 work could add a spatial library (PostGIS if Postgres is available, or an in-process spatial cache).

2. **Sweeper unboundedness and re-offer semantics**: re_offer_job only fires when a job has no pending offers (all declined, expired, or one was accepted and others marked expired). Re-offering happens only by calling re_offer_job explicitly, never automatically. If a re_offer fails (e.g., database error), the job stays in "offering" status and the sweeper must be called again. Mitigation: explicit max_rounds limit, explicit error handling (log and skip the job if an error occurs mid-sweep), and no automatic retry — only explicit endpoint calls.

3. **Clock skew**: expires_at comparison relies on server clock. If clocks skew between app replicas, offers may expire early or late. Mitigation: all servers use NTP; compare against server time in the app, not client time.

4. **Race condition in double-accept across a ring**: two workers accepting distinct pending offers for the same job simultaneously must have exactly one winner. Mitigation: conditional UPDATE on Job with WHERE status='offering' AND assigned_worker_id=NULL, check rowcount. Test verifies this with parallel requests on different offers. SQLite's default isolation level (SERIALIZABLE) and Postgres READ COMMITTED both ensure the WHERE clause is re-checked atomically on the locked row, so no explicit SERIALIZABLE isolation is required.

## Rollback

1. **Branch deletion**: `git branch -D prp/G3` (if not yet merged). **Requires human approval** (destructive operation).
2. **Migration revert**: `alembic downgrade 0002_auth_tenancy` (rolls back only the G3 jobs/worker_locations/job_offers tables, preserving G1/G2 tables).
3. **Route deregistration**: remove `app.include_router(jobs_router)` and `app.include_router(dispatch_router)` from backend/app/main.py.
4. **Database state**: if jobs were created in production before rollback, they will be in orphaned tables; recommend a pre-rollback purge query to delete all job/offer rows before downgrading, or leave them (they won't be queried post-rollback).
