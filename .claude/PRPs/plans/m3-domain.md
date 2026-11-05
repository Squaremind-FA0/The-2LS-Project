# Plan: M3 Core domain — worker profiles, Job state machine, job CRUD

## Summary

`main` @ `f2441df` already has `Job`, `WorkerProfile`, `WorkerLocation`, a job
create/list/PATCH API and clock-in/out (PRPs G3 and G5). M3 reshapes those to the
brief rather than building a parallel set: new columns, one audited state machine
that every status change goes through, optimistic concurrency, full job CRUD and
the worker-side `/api/me/*` routes.

## User Story

As a dispatcher or company admin, I want to manage jobs through an explicit,
audited lifecycle with safe concurrent edits, and as a worker I want to control my
availability and work my own jobs, so that no status change is unexplained and no
edit silently overwrites someone else's.

## Metadata

- **Complexity**: Large (cross-cutting; one migration; about 25 files)
- **Source PRD**: N/A (mission brief)
- **Branch**: `feat/m3-domain` off `main`

## UX Design

Backend-led. `AdminJobsPage` changes only where the contract changes: a Title
field, the `dispatching` status, `version` sent on every PATCH, and admins can
create and transition jobs as well as dispatchers.

## Requirement → decision

| Brief | Decision |
|---|---|
| `WorkerProfile.user_id` PK/FK | **Implemented.** Surrogate `id` dropped. Nothing has an FK to it. |
| `hourly_rate_cents` | **Implemented** as the stored column. `hourly_rate` stays as a Decimal Python property so the public API, the frontend and `feat/m2-auth` keep working. Pay runs read cents directly. |
| `is_available` | **Implemented**, separate from `active` (admin-controlled employment flag). `find_nearby_workers` must honour it. |
| `last_lat/last_lng/last_seen_at` | **Implemented**, backfilled from `worker_locations`. `upsert_location` is the only writer and updates both in one transaction. Matching keeps reading `worker_locations` (it owns the G3 freshness query); removing that table is a follow-up. |
| `Job` new columns | **Implemented.** `created_by` nullable (legacy rows have no author, user deletion SETs NULL); `title` backfilled from `trade`. |
| status `dispatching` | **Implemented** by renaming `offering`, with a data migration both ways. `offer_round`, `JobOffer` and "offer" wording stay: they name offers, not the job status. |
| `app/domain/job_state.py` + `JobEvent` | **Implemented.** Sole writer of `Job.status`. |
| M2 `scoped()` helper | **Not added: no such helper exists, by design.** `app/tenancy.py` scopes every ORM statement fail-closed and `tests/test_route_audit.py` enforces `require_role` + `get_tenant_db`. An opt-in helper would imply unscoped is possible. All new routes use `get_tenant_db`. |
| `/api/jobs` CRUD and cancel, dispatcher/admin | **Implemented.** Widens POST/PATCH from dispatcher-only to both roles, per the brief's assumption. |
| `/api/me/location`, `/api/me/availability` | **Implemented** as worker-only PATCH. Existing `PUT /api/workers/me/location` stays (used by `useShareLocation`) and shares the same service. |
| `/api/me/jobs` start/complete | **Implemented** as thin calls into `time_entries.clock_in/clock_out`. A second path that skipped the TimeEntry would produce completed jobs with no paid hours. So the on-shift rule applies. |
| Version check → 409 | **Implemented** as conditional UPDATE on `version`, same style as the existing race guards. `version_id_col` is not used: it does not see Core `update()`. |

## Patterns to Mirror

- Conditional UPDATE + rowcount: `services/dispatch.py` `_conditional_update`, `transition_job`.
- Service errors → HTTP: `routers/dispatch.py` `_STATUS_FOR`, `raise_http`.
- Stale-read guard tests: `tests/test_roster.py:442` (read, then change the row underneath, then call the service). Per project lesson, concurrent-request tests never reach rowcount-0 branches on SQLite, so guards need deterministic stale-read tests.
- Test data: `tests/factories.py`; `two_companies`, `client`, `transactional_db` in `tests/conftest.py`.
- Commit-then-notify in `dispatch.py`: no change to event publishing.

## Design

### `app/domain/job_state.py`

- `TRANSITIONS: dict[JobStatus, frozenset[JobStatus]]`; legal moves are
  draft→dispatching, dispatching→assigned, assigned→in_progress,
  in_progress→completed, dispatching→unfilled, and every non-terminal→cancelled.
  completed, cancelled and unfilled are terminal.
- `transition(job, to, actor, *, expected_version=None, **values) -> JobEvent`
  - `IllegalTransition` when the table forbids it (nothing written).
  - Conditional `UPDATE ... WHERE id AND status = <read status>` plus
    `AND version = expected_version` when given; sets `status`, `version + 1`, `values`.
  - rowcount 0 → `StaleJob`. Adds the `JobEvent`, flushes, refreshes `job`. Never commits.
- `actor` is a user id, or `None` for system moves (sweep → unfilled).
- Internal callers (accept, clock-in/out, sweep) guard on status, not version: an
  unrelated title edit must not fail a worker's accept. Dispatcher-facing writes
  pass `expected_version`.
- `MANUAL_TRANSITIONS` in `dispatch.py` stays as the narrower set a dispatcher may
  request by PATCH: dispatching→assigned stays accept-only, dispatching→unfilled sweep-only.

### API

| Route | Roles | Notes |
|---|---|---|
| `POST /api/jobs` | dispatcher, admin | `title` required; `description`, `scheduled_for` optional; sets `created_by` |
| `GET /api/jobs`, `GET /api/jobs/{id}` | dispatcher, admin | |
| `PATCH /api/jobs/{id}` | dispatcher, admin | `version` required; either `status` or detail fields, not both (422) |
| `POST /api/jobs/{id}/cancel` | dispatcher, admin | body `{version}` |
| `DELETE /api/jobs/{id}` | dispatcher, admin | `draft` only (409 otherwise): `time_entries.job_id` is RESTRICT, and a draft has no history worth losing. 204 |
| `GET /api/me/jobs` | worker | own assigned jobs |
| `POST /api/me/jobs/{id}/start`, `/complete` | worker | another worker's job is 404 (no oracle, as `_own_offer`) |
| `PATCH /api/me/location`, `/api/me/availability` | worker | |

Status codes: stale version 409, illegal transition 400 (existing mapping), detail
edit on a terminal job 409, other company's job 404.

## Files to Change

- CREATE `backend/app/domain/__init__.py`, `backend/app/domain/job_state.py`, `backend/app/models/job_event.py`, `backend/migrations/versions/0005_core_domain.py`, `backend/tests/test_job_state.py`, `backend/tests/test_jobs_api.py`
- UPDATE models `job.py`, `worker_profile.py`, `__init__.py`; schemas `jobs.py`, `users.py`; services `dispatch.py`, `time_entries.py`, `payruns.py`, `workers.py`; routers `jobs.py`, `me.py`; existing tests for the rename, required `title`/`version`, wider roles
- UPDATE frontend `AdminJobsPage.jsx`, `api/client.js` and their tests

## NOT Building

- Customer self-booking; removing `worker_locations`; a `scoped()` helper; a JobEvent read API; frontend pages for availability or job editing.
- Merging `feat/m2-auth` or `test/g6-coverage-gaps` (flagged in the report).

## Tasks (test-first, one commit each)

1. **Schema + rename**: models, `JobEvent`, migration 0005, rename `offering`, cents column, round-trip migration test with real rows. VALIDATE `pytest -q`, `alembic upgrade head`/`downgrade base`/`upgrade head`, `alembic check`.
2. **State machine**: `test_job_state.py` RED, then `job_state.py`, then route `dispatch.py` + `time_entries.py` through `transition()`.
3. **Availability and location**: `is_available` honoured by matching; write-through; `/api/me/location`, `/api/me/availability`.
4. **Jobs API**: `test_jobs_api.py` RED, then CRUD, cancel, version checks, `/api/me/jobs`; route audit updates.
5. **Frontend contract**: Title field, `dispatching`, `version` on PATCH, admin can create, null `scheduled_for` rendering.

## Validation Commands

```bash
cd backend && pytest -q && ruff check .
cd backend && DATABASE_URL=sqlite:////tmp/m3.db alembic upgrade head   # needs JWT_SECRET
cd backend && DATABASE_URL=sqlite:////tmp/m3.db alembic downgrade base
cd backend && DATABASE_URL=sqlite:////tmp/m3.db alembic upgrade head
cd frontend && npm run build && npm test
```

## Acceptance Criteria

- [ ] `test_job_state.py` parametrised over every legal and illegal transition; each legal one writes a `JobEvent`
- [ ] `test_jobs_api.py`: dispatcher CRUD, worker 403 on create, cross-company denied, stale version 409, worker starts/completes only own job
- [ ] Migration upgrades and downgrades cleanly, preserving data
- [ ] Every status write in `app/` goes through `transition()`
- [ ] Squash-merged to `main`

## Risks

| Risk | Mitigation |
|---|---|
| A status write skips `transition()`, so no event or version bump | Test that scans `app/` for direct status writes; e2e test asserts the full event trail |
| `user_id` PK rebuild on SQLite | `batch_alter_table`; round-trip test with real rows |
| `feat/m2-auth` depends on `WorkerProfile(hourly_rate=...)` | Property setter keeps it working; check with a trial merge |
| No git remote, so no PR is possible | Local `git merge --squash` with the PR body as commit message; reported as a deviation from "PR opened" |
