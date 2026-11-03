# PRP: G1 — Foundation

## Goal

Restructure the backend into a scalable FastAPI package with proper configuration, database abstraction, and migration management. Establish a test harness using pytest with transactional fixtures and configure frontend testing with Vitest. The result is a production-ready foundation for domain-driven development: `/api/health` endpoint working, SPA fallback intact, database migrations runnable via Alembic, and all validation gates passing.

## Non-goals

- Any domain models (User, Job, Company, etc.) — those belong in later PRPs
- API endpoints beyond `/api/health` 
- Database schema beyond the baseline empty migration
- Authentication/authorization implementation
- Frontend business logic or components beyond smoke test setup

## Context

**Current backend structure** (`backend/main.py:1-40`): FastAPI app with CORS middleware, `/api/health` endpoint, and conditional SPA serving. Will be refactored into `backend/app/` package.

**Backend requirements** (`backend/requirements.txt`): fastapi, uvicorn, sqlalchemy>=2.0, alembic>=1.13, pydantic-settings>=2.2, pytest>=8.0, httpx>=0.27 (needed for TestClient per [[dependency_fact]]).

**Frontend stack** (`frontend/package.json:1-28`): React 19, Vite 6, Vitest 2.1.0, @testing-library/react 16.0.0, @testing-library/jest-dom 6.5.0.

**Dockerfile** (`Dockerfile:19-23`): CMD runs `alembic upgrade head && uvicorn main:app` — must be updated to point to `app.main:app`.

**Test patterns** (`backend/tests/conftest.py:1-14`): TestClient fixture exists; needs transactional DB isolation per test.

**Alembic config** (`backend/migrations/env.py:6-7`, `backend/migrations/versions/0001_baseline.py`): Already initialized with empty baseline revision. env.py correctly imports from app.config and app.models.

## Data model / API contracts

No data model or API changes. Contracts already established:

```
GET /api/health
  response: { status: "ok" }
```

Database engine initialized but schema empty (baseline migration has no operations).

## Task list

1. **backend/tests/conftest.py** — write failing test for transactional DB isolation
   - Test should verify that two consecutive tests with DB operations do not see each other's data
   - Requires pytest fixtures that roll back after each test

2. **backend/app/db.py** — implement transactional fixture and session dependency
   - Add `transactional_db` pytest fixture that wraps test in a transaction and rolls back after
   - Update `get_db()` to work with test fixtures (no change needed if using session-scoped fixture)
   - Keep existing engine/SessionLocal/Base/get_db() for production

3. **backend/tests/test_health.py** — verify health endpoint still passes with new DB fixture

4. **backend/pyproject.toml** — ruff configuration is already present; verify it covers E, F, I rules and target-version=py312

5. **backend/main.py** — update Dockerfile CMD reference in comment if present, verify import of `from app.main import app`

6. **Dockerfile** — change CMD from `uvicorn main:app` to `uvicorn app.main:app`

7. **frontend/vitest.config.js** — verify Vitest config includes jsdom, setupFiles pointing to src/test/setup.js, and globals:true

8. **frontend/src/test/setup.js** — verify imports @testing-library/jest-dom for DOM matchers

9. **backend/migrations/alembic.ini** — verify sqlalchemy.url placeholder and relative_to config (already set by init)

10. **backend && pytest -q** — run full backend test suite; must pass with 2+ tests (health + db isolation)

11. **cd frontend && npm test** — run frontend smoke tests; must pass

12. **cd frontend && npm run build** — verify Vite build succeeds

13. **cd backend && ruff check .** — verify no lint errors

14. **cd backend && alembic upgrade head** — verify baseline migration runs without error

15. **docker build -t app . && docker run -p 8080:8080 app** — verify image builds and container boots (uvicorn serves on 8080)

## Validation gates

Run after each task that modifies code; run again before merge:

```bash
cd backend && pytest -q
cd backend && ruff check .
cd backend && alembic upgrade head
cd frontend && npm test
cd frontend && npm run build
docker build -t app .
```

## Acceptance criteria

- ✓ Backend refactored into `backend/app/` package with main.py, config.py, db.py, models/, plus api/, schemas/, services/ directories (can be empty)
- ✓ `app/config.py` uses pydantic-settings; DATABASE_URL defaults to `sqlite:///./2ls.db`; CORS_ORIGINS defaults to `["http://localhost:3000"]`
- ✓ `app/db.py` defines SQLAlchemy 2.0 engine, SessionLocal, Base, get_db() dependency
- ✓ `/api/health` endpoint returns `{"status": "ok"}` (HTTP 200)
- ✓ SPA fallback serves `frontend/dist/index.html` for non-API paths when `frontend/dist/` exists
- ✓ CORS restricted to configured origins (no `allow_origins=["*"]`)
- ✓ `backend/tests/conftest.py` provides TestClient and transactional DB fixtures
- ✓ pytest test suite passes: `cd backend && pytest -q` returns exit 0
- ✓ Alembic baseline migration initialized; `alembic upgrade head` succeeds
- ✓ `cd backend && ruff check .` exits 0 (E, F, I rules, target-version=py312)
- ✓ `cd frontend && npm test` passes all tests (smoke test + others)
- ✓ `cd frontend && npm run build` builds without warnings
- ✓ `docker build -t app . && docker run -e JWT_SECRET=test -p 8080:8080 app` builds and boots; `/api/health` returns 200

## Risks

- **SQLAlchemy session in tests**: transactional fixtures may not work if app code opens new sessions outside the dependency injection. Mitigate by ensuring all DB access flows through `get_db()` dependency.
- **Alembic env.py imports**: Changing imports in env.py or app/models can break `alembic upgrade`. Mitigation: test Alembic locally after any migration-related edits.
- **Dockerfile layer caching**: If npm install layer doesn't cache properly, rebuilds will be slow. Not a blocker for G1 but worth noting.
- **Frontend test discovery**: Vitest must find `.test.jsx` files via glob. If files are named differently, tests won't run. Verify naming convention.

## Rollback

If G1 lands broken and must be reverted:

```bash
git checkout main
git reset --hard HEAD~1  # or git revert <commit-sha>
# If migrations were run to production:
cd backend
alembic downgrade -1  # or alembic downgrade 0001, or specify base
```

If G1 was merged to main but not deployed:

```bash
git log --oneline | grep "G1\|Foundation"  # find the commit
git revert <commit-sha> --no-edit
git push
```

For production rollback, reverse the Docker image tag and redeploy the prior container.
