# PRP: G6 — Frontend apps

## Goal

Two working frontend apps served from the one Vite/React SPA: a tradie **worker** area (`/worker/*`) that shares location, receives realtime job offers as an accessible pop-up with a countdown, accepts/declines them, and tracks shifts/jobs/pay; and a **backoffice** area (`/admin/*`) where `company_admin`/`dispatcher` manage workers, jobs, roster and pay runs. Replaces the `frontend/src/main.jsx`/`App.jsx` placeholder with a real `react-router-dom` route tree and a typed API client.

## Non-goals

- **Native mobile apps.** A PWA-friendly responsive layout is sufficient; no React Native / Capacitor wrapper.
- **Multi-instance realtime** — out of scope for G4 (`PRPs/G4-realtime-offers.md` "Non-goals"), so nothing here either; the WS client only has to handle the single-instance contract.
- **Admin/dispatcher realtime push.** Only the worker socket (`WS /api/ws/worker`) exists. The backoffice "live" job status is refetch-after-action, not a push.
- **Shift conflict resolution UI, recurring shifts, roster editing by admin.** `PRPs/G5-roster-payouts.md` scopes shifts to worker self-service only; see "Known backend gaps" below.
- **Editing or deactivating a worker.** `backend/app/routers/workers.py` has `POST`/`GET`/`GET /{id}` only, no `PUT`/`DELETE`. The backoffice workers page is create + list + view only.
- **Treating client-side route guards as security.** `RoleGuard` (Task 9) is a UX convenience — redirect a user away from a screen their role can't use. The server (`require_role`/`require_ws_role`) is the sole authority; every page still gets real 401/403 bodies back from the API and must handle them, not assume the guard already enforced access.
- **Changing `CLAUDE.md`'s validation gates.** This PRP adds no new gate.

## Prerequisites — read before implementing

This PRP was dispatched with `main` at `fb5de96` (G1+G2+G3 only), with `prp/G4` (realtime offer push) and `prp/G5` (roster and payouts) still unmerged, each tracked by a separate merge mission. **Both have since landed, verified while writing this PRP:**

- **`prp/G4` merged** at commit `1411c98` ("Merge prp/G4: realtime offer push"). `WS /api/ws/worker`, `GET /api/offers/pending`, and the `OfferCreatedEvent`/`OfferClosedEvent` schemas are on `main` — confirmed byte-identical to `PRPs/G4-realtime-offers.md`'s design via `diff <(git show prp/G4:backend/app/routers/ws.py) <(git show main:backend/app/routers/ws.py)` (no diff).
- **`prp/G5` merged** at commit `a77e4c7` ("Merge prp/G5: roster and payouts (shifts, on-shift dispatch, clock-in/out, pay runs, CSV export)"). `backend/app/routers/{shifts,time_entries,payruns}.py` are on `main` — confirmed byte-identical to the `prp/G5` branch tip via the same `diff` check on all three files. `backend/app/main.py`'s router block now registers all of `auth, dispatch, jobs, me, payruns, shifts, time_entries, workers, ws`.
- **There is no longer a merge blocker.** Every task in this PRP's task list can be implemented directly against `main` as it stands today.
- One incidental change worth noting: `main`'s `backend/app/routers/dispatch.py` now has `service.BadRequest -> 400` and `service.Forbidden -> 403` in its `_STATUS_FOR` map (new exception types added by G5's on-shift dispatch filter). This doesn't change any contract cited below — `ApiError` (Task 4) already handles arbitrary status codes generically.

## Context

### Existing frontend scaffold

- `frontend/src/main.jsx` — mounts `<App />`, no change needed.
- `frontend/src/App.jsx:1-26` — placeholder `Login`/`Worker`/`Admin` functions and a 4-route `Routes` tree. Replaced by Task 11.
- `frontend/src/App.test.jsx:1-11` — asserts the placeholder's text; rewritten by Task 10.
- `frontend/src/api/client.js:1-8` — `apiFetch(path, options)`: reads `localStorage.getItem('token')`, sets `Authorization: Bearer`, hits `/api${path}`. Extended, not replaced, by Task 5.
- `frontend/src/test/setup.js` loads `@testing-library/jest-dom/vitest`; `frontend/vitest.config.js` wires `jsdom` + `globals: true` + this setup file. Already present, no change.
- `frontend/package.json` — `react-router-dom@^7`, `@testing-library/react@^16`, `vitest@^2`, `jsdom@^25` are already installed. **Missing:** `@testing-library/user-event`, needed for the focus-trap test (Task 19) to simulate real Tab/Shift+Tab key sequences. This is the only new dependency.
- `frontend/vite.config.js:6` — `server.proxy['/api']` is a bare string, which Vite treats as HTTP-only; it does not proxy WebSocket upgrades. `WS /api/ws/worker` will not reach the backend from `npm run dev` until Task 41.

### Auth / tenancy contracts (`main`, G1+G2)

- `POST /api/auth/login` (`backend/app/routers/auth.py:13-23`) — `{email, password}` → `201`? no, `200 {access_token, token_type: "bearer", expires_in}` (`backend/app/schemas/auth.py`); `401 {detail: "Invalid credentials"}` on bad creds.
- `GET /api/me` (`backend/app/routers/me.py:11-27`) — any authenticated role → `{id, email, full_name, role, company_id, company_name, worker_profile: {trades, hourly_rate, active} | null}`. `role` is one of `company_admin | dispatcher | worker` (`backend/app/models/tenant.py:6-9`). This is the single source of truth for which area/nav a logged-in user sees.
- Role-gated 401/403 are real on every route; `RoleGuard` only prevents a redirect flash, per the Non-goals note above.

### Workers (`main`, G2)

- `POST /api/workers` (`backend/app/routers/workers.py:19-30`, `company_admin` only) — `{email, full_name, initial_password, trades[], hourly_rate}` → `201 WorkerOut`; `409 {detail: "Email already registered"}`.
- `GET /api/workers` / `GET /api/workers/{id}` (`workers.py:33-45`, `company_admin` **or** `dispatcher`) — list/detail. No `PUT`/`DELETE` exists (see Non-goals).

### Jobs (`main`, G3)

- `POST /api/jobs` (`backend/app/routers/jobs.py:16-21`, `dispatcher` only) — `{trade, address, lat, lng, scheduled_for}` → `201 JobOut` (status starts `draft`, no offers made yet).
- `GET /api/jobs` (`jobs.py:24-32`, `dispatcher` **or** `company_admin`) — `?status=&limit=&offset=`. **Workers cannot call this** — see "Known backend gaps" below.
- `PATCH /api/jobs/{id}` (`jobs.py:35-43`, `dispatcher` only) — `{status: "offering"|"cancelled"|"draft"}` → `200 JobOut`; `400` invalid transition.

### Dispatch / offers / location (`main`, G3+G4)

- `PUT /api/workers/me/location` (`backend/app/routers/dispatch.py:34-41`, `worker`) — `{lat, lng}` → `200 WorkerLocationOut`.
- `GET /api/offers/pending` (`dispatch.py:44-51`, `worker`) — `?limit=` (1–200) → `200 list[PendingOfferOut]`, soonest-expiring first. Call this after every WS (re)connect's `auth.ok` — see WS protocol below.
- `POST /api/offers/{id}/accept` (`dispatch.py:54-62`, `worker`) → `200 JobOfferOut`; `409 {detail: "Job already assigned"}` (`backend/app/services/dispatch.py:425`) when someone else already won it; `410 {detail: "Offer expired"}` (`dispatch.py:204` default) when it timed out.
- `POST /api/offers/{id}/decline` (`dispatch.py:65-73`, `worker`) → `200 JobOfferOut`.
- `PendingOfferOut` / `JobOfferOut` shape (`backend/app/schemas/jobs.py:61-69,94-115`):
  ```
  JobOfferOut:   { id, job_id, worker_id, status, expires_at, created_at }
  PendingOfferOut (extends JobOfferOut): {
    ...JobOfferOut, offer_id (== id),
    job: { id, trade, address, lat, lng, scheduled_for },
    distance_km: float | null
  }
  ```
  `distance_km` is precomputed server-side — the client never computes distance itself.

### WS protocol (`main`, G4 — `backend/app/routers/ws.py`, `PRPs/G4-realtime-offers.md` "WebSocket protocol" + "Close codes" tables)

- Connect to `WS /api/ws/worker`. First frame **must** be `{"type":"auth","token":"<jwt>"}` within `WS_AUTH_TIMEOUT_S` (5s) — never a query param.
- Server replies `{"type":"auth.ok"}` once registered; only after this does `GET /api/offers/pending` + anything pushed from here on cover every offer (see reconnect contract below).
- Pushed events: `{"type":"offer.created", ...PendingOfferOut fields}`, `{"type":"offer.expired","offer_id","job_id"}`, `{"type":"offer.taken","offer_id","job_id"}`.
- Heartbeat: server sends `{"type":"ping"}` every `WS_HEARTBEAT_S` (30s); reply with any frame (`{"type":"pong"}` conventionally). A client ping is optional and answered `{"type":"pong"}`.
- Close codes: `4401` (re-auth, reconnect), `4403` (not a worker, do not retry), `4408` (heartbeat timeout, reconnect), `1013` (outbound overflow, reconnect + re-poll), `1012` (server restart, reconnect + re-poll).
- **Reconnect contract (no missed offers):** connect → send `auth` → wait `auth.ok` → `GET /api/offers/pending` → merge by `offer_id`, idempotently. An offer may arrive via both the poll and a push; the server's accept response (`409`/`410`) is authoritative on any remaining race.
- **Timestamp trap:** `expires_at`/`created_at`/`scheduled_for` are naive UTC strings with no `Z` suffix (e.g. `"2026-11-05T10:00:30"`, `backend/app/schemas/jobs.py:1,33-37`). `new Date(str)` in the browser parses a string with no offset as **local time**, which silently shifts every countdown by the browser's UTC offset. Every timestamp from the API must go through `parseUtc` (Task 3), not a bare `new Date(...)`.

### Known backend gaps (frontend scoped around these, not guessed past them)

1. **No worker-readable job list.** `GET /api/jobs` is `dispatcher`/`company_admin` only (`jobs.py:24`). A worker's only source of "my jobs" is the job summary embedded in an offer (`PendingOfferOut.job`) at the moment they accept it. **WorkerJobsPage (Task 27-28) is therefore a locally-held list seeded from accepted offers, not a server list** — it is empty again after a cleared browser / new device until a future PRP adds a `GET /api/jobs/mine`-style endpoint. State this limitation in the page's empty state.
2. **No way for a worker to discover their own PayRun ids.** `GET /api/payruns` is `company_admin`-only (`PRPs/G5-roster-payouts.md` "GET /api/payruns"); `GET /api/payruns/{id}/paylines` is worker-scoped but requires already knowing `{id}`. **WorkerPayPage (Task 31-32) takes `payrunId` as a route param** (`/worker/pay/:payrunId`); with no param it shows a gap message ("ask your company for a pay run reference") instead of guessing or enumerating ids. Flag a future `GET /api/payruns/mine` as the real fix.
3. **No company-wide roster view.** `GET /api/shifts` is `worker`-role only and 403s a `company_admin`/`dispatcher` even before checking `worker_id` (`PRPs/G5-roster-payouts.md` "GET /api/shifts": "Only workers can list their own shifts; company_admin/dispatcher get 403"). **AdminRosterPage (Task 37-38) is a placeholder** stating this explicitly; it is not wired to a fetch that would just 403. A future PRP needs a company-scoped roster read endpoint.
4. **No worker edit/deactivate.** Covered in Non-goals.

Each of these is a real product gap, not a frontend shortcut — flag them to the next backend planning pass rather than building against an endpoint that doesn't exist.

## Data model / API contracts

No backend changes. This PRP only consumes the contracts above and the ones below — all now live on `main` (G1 through G5, see Prerequisites):

```
POST /api/shifts                           (worker)   {starts_at, ends_at} -> 201 ShiftResponse
                                                        409 "Shift overlaps existing shift for this worker"
GET  /api/shifts?worker_id=&limit=&offset= (worker)   -> 200 list[ShiftResponse]

POST /api/jobs/{job_id}/clock-in           (worker)   {} -> 200 TimeEntryResponse (assigned -> in_progress)
                                                        409 "Job not assigned to this worker" | "Job already has a TimeEntry" | "Worker is not on shift"
POST /api/jobs/{job_id}/clock-out          (worker)   {} -> 200 TimeEntryResponse (in_progress -> completed)
                                                        409 "TimeEntry already ended"

GET   /api/payruns?status=&limit=&offset=  (company_admin) -> 200 list[PayRunResponse]
POST  /api/payruns                         (company_admin) {period_start, period_end} -> 201 PayRunResponse (status: draft)
PATCH /api/payruns/{id}                    (company_admin) {status: "approved"|"paid"} -> 200 PayRunResponse
                                                        409 "Cannot transition from <current> to <target>" | "Cannot edit an approved/paid payrun"
GET   /api/payruns/{id}/paylines           (worker)        -> 200 list[PayLineResponse] (own worker_id only, else 403)
GET   /api/payruns/{id}/export?format=csv  (company_admin) -> text/csv stream
                                                        headers: worker_name,worker_email,period_start,period_end,minutes_worked,hourly_rate_cents,amount_cents

ShiftResponse:    { id, worker_id, starts_at, ends_at, created_at }
TimeEntryResponse:{ id, job_id, worker_id, started_at, ended_at }
PayRunResponse:   { id, company_id, period_start, period_end, status, created_at, updated_at }
PayLineResponse:  { id, payrun_id, worker_id, minutes_worked, hourly_rate_cents, amount_cents, created_at }
```

## Route map

```
/                         redirect: unauthenticated -> /login; authenticated -> /worker or /admin by role
/login                    Login                                           (public)
/worker                   RoleGuard[worker] > WorkerLayout
  /worker                 redirect -> /worker/jobs
  /worker/jobs            WorkerJobsPage
  /worker/shifts          WorkerShiftsPage
  /worker/pay             WorkerPayPage
  /worker/pay/:payrunId   WorkerPayPage
/admin                    RoleGuard[company_admin, dispatcher] > AdminLayout
  /admin                  redirect -> /admin/jobs
  /admin/workers          AdminWorkersPage      (create: company_admin only; view: both)
  /admin/jobs             AdminJobsPage         (create/transition: dispatcher; view: both)
  /admin/roster           AdminRosterPage       (gap placeholder, both roles — see Known backend gap #3)
  /admin/payruns          AdminPayRunsPage      (company_admin only)
*                         NotFound
```

## Component tree

```
App
└─ AuthProvider                          (frontend/src/auth/AuthContext.jsx)
   └─ BrowserRouter
      └─ Routes
         ├─ Login                        (frontend/src/pages/Login.jsx)
         ├─ RoleGuard allow=[worker]      (frontend/src/auth/RoleGuard.jsx)
         │  └─ WorkerLayout               (frontend/src/layouts/WorkerLayout.jsx)
         │     ├─ Nav                     (frontend/src/components/Nav.jsx)
         │     ├─ ShareLocationToggle     (uses frontend/src/location/useShareLocation.js)
         │     └─ OfferProvider           (frontend/src/offers/OfferProvider.jsx)
         │        ├─ OfferModal           (frontend/src/offers/OfferModal.jsx)
         │        │  ├─ useCountdown      (frontend/src/offers/useCountdown.js)
         │        │  └─ useFocusTrap      (frontend/src/offers/useFocusTrap.js)
         │        └─ Outlet
         │           ├─ WorkerJobsPage    (frontend/src/pages/worker/WorkerJobsPage.jsx)
         │           ├─ WorkerShiftsPage  (frontend/src/pages/worker/WorkerShiftsPage.jsx)
         │           └─ WorkerPayPage     (frontend/src/pages/worker/WorkerPayPage.jsx)
         └─ RoleGuard allow=[company_admin, dispatcher]
            └─ AdminLayout                (frontend/src/layouts/AdminLayout.jsx)
               ├─ Nav
               └─ Outlet
                  ├─ AdminWorkersPage     (frontend/src/pages/admin/AdminWorkersPage.jsx)
                  ├─ AdminJobsPage        (frontend/src/pages/admin/AdminJobsPage.jsx)
                  ├─ AdminRosterPage      (frontend/src/pages/admin/AdminRosterPage.jsx)
                  └─ AdminPayRunsPage     (frontend/src/pages/admin/AdminPayRunsPage.jsx)
```

## Task list

### Phase 0 — dependency (hotspot file)

1. `frontend/package.json` — add `@testing-library/user-event` as a devDependency. **This is the only task in this PRP allowed to touch `frontend/package.json`.**

### Phase 1 — time + API client foundation

2. `frontend/src/api/time.test.js` — failing test: `parseUtc('2026-11-05T10:00:30')` must equal `new Date('2026-11-05T10:00:30Z')`, not `new Date('2026-11-05T10:00:30')` (proves naive-UTC strings aren't read as local time).
3. `frontend/src/api/time.js` — implement `parseUtc(value)`: append `Z` when the string has no timezone offset, then `new Date(...)`.
4. `frontend/src/api/client.test.js` — failing test: `apiRequest` on a non-2xx `fetch` response throws an `ApiError` carrying `.status` and the parsed `detail` string from the JSON body.
5. `frontend/src/api/client.js` — extend `apiFetch` (`frontend/src/api/client.js:1-8`) with `ApiError`, `apiRequest(path, options)`, and typed resource functions: `auth.login`, `me.get`, `workers.{create,list,get}`, `jobs.{create,list,transition}`, `offers.{pending,accept,decline}`, `location.update`, `shifts.{create,list}`, `payruns.{list,create,transition,paylines,exportUrl}`.

### Phase 2 — auth context and route guard

6. `frontend/src/auth/AuthContext.test.jsx` — failing test: `login(email, password)` stores the returned token in `localStorage`, calls `GET /api/me`, and exposes `user.role` from the response.
7. `frontend/src/auth/AuthContext.jsx` — implement `AuthProvider`/`useAuth` (token + user state; `login`, `logout`; bootstraps `GET /api/me` on mount if a token already exists in `localStorage`).
8. `frontend/src/auth/RoleGuard.test.jsx` — failing test: an authenticated user whose role is not in `allow` is redirected, not rendered; an unauthenticated user is redirected to `/login`.
9. `frontend/src/auth/RoleGuard.jsx` — implement `RoleGuard({ allow, children })` via `useAuth()` + `Navigate`. Comment explicitly: this redirect is UX only — the server remains the sole authority.

### Phase 3 — shell, nav, routing

10. `frontend/src/App.test.jsx` — rewrite the placeholder assertions (`frontend/src/App.test.jsx:7-10`): unauthenticated `/` renders the real `Login` form (email + password fields), not placeholder text.
11. `frontend/src/App.jsx` — replace the placeholder `Login`/`Worker`/`Admin` functions (`frontend/src/App.jsx:3-13`) with the real route tree from the Route map above.
12. `frontend/src/components/Nav.test.jsx` — failing test (**verbatim**): `worker role cannot see /admin nav`.
13. `frontend/src/components/Nav.jsx` — implement `Nav`: link set conditioned on `useAuth().user.role` (worker: Jobs/Shifts/Pay; company_admin: Workers/Jobs/Roster/PayRuns; dispatcher: Workers(view-only)/Jobs/Roster, no PayRuns link).
14. `frontend/src/layouts/WorkerLayout.test.jsx` — failing test: renders `Nav` plus the nested route's `Outlet` content.
15. `frontend/src/layouts/WorkerLayout.jsx` and `frontend/src/layouts/AdminLayout.jsx` — implement both (`Nav` + `<Outlet />`; `WorkerLayout` also mounts `OfferProvider` and `ShareLocationToggle`).
16. `frontend/src/pages/Login.jsx` — implement the login form calling `useAuth().login`; on success redirect to the role's home route; on `401` show `"Invalid credentials"` (`backend/app/routers/auth.py:19`).

### Phase 4 — realtime offers (worker; buildable today, G4 is on `main`)

17. `frontend/src/offers/useCountdown.test.js` — failing test (**verbatim**, split across this hook and Task 21/22): `offer modal renders and countdown hits 0 -> disabled` — a countdown to a target `Date` reaches 0 and the hook reports a disabled state.
18. `frontend/src/offers/useCountdown.js` — implement `useCountdown(targetDate)`: ticks every second, floors at 0, returns `{ secondsLeft, isExpired }`.
19. `frontend/src/offers/useFocusTrap.test.jsx` — failing test (**verbatim**): `modal is keyboard-focus-trapped with labelled buttons` — using `@testing-library/user-event`'s `tab()`, focus cycles only within the trapped container (wraps from last to first and back), and the Accept/Decline buttons carry accessible labels.
20. `frontend/src/offers/useFocusTrap.js` — implement a hand-rolled Tab/Shift+Tab `keydown` trap on a container ref. The modal renders as `role="dialog" aria-modal="true"` on a plain `<div>`, not `<dialog>` — jsdom 25 does not implement `HTMLDialogElement.showModal()`.
21. `frontend/src/offers/OfferModal.test.jsx` — failing test (**verbatim**): `Accept calls POST /api/offers/{id}/accept and handles 409 "taken"` — clicking Accept calls `offers.accept(id)`; a `409 {detail: "Job already assigned"}` response closes the modal with a "no longer available" message rather than throwing an unhandled error; also cover `410 "Offer expired"` the same way.
22. `frontend/src/offers/OfferModal.jsx` — implement: job details (`trade`, `address`), `distance_km`, countdown via `useCountdown(parseUtc(offer.expires_at))`, focus trap via `useFocusTrap`, labelled Accept/Decline buttons, both disabled once the countdown hits 0.
23. `frontend/src/offers/OfferProvider.test.jsx` — failing test: after `auth.ok`, the provider calls `GET /api/offers/pending` and merges the result by `offer_id` with any already-queued WS `offer.created` events, idempotently (no duplicates).
24. `frontend/src/offers/OfferProvider.jsx` — implement: opens `WS /api/ws/worker`, sends the `auth` frame first, on `auth.ok` fetches `GET /api/offers/pending`, keeps an offer queue keyed by `offer_id`, drops offers on `offer.expired`/`offer.taken`, exposes `activeOffer`/`accept`/`decline` via context, reconnects with backoff on `4408`/`1012`/`1013` and forces logout (via `useAuth().logout`) on `4401`/`4403`.

### Phase 5 — worker self-service (location, shifts, clock-in/out, pay)

25. `frontend/src/location/useShareLocation.test.js` — failing test: toggling on calls `navigator.geolocation.watchPosition` and `PUT /api/workers/me/location` with each position; toggling off clears the watch.
26. `frontend/src/location/useShareLocation.js` — implement the toggle hook.
27. `frontend/src/pages/worker/WorkerJobsPage.test.jsx` — failing test: a job accepted via an offer appears in "my jobs"; "Start" calls `POST /api/jobs/{id}/clock-in` and moves it to `in_progress`; "Complete" calls `POST /api/jobs/{id}/clock-out` and moves it to `completed`.
28. `frontend/src/pages/worker/WorkerJobsPage.jsx` — implement against the locally-held job list seeded from accepted offers (Known backend gap #1); empty state names the gap.
29. `frontend/src/pages/worker/WorkerShiftsPage.test.jsx` — failing test: creating an overlapping shift surfaces the exact 409 detail `"Shift overlaps existing shift for this worker"`.
30. `frontend/src/pages/worker/WorkerShiftsPage.jsx` — implement list (`GET /api/shifts`) + create form (`POST /api/shifts`).
31. `frontend/src/pages/worker/WorkerPayPage.test.jsx` — failing test: given a `payrunId` route param, renders only that worker's own paylines from `GET /api/payruns/{id}/paylines`; with no param, renders the gap message (Known backend gap #2) instead of guessing an id.
32. `frontend/src/pages/worker/WorkerPayPage.jsx` — implement.

### Phase 6 — backoffice (workers, jobs, roster placeholder, pay runs)

33. `frontend/src/pages/admin/AdminWorkersPage.test.jsx` — failing test: `company_admin` sees a create form and the created worker appears in the list; a `dispatcher` sees the list without a create form; a `409 "Email already registered"` surfaces on the form.
34. `frontend/src/pages/admin/AdminWorkersPage.jsx` — implement `GET /api/workers`, `POST /api/workers` (admin only), link to detail (`GET /api/workers/{id}`). No edit/delete UI — the route doesn't exist (Non-goals).
35. `frontend/src/pages/admin/AdminJobsPage.test.jsx` — failing test: `dispatcher` creates a job and the list refetches and shows its status; `company_admin` sees the list without a create form.
36. `frontend/src/pages/admin/AdminJobsPage.jsx` — implement. "Live status" means refetch after create/transition — there is no admin-facing push.
37. `frontend/src/pages/admin/AdminRosterPage.test.jsx` — failing test: renders the documented backend-gap message (Known backend gap #3), not a broken fetch or a 403 the user didn't ask for.
38. `frontend/src/pages/admin/AdminRosterPage.jsx` — implement the placeholder.
39. `frontend/src/pages/admin/AdminPayRunsPage.test.jsx` — failing test: a `draft` PayRun transitions `draft -> approved -> paid` via `PATCH /api/payruns/{id}`; a further `PATCH` after `paid` surfaces its `409` detail; an export control points at `GET /api/payruns/{id}/export?format=csv` (an `<a href>`, not a `fetch`-then-blob dance — the browser's native download handles the stream).
40. `frontend/src/pages/admin/AdminPayRunsPage.jsx` — implement list/create/transition/export-link; `company_admin` only — hidden from `dispatcher`'s `Nav` (Task 13) and guarded in-page as a defense-in-depth UX check.

### Phase 7 — dev wiring

41. `frontend/vite.config.js` — add `ws: true` to the `/api` proxy (`frontend/vite.config.js:6`) so `WS /api/ws/worker` reaches the backend under `npm run dev`. No automated test — Vitest doesn't drive the Vite dev server; verify manually (open `/worker`, confirm the socket connects, in a real `npm run dev` + `uvicorn --reload` session) and note the manual check in the PR description.
42. Run the full validation gate list below end to end; fix any regression before the final commit of the implementation pass.

## Named Vitest/RTL test list

Required verbatim (do not paraphrase or weaken):
1. `offer modal renders and countdown hits 0 → disabled`
2. `Accept calls POST /api/offers/{id}/accept and handles 409 "taken"`
3. `worker role cannot see /admin nav`
4. `modal is keyboard-focus-trapped with labelled buttons`

Additional tests from the task breakdown:
5. `parseUtc reads a naive backend timestamp as UTC, not local time`
6. `apiRequest throws ApiError with status and parsed detail on a non-2xx response`
7. `AuthContext.login stores the token and loads the current user from GET /api/me`
8. `RoleGuard redirects a user whose role is not in allow, and an unauthenticated user to /login`
9. `App renders the real Login form at / when unauthenticated`
10. `WorkerLayout renders Nav and the active child route`
11. `OfferProvider merges GET /api/offers/pending with queued WS events by offer_id without duplicates`
12. `OfferModal Decline calls POST /api/offers/{id}/decline`
13. `useShareLocation toggling on calls watchPosition and PUT /api/workers/me/location; toggling off clears the watch`
14. `WorkerJobsPage Start/Complete call clock-in/clock-out and move the job through in_progress to completed`
15. `WorkerShiftsPage surfaces the exact 409 overlap detail from the API`
16. `WorkerPayPage with no payrunId route param shows the pay-run-discovery gap message instead of guessing an id`
17. `AdminWorkersPage hides the create form from a dispatcher and surfaces 409 duplicate-email`
18. `AdminJobsPage create form is hidden from company_admin and refetches the list after a dispatcher creates a job`
19. `AdminRosterPage renders the documented backend-gap message`
20. `AdminPayRunsPage transitions draft → approved → paid and blocks further edits with the 409 detail after paid`

## Validation gates

Exact commands from `CLAUDE.md`'s "Test / validate" section — this PRP adds no new gate:

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head      # needs JWT_SECRET; use a scratch DATABASE_URL
cd backend && alembic downgrade base    # then upgrade head again to prove the round trip
cd frontend && npm run build
cd frontend && npm test
```

Plus the manual dev-proxy WS check from Task 41 (not automatable in Vitest).

## Acceptance criteria

1. `frontend/src/App.jsx` renders the full route map above; `/`, `/login`, `/worker/*`, `/admin/*` all resolve to real components, not the old placeholders.
2. A `worker` logging in sees `/worker/jobs`, `/worker/shifts`, `/worker/pay`, can toggle sharing location, and — with the backend running and an offer created for them — sees the accessible offer modal with job details, distance, and a live countdown, and can Accept/Decline.
3. A `company_admin`/`dispatcher` logging in sees `/admin/*` and cannot see or reach `/worker/*` (redirected); a `worker` cannot see or reach `/admin/*` nav or routes.
4. All four verbatim tests in the Named test list pass, plus the rest of that list.
5. `cd frontend && npm run build` and `npm test` both pass; `frontend/package.json` was touched by exactly one task (Task 1).
6. Before implementation starts, re-confirm `main` still contains `backend/app/routers/{shifts,time_entries,payruns}.py` and `WS /api/ws/worker` (both merged as of `a77e4c7`/`1411c98` per Prerequisites) — if a later history rewrite ever reverted either merge, re-apply the `[BLOCKED]` treatment from the original task-list drafting before continuing.

## Risks

- **Scope size.** 42 tasks across two apps is large for one PRP; if the implementation session's budget is tight, the natural split is Phase 0–4 (buildable today, no G5 dependency) as one implementation pass and Phase 5/6's G5-dependent tasks as a follow-on pass once `prp/G5` lands — both still inside this one PRP file and branch, just sequenced.
- **Rebase freshness.** `prp/G6` is branched from `main` at `a77e4c7` (after both the G4 and G5 merges). If `main` moves further before `/prp-implement` runs, rebase `prp/G6` onto the new tip first and re-check the Prerequisites citations still hold.
- **Naive-UTC timestamps.** Every countdown, shift time, and pay period date from the API has no timezone marker. Mitigation: `parseUtc` (Task 3) is the only sanctioned way to turn an API timestamp into a `Date`; a future code review should grep for bare `new Date(` against API responses.
- **jsdom has no native `<dialog>` support.** Mitigation: the offer modal is a plain `role="dialog"` div with a hand-rolled focus trap (Tasks 19–20), not `HTMLDialogElement`.
- **Backend gaps 1–3 (Context).** Each scopes a page down to what the API actually supports; if a future backend PRP adds the missing endpoints, these three pages (WorkerJobsPage, WorkerPayPage, AdminRosterPage) are the ones to revisit.

## Rollback

- Delete branch `prp/G6`.
- Remove `PRPs/G6-frontend.md`.
- No other files are touched by this planning step — there is nothing else to revert.

---

**Authored by**: Farhan Rashid
**Date**: 2026-11-05
