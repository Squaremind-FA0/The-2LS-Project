# Plan: M1 Foundation — backend package, DB/migrations, test harness

## Summary
Restructure the bare scaffold (commit e18930c) into a maintainable foundation: a
proper `backend/app` package with settings/DB/session plumbing, Alembic migrations
run on container start, a locked-down CORS config, pytest/ruff for the backend,
and vitest/eslint-equivalent tooling plus routing scaffolding for the frontend.
No domain models are introduced — this is pure scaffolding for later missions
(auth, jobs, dispatch) to build on.

## User Story
As a platform engineer picking up this project after M1,
I want a backend package layout with config/DB/migrations and a test harness,
So that I can add domain models and routes in later missions without re-plumbing
the foundation each time.

## Problem → Solution
Current: `backend/main.py` is a 38-line flat module with a hardcoded `allow_origins=["*"]`,
no settings module, no DB, no migrations, and no tests. `frontend/src/main.jsx` renders a
single `<h1>`. → Desired: `backend/app/{main,config,db}.py` + `app/models/__init__.py`,
Alembic baseline migration, `app/main.py` wired to `settings.CORS_ORIGINS`, pytest suite,
and a frontend with routing (`/login`, `/worker/*`, `/admin/*` placeholders), a fetch-based
API client, and a vitest smoke test. `backend/main.py` stays as a one-line shim so the
Dockerfile's `uvicorn main:app` keeps working unchanged.

## Metadata
- **Complexity**: Medium
- **Source PRD**: N/A
- **PRD Phase**: N/A
- **Estimated Files**: ~20 (create-heavy; two files updated: `Dockerfile`, `README.md`)

---

## UX Design

### Before
```
┌─────────────────────────────┐
│  GET /  → "Full-Stack       │
│  starter" <h1>, nothing     │
│  else                       │
└─────────────────────────────┘
```

### After
```
┌─────────────────────────────┐
│  GET /login   → placeholder │
│  GET /worker/*→ placeholder │
│  GET /admin/* → placeholder │
│  (react-router client-side; │
│  unmatched paths still fall │
│  back to index.html via the │
│  FastAPI SPA catch-all)      │
└─────────────────────────────┘
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| `GET /` | Static `<h1>` | Redirects/renders via router | `App.jsx` owns routing now |
| `GET /api/health` | `{"status":"ok"}`, CORS `*` | Same JSON, CORS restricted to `settings.CORS_ORIGINS` | Behavior-identical payload |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `backend/main.py` | 1-38 | Exact health + SPA behavior to preserve verbatim in `app/main.py` |
| P0 | `Dockerfile` | 1-21 | CMD runs `uvicorn main:app` from `/app/backend` — shim and migration-on-start must not break this |
| P1 | `frontend/src/main.jsx` | 1-7 | Current single-component entry point to replace with router-aware `App.jsx` |
| P1 | `frontend/vite.config.js` | 1-7 | Dev proxy `/api` → `:8000`; keep intact |
| P2 | `frontend/package.json` | 1-8 | No lockfile; `npm install` not `npm ci` — scripts object to extend |
| P2 | `.gitignore` / `.dockerignore` | all | Already exclude `*.db`, `.venv`, `node_modules` — no changes needed |

## External Documentation

| Topic | Source | Key Takeaway |
|---|---|---|
| pydantic-settings | docs.pydantic.dev/latest/concepts/pydantic_settings/ | `BaseSettings` reads env vars + `.env`; use `model_config = SettingsConfigDict(env_file=".env")` |
| SQLAlchemy 2.0 | docs.sqlalchemy.org/en/20/orm/quickstart.html | Use `sqlalchemy.orm.DeclarativeBase`, `create_engine`, `sessionmaker` with `autoflush=False` |
| Alembic | alembic.sqlalchemy.org/en/latest/tutorial.html | `alembic init migrations`; baseline revision has empty `upgrade`/`downgrade` since no models yet |
| Vitest + RTL | vitest.dev / testing-library.com/react | `@testing-library/react` + `jsdom` environment; `vitest.config.js` with `environment: 'jsdom'` |

---

## Patterns to Mirror

### SPA_CATCH_ALL
// SOURCE: backend/main.py:19-37
```python
_DIST = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")
)
if os.path.isdir(_DIST):
    ...
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        ...
```
Must be preserved exactly (just relocated, with `_DIST` path adjusted for the new
`app/` nesting depth: one extra `..`).

### CORS_SETUP
// SOURCE: backend/main.py:9-11
```python
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
```
Replace `allow_origins=["*"]` with `allow_origins=settings.CORS_ORIGINS` (a `list[str]`
parsed from a comma-separated env var, defaulting to `["http://localhost:3000"]` — the
Vite dev port implied by `vite.config.js`'s proxy setup).

### DOCKER_CMD
// SOURCE: Dockerfile:21
```dockerfile
CMD ["sh", "-c", "uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]
```
Extend to run migrations first: `alembic upgrade head && uvicorn main:app ...`, still as a
single `sh -c` string, still from `WORKDIR /app/backend`.

### DEV_PROXY
// SOURCE: frontend/vite.config.js:1-7
```js
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://localhost:8000' } },
});
```
Unchanged — new routes are client-side only, no new dev-server proxy rules needed.

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `backend/app/__init__.py` | CREATE | Make `app` a package |
| `backend/app/config.py` | CREATE | pydantic-settings: `DATABASE_URL`, `JWT_SECRET`, `CORS_ORIGINS` |
| `backend/app/db.py` | CREATE | SQLAlchemy 2.x engine, `SessionLocal`, `get_db` dependency, `Base` |
| `backend/app/models/__init__.py` | CREATE | Empty models package (re-exports `Base` for Alembic's `target_metadata`) |
| `backend/app/main.py` | CREATE | Moved health/SPA logic from `backend/main.py`, CORS now from settings |
| `backend/main.py` | UPDATE | Replace body with `from app.main import app` shim |
| `backend/alembic.ini` | CREATE | Alembic config pointing at `migrations/` |
| `backend/migrations/env.py` | CREATE | Alembic env wired to `settings.DATABASE_URL` and `Base.metadata` |
| `backend/migrations/script.py.mako` | CREATE | Standard Alembic revision template |
| `backend/migrations/versions/0001_baseline.py` | CREATE | Empty baseline revision (no-op upgrade/downgrade) |
| `backend/requirements.txt` | UPDATE | Add sqlalchemy, alembic, pydantic-settings, pyjwt, passlib[bcrypt] |
| `backend/requirements-dev.txt` | CREATE | pytest, httpx, ruff |
| `backend/pyproject.toml` | CREATE | ruff config (line-length, target-version) |
| `backend/tests/__init__.py` | CREATE | Make tests a package |
| `backend/tests/conftest.py` | CREATE | In-memory SQLite engine override + `TestClient` fixture |
| `backend/tests/test_health.py` | CREATE | Asserts `GET /api/health == {"status": "ok"}` |
| `Dockerfile` | UPDATE | CMD runs `alembic upgrade head` before `uvicorn` |
| `frontend/package.json` | UPDATE | Add react-router-dom dep; vitest/@testing-library devDeps; `test`/`lint` scripts |
| `frontend/src/App.jsx` | CREATE | `BrowserRouter` with `/login`, `/worker/*`, `/admin/*` placeholder routes |
| `frontend/src/main.jsx` | UPDATE | Render `<App />` instead of inline `<h1>` |
| `frontend/src/api/client.js` | CREATE | `fetch` wrapper attaching `Authorization: Bearer <token>` |
| `frontend/src/App.test.jsx` | CREATE | Smoke test — renders `App`, asserts login placeholder text |
| `frontend/vitest.config.js` | CREATE | jsdom environment, globals, setup file |
| `frontend/src/test/setup.js` | CREATE | `@testing-library/jest-dom` import for vitest |
| `Makefile` | CREATE | `check` target: ruff, pytest, vitest, vite build |
| `README.md` | UPDATE | Document new layout, `make check`, migrations |

## NOT Building

- No domain models (User, Job, Company, etc.) — later missions own these.
- No real authentication flow — only the JWT secret/config and a bearer-token-aware
  fetch client; no login endpoint yet.
- No CI workflow file (`.github/workflows/*`) — out of scope per mission brief.

---

## Step-by-Step Tasks

### Task 1: Backend package skeleton
- **ACTION**: Create `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/db.py`, `backend/app/models/__init__.py`.
- **IMPLEMENT**: `config.py` defines `Settings(BaseSettings)` with `DATABASE_URL: str = "sqlite:///./2ls.db"`, `JWT_SECRET: str` (no default — required from env, but tests/dev set a fallback via `.env.example`), `CORS_ORIGINS: list[str] = ["http://localhost:3000"]` using a `field_validator` to split a comma-separated env string. `db.py` creates `engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False} if sqlite)`, `SessionLocal`, `Base(DeclarativeBase)`, `get_db()` generator dependency.
- **MIRROR**: N/A — new module, no existing pattern to mirror beyond keeping style consistent with `backend/main.py`'s plain function style.
- **IMPORTS**: `pydantic_settings.BaseSettings`, `sqlalchemy.create_engine`, `sqlalchemy.orm.sessionmaker`, `sqlalchemy.orm.DeclarativeBase`.
- **GOTCHA**: `JWT_SECRET` must have NO committed default/value — pydantic-settings will raise `ValidationError` at import time if unset. Set it in `.env.example` (placeholder only) and in test fixtures via `monkeypatch.setenv`/env in `conftest.py`, and in the Dockerfile via a documented `ENV` comment (not a baked value).
- **VALIDATE**: `python -c "from app.config import settings"` (with `JWT_SECRET` env set) succeeds.

### Task 2: Move main app logic into `app/main.py`, shim `backend/main.py`
- **ACTION**: Create `backend/app/main.py` with the FastAPI app, CORS using `settings.CORS_ORIGINS`, `/api/health`, and the SPA catch-all (adjust `_DIST` path: now two levels up from `app/main.py` to `backend/`, then one more to repo root — i.e. `os.path.join(..., "..", "..", "frontend", "dist")`). Replace `backend/main.py` contents with `from app.main import app`.
- **IMPLEMENT**: Exact same `/api/health` handler and SPA block as today; only CORS and import path change.
- **MIRROR**: `SPA_CATCH_ALL` and `CORS_SETUP` patterns above — copy verbatim except the two noted changes.
- **IMPORTS**: `from app.config import settings` in `app/main.py`.
- **GOTCHA**: Dockerfile's `WORKDIR /app/backend` + `CMD uvicorn main:app` means `main.py` must stay at `backend/main.py` (not moved), and `app` must be importable as a top-level package from that same `WORKDIR` — so `backend/app/` (not `backend/src/app/`).
- **VALIDATE**: `cd backend && uvicorn main:app --port 8001 &` then `curl localhost:8001/api/health` returns `{"status":"ok"}`; kill the process after.

### Task 3: Alembic baseline
- **ACTION**: Create `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/script.py.mako`, `backend/migrations/versions/0001_baseline.py`.
- **IMPLEMENT**: `env.py` imports `from app.config import settings` and `from app.models import Base`, sets `config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)`, `target_metadata = Base.metadata`. Baseline revision has empty `def upgrade(): pass` / `def downgrade(): pass` (no models yet, per non-goals).
- **MIRROR**: Standard `alembic init` output, adapted to point at `app.config.settings` instead of reading `alembic.ini`'s `sqlalchemy.url` directly.
- **IMPORTS**: `alembic`, `sqlalchemy`.
- **GOTCHA**: `alembic.ini`'s `script_location = migrations` is relative to CWD — Dockerfile CMD must `cd /app/backend` (it already does via `WORKDIR`) before running `alembic upgrade head`.
- **VALIDATE**: `cd backend && JWT_SECRET=test rm -f /tmp/test.db && DATABASE_URL=sqlite:////tmp/test.db alembic upgrade head` succeeds with no errors.

### Task 4: CORS wiring + Dockerfile migration step
- **ACTION**: Update `Dockerfile` CMD to run `alembic upgrade head` before `uvicorn`.
- **IMPLEMENT**: `CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]`.
- **MIRROR**: `DOCKER_CMD` pattern above.
- **IMPORTS**: N/A (Dockerfile).
- **GOTCHA**: `JWT_SECRET` has no committed default, so the container needs it set at runtime (`docker run -e JWT_SECRET=...`). Document this in README; acceptance criteria's `docker run` command in the mission brief doesn't pass it, so give `Settings` a safe non-production fallback generated at import time ONLY if absent, with a loud comment — actually simplest and safest: keep `JWT_SECRET` required via env with no default, and set a harmless default in `Dockerfile` via `ENV JWT_SECRET=dev-insecure-change-me` so the mission's bare `docker run -e PORT=8080` smoke test still boots, while any real deployment overrides it. Document this tradeoff in README.
- **VALIDATE**: `docker build -t 2ls . && docker run -e PORT=8080 -p 8080:8080 2ls` then `curl localhost:8080/api/health` and `curl localhost:8080/` both succeed.

### Task 5: Backend deps + lint/test config
- **ACTION**: Update `backend/requirements.txt`; create `backend/requirements-dev.txt`, `backend/pyproject.toml`.
- **IMPLEMENT**: `requirements.txt` adds `sqlalchemy>=2.0`, `alembic>=1.13`, `pydantic-settings>=2.2`, `pyjwt>=2.8`, `passlib[bcrypt]>=1.7`. `requirements-dev.txt` has `pytest>=8.0`, `httpx>=0.27` (needed by `TestClient`), `ruff>=0.4`. `pyproject.toml` has minimal `[tool.ruff]` with `line-length = 100`.
- **MIRROR**: Existing `requirements.txt` style (plain `pkg>=ver` lines, no comments).
- **IMPORTS**: N/A.
- **GOTCHA**: FastAPI's `TestClient` requires `httpx` installed in modern FastAPI/Starlette — must be in dev deps, not just transitively hoped-for.
- **VALIDATE**: `pip install -r requirements.txt -r requirements-dev.txt` succeeds in a clean venv.

### Task 6: Backend tests
- **ACTION**: Create `backend/tests/__init__.py`, `backend/tests/conftest.py`, `backend/tests/test_health.py`.
- **IMPLEMENT**: `conftest.py` sets `os.environ.setdefault("JWT_SECRET", "test-secret")` and `os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")` BEFORE importing `app.main` (module-level, before other imports, since settings are read at import time), then provides a `client` fixture yielding `TestClient(app)`. `test_health.py` does `def test_health(client): assert client.get("/api/health").json() == {"status": "ok"}`.
- **MIRROR**: `TEST_STRUCTURE` — no prior test in repo, so this establishes the pattern for future missions (fixture-based `TestClient`, env set in `conftest.py`).
- **IMPORTS**: `fastapi.testclient.TestClient`.
- **GOTCHA**: Settings are instantiated once at module import (`settings = Settings()` in `config.py`) — env vars must be set via `conftest.py` at collection time, before `app.main` (which imports `app.config`) is first imported anywhere in the test session.
- **VALIDATE**: `cd backend && pytest -q` → 1 passed.

### Task 7: Frontend routing + API client
- **ACTION**: Add `react-router-dom` to `frontend/package.json` deps. Create `frontend/src/App.jsx`, `frontend/src/api/client.js`. Update `frontend/src/main.jsx`.
- **IMPLEMENT**: `App.jsx` wraps `BrowserRouter` + `Routes` with `<Route path="/login" element={<div>Login</div>} />`, `<Route path="/worker/*" element={<div>Worker</div>} />`, `<Route path="/admin/*" element={<div>Admin</div>} />`, and a default `<Route path="/" element={<Navigate to="/login" replace />} />`. `client.js` exports `apiFetch(path, opts)` that reads a token (e.g. `localStorage.getItem('token')`) and sets `Authorization: Bearer <token>` if present, prefixing `path` with `/api`. `main.jsx` renders `<App />` instead of the inline `<h1>` component.
- **MIRROR**: Keep `main.jsx`'s existing `createRoot(...).render(...)` pattern — only the rendered component changes.
- **IMPORTS**: `react-router-dom`'s `BrowserRouter`, `Routes`, `Route`, `Navigate`.
- **GOTCHA**: Vite dev server has no history-API fallback configured — client-side routes work via `BrowserRouter` in dev (Vite serves `index.html` for unknown paths by default) and in prod via the FastAPI SPA catch-all (Task 2), so no extra config needed either side.
- **VALIDATE**: `npm run dev` then manually hit `/login`, `/worker/x`, `/admin/y` in a browser — all render without 404.

### Task 8: Frontend test harness
- **ACTION**: Add `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom` to `frontend/package.json` devDeps. Create `frontend/vitest.config.js`, `frontend/src/test/setup.js`, `frontend/src/App.test.jsx`. Add `"test": "vitest"` and `"lint": "echo 'no linter configured'"` scripts (or a minimal eslint config if time allows — mission only requires a `lint` script to exist for `scripts/check.sh`/Makefile to call).
- **IMPLEMENT**: `vitest.config.js` sets `test: { environment: 'jsdom', setupFiles: ['./src/test/setup.js'], globals: true }`. `setup.js` does `import '@testing-library/jest-dom/vitest'`. `App.test.jsx` renders `<App />` wrapped appropriately (it already contains its own `BrowserRouter`) and asserts the default route redirects to login content.
- **MIRROR**: N/A — establishes the frontend test pattern.
- **IMPORTS**: `@testing-library/react`'s `render`, `screen`; `vitest`'s `describe`/`it`/`expect`.
- **GOTCHA**: `BrowserRouter` in a test needs `jsdom` environment (not `node`) — must be set in `vitest.config.js`, not just per-file.
- **VALIDATE**: `cd frontend && npm test -- --run` → smoke test passes.

### Task 9: Root check script + README
- **ACTION**: Create `Makefile` (or `scripts/check.sh`) at repo root. Update `README.md`.
- **IMPLEMENT**: `Makefile` with a `check` target running, in order: `cd backend && ruff check . && pytest -q`, then `cd frontend && npm test -- --run && npm run build`. README documents the new `backend/app/` layout, how to run migrations (`cd backend && alembic upgrade head`), `make check`, and the `JWT_SECRET` env requirement.
- **MIRROR**: README's existing terse command-block style.
- **IMPORTS**: N/A.
- **GOTCHA**: `make` targets must `cd` per-step (each recipe line runs in its own shell) or use `&&` within one line — use one-line `&&`-joined recipes per target to avoid directory-reset surprises.
- **VALIDATE**: `make check` from repo root runs end-to-end and exits 0.

---

## Testing Strategy

### Unit Tests
| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `test_health.py::test_health` | `GET /api/health` | `{"status": "ok"}`, 200 | No |
| `App.test.jsx` smoke test | Render `<App />` at `/` | Redirects to `/login`, shows placeholder text | No |

### Edge Cases Checklist
- [x] Empty input — N/A, no request bodies yet
- [ ] Maximum size input — N/A
- [ ] Invalid types — N/A, no domain models
- [ ] Concurrent access — N/A, in-memory SQLite per-test is isolated
- [ ] Network failure — N/A, no external calls
- [x] Permission denied — N/A, no auth routes yet; `JWT_SECRET` required-env-var failure mode documented as a GOTCHA

---

## Validation Commands

### Static Analysis
```bash
cd backend && ruff check .
```
EXPECT: Zero lint errors

### Unit Tests
```bash
cd backend && pytest -q
cd frontend && npm test -- --run
```
EXPECT: All tests pass

### Full Test Suite
```bash
make check
```
EXPECT: No regressions across backend + frontend

### Database Validation
```bash
cd backend && rm -f /tmp/m1-check.db && DATABASE_URL=sqlite:////tmp/m1-check.db JWT_SECRET=test alembic upgrade head
```
EXPECT: Baseline revision applies cleanly to a fresh DB

### Browser Validation
```bash
docker build -t 2ls . && docker run -e PORT=8080 -p 8080:8080 2ls
curl localhost:8080/api/health
curl -I localhost:8080/
```
EXPECT: Health JSON returns ok; `/` serves the built SPA (200, `text/html`)

### Manual Validation
- [ ] `docker run` logs show `alembic upgrade head` completing before uvicorn starts
- [ ] No secrets in git history (`git log -p -- '*.env' 'backend/app/config.py'` shows no real secret values)

---

## Acceptance Criteria
- [ ] All tasks completed
- [ ] All validation commands pass
- [ ] Tests written and passing
- [ ] No lint errors
- [ ] `cd backend && ruff check . && pytest -q` passes; `test_health.py` asserts `GET /api/health == {"status":"ok"}`
- [ ] `cd frontend && npm test -- --run && npm run build` passes
- [ ] `cd backend && alembic upgrade head` succeeds on a fresh SQLite DB
- [ ] `docker build -t 2ls . && docker run -e PORT=8080 -p 8080:8080 2ls` serves `/` (SPA) and `/api/health`
- [ ] No secrets committed; CORS no longer a wildcard

## Completion Checklist
- [ ] Code follows discovered patterns (SPA catch-all, CORS setup preserved verbatim where behavior must not change)
- [ ] Error handling matches codebase style (minimal, FastAPI defaults)
- [ ] Logging follows codebase conventions (none exists yet — out of scope)
- [ ] Tests follow test patterns established in this plan
- [ ] No hardcoded values (DB URL, JWT secret, CORS origins all env-driven)
- [ ] Documentation updated (README.md)
- [ ] No unnecessary scope additions (no domain models, no auth endpoints)
- [ ] Self-contained — no questions needed during implementation

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `JWT_SECRET` required-env breaks the mission's bare `docker run -e PORT=8080` smoke test | Medium | High (acceptance criteria fail) | Set a non-production `ENV JWT_SECRET=dev-insecure-change-me` in the Dockerfile only; never commit a real secret, document override for real deployments |
| Alembic `env.py` import path mismatch when run from `backend/` vs `backend/migrations/` | Low | Medium | Keep `script_location = migrations` relative, always run alembic from `backend/` (matches Dockerfile `WORKDIR`) |
| `npm install` without a lockfile produces different versions across runs | Low | Low | Pin minor versions loosely in `package.json`; acceptable given scaffold stage, no lockfile committed per existing `Dockerfile` comment |

## Notes
No remote git repository is configured for this project (`git remote -v` is empty), so
the PR step of this pipeline is executed as a local squash-merge into `main` rather than
a GitHub pull request — recorded explicitly in the implementation report's deviations.
