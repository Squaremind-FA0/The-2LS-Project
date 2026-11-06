# Plan: M5 Rostering — shifts + availability windows

## Summary

`main` @ `256ae0c` already has a `Shift` model, a worker-only self-service API
(`POST`/`GET /api/shifts`, `backend/app/services/shifts.py`), and an on-shift
dispatch filter inlined in `find_nearby_workers`
(`backend/app/services/dispatch.py:96-106`), all built under PRP G5
(`PRPs/G5-roster-payouts.md`, merged `a77e4c7`). `AdminRosterPage.jsx` is an
explicit documented gap ("G6 known backend gap 3" — no company-wide roster
read). M5 does **not** rebuild shifts — it extends the existing table and adds
the missing admin surface, following the M3/M4 precedent of closing gaps
against already-shipped PRP work rather than building a parallel system.

## Gaps vs. the brief (verified against the code before writing this plan)

| Brief requirement | Current state | Gap |
|---|---|---|
| `Shift(id, company_id, worker_id, starts_at, ends_at, notes, status)` | `Shift` has no `notes`, no `status` (`app/models/shift.py`). | Add both columns, migration 0006. |
| DB `CHECK ends_at > starts_at` | Enforced only in app code (`services/shifts.py:16-17`), no DB constraint. | Add via `batch_alter_table` (SQLite). |
| Index on `(company_id, worker_id, starts_at)` | `ix_shifts_company_worker_window` already covers `(company_id, worker_id, starts_at, ends_at)` — a superset, left-prefix-compatible index. | **No new index** — adding a redundant narrower one would just cost write overhead for no query benefit. Documented divergence. |
| Admin/dispatcher CRUD at `/api/roster/shifts` | Does not exist. Only worker self-service at `/api/shifts`. | New `app/roster/` router + service. |
| `GET /api/roster?from=&to=` week view | Does not exist; `AdminRosterPage.jsx` is a placeholder for exactly this. | New endpoint. |
| Overlap rejected 409 | Implemented for the worker path (`services/shifts.py:23-32`), excluding nothing by status (no status existed). | Reuse the same guard for admin CRUD; both now exclude `cancelled` shifts once `status` exists. |
| `GET /api/me/shifts` for workers | Workers currently read their own shifts at `GET /api/shifts` (worker-only role already). | Add `/api/me/shifts` as a thin alias in `app/routers/me.py`, alongside `/api/me/location`, `/api/me/availability`, `/api/me/jobs`, `/api/me/offers` — matching the existing `/api/me/*` convention. `GET /api/shifts` stays: `WorkerShiftsPage.jsx` posts to it and removing it is out of scope. |
| `is_on_shift(worker_id, at)` pure function in `app/roster/queries.py` | On-shift logic is inlined as an `EXISTS` subquery in `find_nearby_workers` (`services/dispatch.py:96-106`), not a reusable function. | New function, DB-backed (session-scoped, so not argument-free, but read-only/deterministic — "pure" in the brief's sense). **Not wired into the matcher this PR**, per the brief's explicit instruction. |
| M2 `scoped()` helper | No such helper exists anywhere in the codebase (confirmed by grep) — same divergence M3 documented: `app/tenancy.py`'s fail-closed session listener + `get_tenant_db` + `tests/test_route_audit.py` play that role instead. | Not added, for the same reason M3 didn't add one. All new routes use `get_tenant_db`. |

## Non-goals

- Wiring `is_on_shift` into `find_nearby_workers`/the matcher. The brief is
  explicit this is a follow-up after M4 (already merged). The existing inline
  `EXISTS` filter in `services/dispatch.py` is untouched.
- Touching `backend/app/dispatch/` at all (mission constraint: M5 runs in
  parallel with M4 and must not touch it). Grep-verified clean before and
  after.
- Removing or changing `GET`/`POST /api/shifts` (worker self-service) beyond
  the additive `notes`/`status` fields on the response, and excluding
  cancelled shifts from its overlap guard.
- A separate `AvailabilityWindow` entity. The brief's task list only ever
  describes one entity (`Shift`); "availability windows" in the title is
  descriptive of what a shift *is*, not a second model.
- Frontend changes. `AdminRosterPage.jsx` stays a placeholder; wiring it to
  the new endpoints is a natural follow-up but is not in this mission's task
  list.
- Initializing the git remote. `git remote -v` now shows `origin` pointing at
  a real, reachable, **empty** GitHub repo (no branches pushed yet) —
  different from M3/M4's "no remote exists" precedent. Pushing `main`'s full
  history (including every prior PRP/mission's work) to a remote for the
  first time is a repo-wide, hard-to-reverse action outside this mission's
  scope. This PRP follows the M3/M4 precedent: local `--squash` merge into
  local `main`, and flags the remote's existence as a decision for a human in
  the final report.

## Branch

`feat/m5-roster` off `main` (`256ae0c`).

## Patterns to mirror

- Overlap guard with row lock: `services/shifts.py:15-36` (`with_for_update`
  on the worker's `User` row, then a half-open-interval overlap query). Same
  pattern reused for admin CRUD, with `status == scheduled` added to both the
  read and the write side so a cancelled shift never blocks a new one.
- Service errors → HTTP: `app/routers/dispatch.py`'s `raise_http` +
  `app.services.dispatch.{NotFound,Conflict,BadRequest,DispatchError}`,
  already imported this way by `app/routers/shifts.py`. Reused as-is for the
  new `app/roster/` router — no new exception hierarchy.
- `Enum(..., native_enum=False, length=20)` + `server_default` for a status
  column: `app/models/payrun.py:14-33` (`PayRunStatus`).
- `batch_alter_table` for a SQLite-safe column + constraint add:
  `migrations/versions/0005_core_domain.py:52-62`.
- Tenant scoping is automatic and fail-closed: a plain `db.get(User, id)` or
  `select(Shift)...` inside a `get_tenant_db`-scoped session already filters
  to the caller's company (`app/tenancy.py`). Cross-company existence checks
  need no explicit `company_id ==` clause — confirmed this is how
  `find_nearby_workers` and every router already behaves.
- Test data: `tests/factories.py` (`two_companies`, `tenant_context`), and
  `tests/test_dispatch.py`'s `add_worker`/`add_shift`/`fresh` re-exported into
  `test_roster.py` already (`td = tests.test_dispatch`).

## Data model / API contracts

### `backend/app/models/shift.py`

```python
class ShiftStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"

class Shift(TenantScoped, Base):
    __table_args__ = (
        Index("ix_shifts_company_worker_window", "company_id", "worker_id", "starts_at", "ends_at"),
        CheckConstraint("ends_at > starts_at", name="ck_shifts_ends_after_starts"),
    )
    ...
    notes: Mapped[str | None] = mapped_column(String(500), default=None)
    status: Mapped[ShiftStatus] = mapped_column(
        Enum(ShiftStatus, name="shift_status", native_enum=False, length=20),
        default=ShiftStatus.scheduled, server_default=ShiftStatus.scheduled.value,
    )
```

### Migration `0006_roster_shift_status.py` (revises `0005`)

`batch_alter_table("shifts")`: add `notes` (nullable `String(500)`), add
`status` (`Enum(..., native_enum=False, length=20)`, `server_default="scheduled"`,
`nullable=False` — existing rows backfill to `scheduled`), add check
constraint `ck_shifts_ends_after_starts`. Downgrade drops all three in
reverse order.

### `backend/app/roster/` (new package)

- `queries.py`: `is_on_shift(db: Session, worker_id: int, at: datetime) -> bool`
  — `SELECT 1 FROM shifts WHERE worker_id=:w AND status='scheduled' AND
  starts_at <= :at AND ends_at > :at LIMIT 1`, same half-open semantics as
  the matcher's existing inline filter (`<=` start, `>` end) so a future
  switchover is behavior-preserving.
- `service.py`: `create_shift(db, worker_id, starts_at, ends_at, notes=None)`,
  `get_shift(db, shift_id)`, `update_shift(db, shift_id, **fields)` (accepts
  any of `starts_at`/`ends_at`/`notes`/`status`; re-validates the window and
  re-checks overlap — excluding itself — whenever the shift ends up
  `scheduled` with new or existing times), `delete_shift(db, shift_id)`,
  `list_week(db, from_dt, to_dt, worker_id=None)` (shifts whose window
  overlaps `[from_dt, to_dt)`). Cross-company `worker_id` on create: a bare
  `db.get(User, worker_id)` under the tenant-scoped session returns `None`
  for another company's user (fail-closed via `app/tenancy.py`), raising
  `NotFound` → 404.
- `router.py`: `APIRouter(prefix="/api/roster")`, roles
  `require_role(Role.dispatcher, Role.company_admin)`:
  - `POST /api/roster/shifts` — body `{worker_id, starts_at, ends_at, notes?}`, 201.
  - `GET /api/roster/shifts/{shift_id}` — 200/404.
  - `PATCH /api/roster/shifts/{shift_id}` — any subset of
    `{starts_at, ends_at, notes, status}`, `exclude_unset=True` so an omitted
    field is left alone (not nulled).
  - `DELETE /api/roster/shifts/{shift_id}` — 204.
  - `GET /api/roster?from=&to=&worker_id=` — week view, `from`/`to` required
    query params (`from` aliased since it's a Python keyword), `worker_id`
    optional filter.

### `backend/app/routers/me.py`

`GET /api/me/shifts` — worker-only, `?limit=&offset=`, reuses
`app.services.shifts.list_shifts` (unchanged function), returns the now-wider
`ShiftResponse`.

### `backend/app/schemas/shifts.py`

- `ShiftCreate` (worker-facing, unchanged route): add optional `notes: str | None = None`.
- `ShiftResponse`: add `notes: str | None` and `status: ShiftStatus`. **Breaking
  change to `test_shift_response_and_stored_owner_come_from_the_token`'s exact
  key-set assertion** — updated in task 1 below, in scope per the brief.
- New `RosterShiftCreate` (`worker_id` required, `extra="forbid"`) and
  `RosterShiftUpdate` (`extra="forbid"`, every field optional) for the admin
  router. Both share the same naive-UTC datetime normalizer as `ShiftCreate`.

### `backend/app/services/shifts.py`

`create_shift` gains an optional `notes` param; its overlap query adds
`Shift.status == ShiftStatus.scheduled` so a cancelled shift never blocks a
new one for the worker-facing path either.

### `backend/app/main.py`

`from app.roster.router import router as roster_router`; add
`app.include_router(roster_router)` next to the other routers.

## Task list (test-first, one commit each)

1. **RED — `backend/tests/test_migrations.py`**: add
   `test_0006_adds_shift_status_and_notes_and_downgrades_to_0005` (mirrors
   `test_0004_...`/`test_0005_...`): upgrade to head, assert `shifts` has
   `notes`/`status` columns and the `ck_shifts_ends_after_starts` check
   constraint (`inspector.get_check_constraints`), insert a row violating the
   check via raw SQL and assert it raises, `alembic check` clean, downgrade
   to `0005` drops both columns and the constraint, upgrade back to head.
   **GREEN** — `backend/migrations/versions/0006_roster_shift_status.py`,
   `backend/app/models/shift.py` (`ShiftStatus`, `notes`, `status`, the check
   constraint), `backend/app/models/__init__.py` (export `ShiftStatus`).
2. **RED — `backend/tests/test_roster.py`**: update
   `test_shift_response_and_stored_owner_come_from_the_token`'s key-set
   assertion to include `notes`/`status`; add
   `test_cancelled_shift_does_not_block_a_new_overlapping_shift` (worker path:
   create, cancel via direct DB update — no admin endpoint exists yet at this
   task — then post an overlapping shift and expect 201).
   **GREEN** — `backend/app/schemas/shifts.py` (`ShiftResponse` fields,
   `ShiftCreate.notes`), `backend/app/services/shifts.py` (`notes` param,
   `status == scheduled` in the overlap query), `backend/app/routers/shifts.py`
   (pass `payload.notes` through).
3. **RED — `backend/tests/test_roster.py`**: add
   `test_is_on_shift_true_inside_window_false_outside_at_the_edges`
   (parametrized like `test_on_shift_filter_uses_the_half_open_window`: covers
   now / starts_at inclusive / ends_at exclusive / before / after) and
   `test_is_on_shift_ignores_a_cancelled_shift`, calling
   `app.roster.queries.is_on_shift` directly against `transactional_db`.
   **GREEN** — `backend/app/roster/__init__.py`, `backend/app/roster/queries.py`.
4. **RED — `backend/tests/test_roster.py`**: add admin CRUD tests —
   `test_admin_creates_a_shift_for_a_worker`,
   `test_dispatcher_and_admin_can_create_list_update_and_delete_a_shift`,
   `test_admin_shift_create_overlap_rejected_with_409`,
   `test_admin_shift_create_touching_boundary_allowed`,
   `test_admin_create_for_a_cross_company_worker_is_404`,
   `test_admin_update_excludes_itself_from_its_own_overlap_check`,
   `test_cancelling_a_shift_via_patch_then_frees_the_slot`,
   `test_worker_role_is_403_on_every_roster_admin_route`. **GREEN** —
   `backend/app/roster/service.py`, `backend/app/roster/router.py`,
   `backend/app/schemas/shifts.py` (`RosterShiftCreate`, `RosterShiftUpdate`),
   `backend/app/main.py` (register the router).
5. **RED — `backend/tests/test_roster.py`**: add
   `test_roster_week_view_returns_shifts_overlapping_the_range_across_workers`,
   `test_roster_week_view_can_filter_by_worker_id`,
   `test_roster_week_view_is_company_scoped`,
   `test_roster_week_view_requires_admin_or_dispatcher_role`. **GREEN** —
   `app/roster/service.py::list_week`, `app/roster/router.py`'s `GET ""` route.
6. **RED — `backend/tests/test_roster.py`**: add
   `test_worker_reads_only_own_shifts_at_me_shifts`,
   `test_me_shifts_is_403_for_admin_and_dispatcher`. **GREEN** —
   `backend/app/routers/me.py` (`GET /api/me/shifts`).
7. **RED — `backend/tests/test_route_audit.py`**: add
   `/api/roster`, `/api/roster/shifts/{shift_id}`, `/api/me/shifts` to
   `test_audit_actually_sees_the_api_routes`'s guarded set; add
   `("POST", "/api/roster/shifts"): {"dispatcher", "company_admin"}`,
   `("GET", "/api/roster/shifts/{shift_id}"): {"dispatcher", "company_admin"}`,
   `("PATCH", "/api/roster/shifts/{shift_id}"): {"dispatcher", "company_admin"}`,
   `("DELETE", "/api/roster/shifts/{shift_id}"): {"dispatcher", "company_admin"}`,
   `("GET", "/api/roster"): {"dispatcher", "company_admin"}`,
   `("GET", "/api/me/shifts"): {"worker"}` to
   `test_roster_and_payroll_routes_require_the_prp_roles`. **GREEN** — none
   expected (the routes already carry the right roles from task 4/6; this
   task only locks the contract down, matching the M4 precedent of updating
   this file in the same PRP that adds routes).
8. **Final check**: re-run every validation gate against the full branch;
   confirm every acceptance criterion below explicitly; grep-confirm
   `backend/app/dispatch/` is untouched by `git diff main...HEAD`.

## Validation gates

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head      # needs JWT_SECRET; use a scratch DATABASE_URL
cd backend && alembic downgrade base    # then upgrade head again to prove the round trip
cd frontend && npm run build
cd frontend && npm test
```

From root `CLAUDE.md`. No frontend file is touched by this PRP; the frontend
gates only confirm no regression.

## Acceptance criteria

- [ ] `test_roster.py` covers: admin/dispatcher CRUD on `/api/roster/shifts`;
      overlap rejected 409 (both worker and admin paths); a touching boundary
      (`end == next start`) allowed; a worker reads only their own shifts
      (`GET /api/me/shifts`); cross-company access denied (both the admin
      create-for-another-company's-worker case and the existing worker
      cross-company case); `is_on_shift` true/false at the edges, including
      a cancelled shift never counting as on-shift.
- [ ] Migration 0006 upgrades and downgrades cleanly (`alembic check` clean
      both directions), proven against a real row.
- [ ] `backend/app/dispatch/` is untouched (`git diff main...HEAD -- backend/app/dispatch/`
      empty) — M5 runs in parallel with M4 and must not conflict with it.
- [ ] `is_on_shift` exists in `app/roster/queries.py` and is **not** called
      from `app/services/dispatch.py` or `app/dispatch/matcher.py` this PR.
- [ ] Squash-merged to `main`.

## Risks

| Risk | Mitigation |
|---|---|
| Adding `status` with a DB-level default changes the overlap query's result set for every pre-existing worker-facing test that calls `add_shift` (no `status` kwarg). | `add_shift`'s ORM insert goes through the Python-level `default=ShiftStatus.scheduled`, so every existing row is `scheduled` — behavior is unchanged for every pre-existing test. Verified by running the full suite unmodified before task 2's schema change lands. |
| `GET /api/roster` and `/api/roster/shifts/*` share a prefix but different role sets in spirit (both are dispatcher/admin here) — easy to typo a role tuple. | Task 7 pins every new route's exact role set in `test_route_audit.py`, mirroring the M4 precedent. |
| A second, parallel M4 worktree/branch could still be touching `app/services/dispatch.py` for an unrelated reason. | This PRP makes zero edits to `app/services/dispatch.py`, `app/dispatch/`, or any dispatch test file — confirmed by task 8's diff grep before merge. |
| `origin` now exists (unlike M3/M4's assumption) but is empty; pushing `main` for the first time is irreversible-ish and outside this mission's scope. | Local squash-merge, same as M3/M4; flagged as a decision for a human in the mission report rather than acted on unilaterally. |

## Rollback

1. **Before merge:** abandon `feat/m5-roster`. Branch deletion is destructive
   and **requires human approval**.
2. **After merge:** revert the merge commit, then `alembic downgrade 0005` on
   any environment that already ran `0006`.
3. **Partial rollback (keep the migration, drop only the new endpoints):**
   remove `app.include_router(roster_router)` and the `/api/me/shifts` route;
   the `notes`/`status` columns are additive and harmless to leave in place.
