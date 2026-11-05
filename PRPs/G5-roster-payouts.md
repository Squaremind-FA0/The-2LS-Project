# PRP: G5 — Roster and payouts

## Goal

Enable workers to log shift hours and companies to generate payroll. Workers can create overlapping-shift-guarded shift records; the dispatch system filters available workers by on-shift status; clock-in/out transitions capture work duration; and payroll aggregates TimeEntry records into PayRun/PayLine data that companies export as CSV for payment.

## Non-goals

- **Real money movement** — Stripe Connect or any payment-rail integration is out of scope; this PRP stops at CSV export for the payroll provider to ingest.
- **Shift conflict resolution UI** — no auto-rescheduling or conflict resolution workflows; 409 rejection is the policy.
- **Advanced shift modeling** — no recurring shifts, no break time, no job-to-shift matching; shifts are fixed windows and workers must be on shift at offer time.
- **Realtime shift updates** — no WebSocket/push for shift changes; polling-based UIs are acceptable.
- **Audit UI** — AuditLog writes are for compliance; reporting/querying the audit log is a follow-up.

## Context

### Existing patterns (must follow exactly)

**TenantScoped isolation** (`app/models/tenant.py:51-56`): Every new table (Shift, TimeEntry, PayRun, PayLine, AuditLog) must inherit `TenantScoped` so company_id is auto-stamped by the before_flush listener and ORM queries are auto-scoped. WorkerProfile (`app/models/worker_profile.py:13-26`) shows the pattern: `company_id` is inherited; `user_id` links to a worker; hourly_rate is `Numeric(10,2)` (dollars).

**Authentication/authorization** (`app/auth/deps.py:36-92`): All routes outside `PUBLIC_API_PATHS` depend on `require_role(...)` and read data through `get_tenant_db`. Every service takes a tenant-scoped session and returns or commits data within that session's company boundary. `get_current_user` resolves the bearer token to a User; `current_company_id` extracts the user's company; new payroll endpoints must restrict access by role (e.g., company_admin for PayRun, worker for TimeEntry and own PayLines).

**Job status state machine** (`app/models/job.py:19-26`, `app/services/dispatch.py:202-205`): JobStatus enum has `draft | offering | assigned | in_progress | completed | cancelled | unfilled`. MANUAL_TRANSITIONS dict enforces allowed transitions; G3 only wires `draft → {offering, cancelled}` and `offering → {cancelled}` — **assigned→in_progress→completed transitions are NOT implemented by G3 and must be added by G5** (see Task list).

**Conditional updates for race safety** (`app/services/dispatch.py:224-250`): Use `_conditional_update()` — an ORM UPDATE with `WHERE clause` — to protect against concurrent writes. Return rowcount; if 0, a race was won by another transaction.

**Bounding-box + haversine dispatch** (`app/services/dispatch.py:120-167`): `find_nearby_workers()` prefilters by bounding box + staleness, then ranks by haversine distance. Must accept workers with a Shift covering `now` (see Task list).

**API contract style** (`app/routers/jobs.py`, `app/routers/workers.py`): Use Pydantic schemas in `app/schemas/` (e.g., `JobCreateRequest`), FastAPI dependencies for auth/tenancy, and return 409 Conflict on race/constraint violation.

### Files to read before implementing

1. `app/models/tenant.py:51-56` — TenantScoped base class
2. `app/models/worker_profile.py` — hourly_rate field, user_id relationship
3. `app/models/job.py:19-26` — JobStatus enum (must extend, not rewrite)
4. `app/models/job_offer.py` — example ORM model with timestamps
5. `app/services/dispatch.py:202-250` — MANUAL_TRANSITIONS pattern, conditional updates, find_nearby_workers
6. `app/auth/deps.py:36-92` — get_current_user, require_role, get_tenant_db
7. `backend/migrations/versions/0003_jobs_dispatch.py` — migration pattern, alembic structure
8. `backend/app/main.py:22-30` — router registration block (collision hotspot with G4)

## Data model / API contracts

### New tables

```
Shift (inherits TenantScoped):
  id: int, primary_key
  worker_id: int, FK users.id (not nullable)
  starts_at: datetime (inclusive, naive UTC)
  ends_at: datetime (exclusive, naive UTC)
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()
  
  CONSTRAINT: unique per (company_id, worker_id, starts_at, ends_at) is NOT required.
  CONSTRAINT: half-open interval [starts_at, ends_at) — new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at raises 409.
  Overlap is checked per-worker only; same shift times across different workers in the same company is allowed.

TimeEntry (inherits TenantScoped):
  id: int, primary_key
  job_id: int, FK jobs.id
  worker_id: int, FK users.id (extracted from job.assigned_worker_id for clarity)
  started_at: datetime (clock-in time, naive UTC)
  ended_at: datetime | NULL (clock-out time, set on completed; nullable until clock-out)
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()

PayRun (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  period_start: date (inclusive, start of pay period)
  period_end: date (inclusive, end of pay period)
  status: str (draft | approved | paid) [Enum, default draft]
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()

PayLine (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  payrun_id: int, FK payrun.id
  worker_id: int, FK users.id
  minutes_worked: int (total minutes in this pay period for this worker)
  hourly_rate_cents: int (hourly_rate_dollars * 100, e.g. 2500 for $25.00)
  amount_cents: int (minutes_worked * hourly_rate_cents / 60, rounded round-half-up to nearest cent)
  created_at: datetime, server_default=now()
  updated_at: datetime, onupdate=now()
  
  CONSTRAINT: one PayLine per (payrun_id, worker_id) — unique(payrun_id, worker_id)

AuditLog (inherits TenantScoped):
  id: int, primary_key
  company_id: int, FK companies.id (from TenantScoped)
  entity_type: str (e.g., "payrun", "payline", "shift")
  entity_id: int
  action: str (e.g., "created", "updated", "status_changed")
  user_id: int | NULL, FK users.id (who triggered the action; NULL if system)
  old_values: JSON | NULL (previous state snapshot)
  new_values: JSON | NULL (new state snapshot)
  created_at: datetime, server_default=now()
```

### Index requirements

```
shifts(company_id, worker_id, starts_at, ends_at)     -- check overlaps, find shifts for worker
time_entries(company_id, job_id)                      -- find entries for a job
time_entries(company_id, worker_id)                   -- find entries for a worker
payruns(company_id, status)                           -- list by company/status
paylines(company_id, payrun_id, worker_id)            -- join payruns to paylines
audit_logs(company_id, entity_type, entity_id)        -- find audit trail for an entity
```

### Rounding rule (explicit for implementer)

**Numeric example:**
- Worker hourly_rate: $25.00 (stored as Decimal(10,2) = 25.00)
- Minutes worked in period: 1210 minutes (20 hours 10 minutes)
- Calculation: `1210 * 2500 / 60 = 50416.666... cents`
- Rounding: round-half-up to nearest cent = **50417 cents** ($504.17)
- Formula: `round(minutes_worked * hourly_rate_dollars * 100 / 60)` applied ONCE at the final amount, not per-minute.

Python: use `Decimal(...).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)` or equivalent; convert to int cents.

### API routes

```
POST /api/shifts
  dependencies: [Depends(require_role(Role.worker))]
  request: { starts_at: datetime, ends_at: datetime }
  response: 201 { id, worker_id, starts_at, ends_at, created_at }
           400 { detail: "starts_at must be before ends_at" }
           409 { detail: "Shift overlaps existing shift for this worker" }
  
  Error detail for 409: state the exact overlap condition (new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at).
  Overlap is checked per-worker only (same company, same worker).

GET /api/shifts
  dependencies: [Depends(require_role(Role.worker))]
  query: ?worker_id=<id>&limit=50&offset=0 (worker_id optional, defaults to current user)
  response: [{ id, worker_id, starts_at, ends_at, created_at }]
  
  Only workers can list their own shifts; company_admin/dispatcher get 403 unless further scoped (out of scope for G5).

POST /api/jobs/{job_id}/clock-in
  dependencies: [Depends(require_role(Role.worker))]
  request: {}
  response: 200 { id, job_id, worker_id, started_at, ended_at: null }
           404 { detail: "Job not found" }
           409 { detail: "Job not assigned to this worker" }
           409 { detail: "Job already has a TimeEntry" }
           409 { detail: "Worker is not on shift" }
  
  Preconditions:
  - job.assigned_worker_id == current_user.id
  - job.status == in_progress (clock-in transitions assigned → in_progress)
  - No existing TimeEntry for this job_id
  - A Shift exists for current_user.id covering now() (now <= shift.ends_at AND now >= shift.starts_at)

POST /api/jobs/{job_id}/clock-out
  dependencies: [Depends(require_role(Role.worker))]
  request: {}
  response: 200 { id, job_id, worker_id, started_at, ended_at }
           404 { detail: "Job or TimeEntry not found" }
           409 { detail: "Job not assigned to this worker" }
           409 { detail: "TimeEntry already ended" }
  
  Preconditions:
  - job.assigned_worker_id == current_user.id
  - Existing TimeEntry for job_id with ended_at == null
  - Clock-out transitions in_progress → completed

PATCH /api/jobs/{job_id}/transition
  dependencies: [Depends(require_role(Role.dispatcher))]
  request: { status: "in_progress" | "completed" | ... }
  response: 200 { ..., status: <new_status> }
           409 { detail: "Invalid status transition" }
  
  Existing route (G3); clockin/clockout are alternative worker-driven paths, not required to go through this endpoint.

GET /api/payruns
  dependencies: [Depends(require_role(Role.company_admin))]
  query: ?status=<draft|approved|paid>&limit=50&offset=0
  response: [{ id, company_id, period_start, period_end, status, created_at }]

POST /api/payruns
  dependencies: [Depends(require_role(Role.company_admin))]
  request: { period_start: date, period_end: date }
  response: 201 { id, company_id, period_start, period_end, status: "draft", created_at }

PATCH /api/payruns/{payrun_id}
  dependencies: [Depends(require_role(Role.company_admin))]
  request: { status: "approved" | "paid" }
  response: 200 { ..., status: <new_status>, updated_at }
           409 { detail: "Cannot transition from <current> to <target>" }
           409 { detail: "Cannot edit an approved/paid payrun" }
  
  State machine: draft → approved → paid (no reverse). On every transition, write an AuditLog row.
  If payrun is approved or paid, any PATCH request (including status change) returns 409.

GET /api/payruns/{payrun_id}/paylines
  dependencies: [Depends(require_role(Role.worker))]
  response: [{ id, payrun_id, worker_id, minutes_worked, hourly_rate_cents, amount_cents, created_at }]
  
  Worker sees only their own paylines (worker_id == current_user.id); other workers get 403.
  company_admin can see any paylines for their company (out of scope for G5 if not already general pattern).

GET /api/payruns/{payrun_id}/export?format=csv
  dependencies: [Depends(require_role(Role.company_admin))]
  response: text/csv, streaming
  
  Headers (in this exact order): worker_name,worker_email,period_start,period_end,minutes_worked,hourly_rate_cents,amount_cents
  
  Rows: one per PayLine in payrun_id, sorted by worker name (or id for determinism if names collide).
  Example line: "Alice Johnson,alice@example.com,2026-11-01,2026-11-30,1210,2500,50417"
  
  Payrun must be draft, approved, or paid; if not found, return 404.
```

## Task list

### Phase 0: Migrations and data models

1. `backend/migrations/versions/0004_shifts_payroll.py` — create migration (TEST: alembic upgrade head, alembic downgrade base, alembic upgrade head round-trip)
2. `backend/app/models/shift.py` — Shift model with TenantScoped, overlap constraint logic in service (not in model)
3. `backend/app/models/time_entry.py` — TimeEntry model with job_id, worker_id, started_at, ended_at
4. `backend/app/models/payrun.py` — PayRun model with company_id, period_start/end, status enum (draft | approved | paid)
5. `backend/app/models/payline.py` — PayLine model with minutes_worked and *_cents integer fields
6. `backend/app/models/audit_log.py` — AuditLog model with entity_type, action, user_id, JSON snapshots

### Phase 1: Shift API and overlap guard

7. `backend/tests/test_shifts.py` — failing test: POST /api/shifts with overlapping times returns 409 (half-open interval [starts_at, ends_at))
8. `backend/app/services/shifts.py` — create_shift(), check overlap per-worker (query Shifts where company_id == job.company_id, worker_id == worker_id, [starts_at, ends_at) overlaps with [new_starts_at, new_ends_at))
9. `backend/app/routers/shifts.py` — POST /api/shifts, GET /api/shifts with worker_id/limit/offset (worker auth required)
10. `backend/app/schemas/shifts.py` — ShiftCreate, ShiftResponse schemas

### Phase 2: Dispatch on-shift filter

11. `backend/tests/test_dispatch.py` — failing test: off-shift worker is never offered (find_nearby_workers excludes workers without a Shift covering now)
12. `backend/app/services/dispatch.py` — update find_nearby_workers() to add `.join(Shift, ...)` + `.where(Shift.starts_at <= now, Shift.ends_at > now)` (additive WHERE clause, preserve existing haversine/bounding-box logic)

### Phase 3: Job clock-in/out and state machine

13. `backend/tests/test_time_entries.py` — failing test: POST /api/jobs/{job_id}/clock-in requires job.status == assigned and transitions to in_progress
14. `backend/tests/test_time_entries.py` — failing test: POST /api/jobs/{job_id}/clock-out requires job.status == in_progress and transitions to completed
15. `backend/tests/test_time_entries.py` — failing test: clock-in without on-shift Shift returns 409
16. `backend/app/services/dispatch.py` — extend MANUAL_TRANSITIONS: add assigned → {in_progress, cancelled}, in_progress → {completed, cancelled}
17. `backend/app/services/time_entries.py` — create clock_in() (transition assigned → in_progress, create TimeEntry with started_at=now), clock_out() (set TimeEntry.ended_at, transition in_progress → completed)
18. `backend/app/routers/time_entries.py` — POST /api/jobs/{job_id}/clock-in, POST /api/jobs/{job_id}/clock-out
19. Decision in task 18 comment: clock-in/out extend transition_job() or add separate endpoints? Decision: separate clock_in/clock_out functions in time_entries service; endpoints are /jobs/{job_id}/clock-in and /clock-out (not under /transitions) for clarity.

### Phase 4: PayRun and PayLine aggregation

20. `backend/tests/test_payruns.py` — failing test: POST /api/payruns creates draft PayRun
21. `backend/tests/test_payruns.py` — failing test: list TimeEntries for a pay period, compute minutes and amount_cents with exact rounding rule (show worked example)
22. `backend/tests/test_payruns.py` — failing test: PATCH /api/payruns/{id} draft → approved writes AuditLog
23. `backend/tests/test_payruns.py` — failing test: PATCH /api/payruns/{id} approved → paid writes AuditLog
24. `backend/app/services/payruns.py` — create_payrun(), list_payruns(), compute_paylines(payrun_id) [query TimeEntries, join WorkerProfile, compute minutes and amount_cents], transition_payrun(payrun_id, target_status), write audit logs
25. `backend/app/routers/payruns.py` — GET /api/payruns, POST /api/payruns, PATCH /api/payruns/{id}
26. `backend/app/schemas/payruns.py` — PayRunCreate, PayRunResponse, PayLineResponse

### Phase 5: PayLine immutability and worker visibility

27. `backend/tests/test_payruns.py` — failing test: PATCH /api/payruns/{id} after approved → 409
28. `backend/tests/test_payruns.py` — failing test: GET /api/payruns/{id}/paylines as worker returns only own paylines (worker_id == current_user.id); other workers return 403
29. `backend/app/services/payruns.py` — guard in PATCH: if payrun.status in (approved, paid), return 409
30. `backend/app/routers/payruns.py` — GET /api/payruns/{id}/paylines with worker scoping via require_role(Role.worker) + current_user.id filter

### Phase 6: CSV export

31. `backend/tests/test_payruns.py` — failing test: GET /api/payruns/{id}/export?format=csv returns CSV with exact headers (worker_name,worker_email,period_start,period_end,minutes_worked,hourly_rate_cents,amount_cents) and one row per payline
32. `backend/app/routers/payruns.py` — GET /api/payruns/{id}/export with StreamingResponse, csv.writer, company_admin auth, exact header order
33. `backend/app/schemas/payruns.py` — PayLineCSVRow (helper for export serialization if needed)

### Phase 7: Integration and regressions

34. `backend/app/main.py` — add `app.include_router(shifts.router)`, `app.include_router(payruns.router)`, `app.include_router(time_entries.router)` in the router block (watch for G4 collision: if G4 has also added routers, rebasing will hit this block)
35. `backend/tests/test_route_audit.py` — verify all new /api/* routes (except PUBLIC_API_PATHS) have require_role and use get_tenant_db (auto-verified if routers follow pattern)
36. Run validation gates (see Validation gates section)

## Validation gates

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head      # from clean DB
cd backend && alembic downgrade base    # prove round-trip
cd backend && alembic upgrade head
cd frontend && npm run build
```

These are the project standard (from CLAUDE.md); G5 does not add new gates.

## Acceptance criteria

1. **Shift overlap guard** — POST /api/shifts rejects overlapping times with 409, citing exact half-open interval predicate. Overlap checked per-worker only (same company, same worker).
2. **Off-shift dispatch** — find_nearby_workers() excludes workers without a Shift covering `now` (where `shift.starts_at <= now < shift.ends_at`); named test proves off-shift worker is never matched even if nearest.
3. **Pay calculation** — PayLine.amount_cents = round(minutes_worked * hourly_rate_cents / 60), rounded round-half-up to nearest cent, applied once at the final amount. Example in context verifies $504.17 from 1210 minutes at $25/hour.
4. **Approved/paid immutability** — PATCH /api/payruns/{id} after status is approved or paid returns 409. Every status transition writes an AuditLog row (action, old_values, new_values).
5. **Worker pay visibility** — GET /api/payruns/{id}/paylines as worker returns only own PayLines (worker_id == current_user.id); other workers get 403.
6. **CSV export** — GET /api/payruns/{id}/export?format=csv streams text/csv with headers (worker_name,worker_email,period_start,period_end,minutes_worked,hourly_rate_cents,amount_cents) and correct values.

## Risks

- **G4 / main.py collision** — G4 (prp/G4, branch off same fb5de96) also adds routers to `app/main.py` lines 22-30. Whichever merges second must rebase and pick both sets of `app.include_router()` calls. Mitigation: `/prp-pr` is aware of this; plan for manual rebase if needed.
- **Migration head drift** — G4 has no migration; G5 creates 0004. If G4 merges first, 0004's down_revision must still point to 0003 (no change). State this explicitly in migration comment to avoid confusion.
- **Concurrent clock-in/out** — two workers clock-in to same job simultaneously. Mitigation: conditional update in transition_job (already blocks dual-accept); only one can set job.status to in_progress.
- **Rounding errors at scale** — if payroll system expects exact cents, rounding differences compound. Mitigation: frozen rounding rule in docstring + unit tests with worked examples. No float arithmetic; use Decimal throughout.
- **Shift staleness** — a worker clocks in, then shift expires while still working. Mitigation: clock-in checks shift.ends_at > now; clock-out does not re-check (work is already logged). Out-of-shift clock-out is allowed (no re-check); TimeEntry will be correct if the worker was on-shift at clock-in.

## Rollback

- Delete branch `prp/G5`.
- Revert migration 0004: `alembic downgrade 0003`.
- Remove files: `backend/app/models/{shift,time_entry,payrun,payline,audit_log}.py`, `backend/app/services/shifts.py`, `backend/app/services/payruns.py`, `backend/app/services/time_entries.py`, `backend/app/routers/{shifts,payruns,time_entries}.py`, `backend/app/schemas/{shifts,payruns}.py`.
- Remove `app.include_router(shifts.router)`, `app.include_router(payruns.router)`, `app.include_router(time_entries.router)` from `backend/app/main.py:26-30`.
- Remove test files: `backend/tests/test_shifts.py`, `backend/tests/test_time_entries.py`, `backend/tests/test_payruns.py`.

---

## Cross-plan coordination (G4 collision hotspot)

**G4 (prp/G4, already planned, off same main tip fb5de96) touches `backend/app/main.py` to add `app.include_router(ws.router)` (WebSocket router for realtime offers) and one import statement. G4 explicitly asserts zero diff in migrations.**

**G5 will also modify `backend/app/main.py` (add 3 routers: shifts, payruns, time_entries) and will create a new migration 0004.**

**Collision risk**: Both branches modify lines 22-30 of main.py (the router block). Mitigation:
- If G4 merges to main first, `prp/G5` must rebase on updated main and resolve the merge conflict by picking both router sets (no override).
- If G5 merges first, G4 must rebase and include G5's routers in the router block.
- Migration order is safe: G4 has no migration, G5 creates 0004 with `down_revision = "0003"`, so whichever merges second sees the correct migration chain.

**Plan for `/prp-pr`**: If a rebase conflict occurs in main.py, the merge process should stop and ask for manual resolution (not auto-pick one side).

---

**Authored by**: Farhan Rashid
**Date**: 2026-11-05
