# PRP: g32 — enable the first end-to-end PRP validation workflow

## Goal
Make the repository's documented validation gates executable by declaring the
backend test/lint tools and adding a frontend Vitest runner. The initial PRP
pipeline run will leave repeatable tests that prove those two tool contracts.

## Non-goals
- Change the FastAPI health API or the visible React starter screen.
- Add application features, browser E2E coverage, CI configuration, or a
  frontend component-testing library.
- Pin unrelated production dependency versions.

## Context
- `CLAUDE.md:25-36` declares `pytest -q`, `ruff check .`, `npm run build`, and
  a future `npm test` as the project validation gates.
- `backend/requirements.txt:1-2` currently declares only FastAPI and Uvicorn,
  so neither backend test nor lint tooling is installed from project
  dependencies.
- `frontend/package.json:1-8` has only `dev` and `build` scripts and does not
  declare a test runner.
- `backend/main.py:14-16` exposes the existing health endpoint; this PRP does
  not alter it.
- `frontend/src/main.jsx:3-5` contains the unchanged starter UI; the frontend
  test is runner smoke coverage rather than a UI behavior change.

## Data model / API contracts
No data model or API changes.

## Task list
1. `backend/tests/test_quality_tooling.py` and `frontend/src/test/smoke.test.js`
   — write failing quality-tooling contract tests: backend requirements must
   declare pytest and ruff, and `npm test` must execute the frontend smoke test.
   Observe RED before installing either tool.
2. `backend/requirements.txt`, `frontend/package.json`, `frontend/package-lock.json`,
   and `frontend/vitest.config.js` — declare pytest and ruff, install/configure
   Vitest, and add the `test` script so the tests from task 1 pass.

## Validation gates
Exact project-wide commands from `CLAUDE.md`:
```bash
cd backend && pytest -q
cd backend && ruff check .
cd frontend && npm run build
cd frontend && npm test
```

## Acceptance criteria
- `pip install -r backend/requirements.txt` installs pytest and ruff, and
  `cd backend && ruff check .` succeeds.
- `cd backend && pytest -q` succeeds and includes a regression assertion that
  requirements declare both quality tools.
- `cd frontend && npm test` runs a Vitest test and succeeds.
- `cd frontend && npm run build` succeeds with the test tooling installed.
- The health endpoint response and starter-screen text are unchanged.

## Risks
- An unconstrained frontend tool version could require a newer Node release;
  verify with the repository's current Node 26 runtime.
- A broad ruff default could flag the starter FastAPI style; run it before
  merging and make only focused lint-compatible changes if necessary.
- Lockfile changes must remain limited to the declared Vitest dependency tree.

## Rollback
Revert the local merge commit for `g32` from `main` (or revert the two PRP task
commits before it is merged). If the branch has not been merged, delete
`prp/g32`; no data migration or runtime data rollback is needed.
