# CLAUDE.md

## Stack

- **Backend:** FastAPI (`backend/main.py`), served by Uvicorn. Single
  process serves `/api/*` and, when `frontend/dist` exists, the built SPA
  (see the static-file block at the bottom of `main.py`).
- **Frontend:** React 19 + Vite (`frontend/src/main.jsx`, `frontend/index.html`).
  Dev server proxies `/api` to the backend; production build is a static
  `dist/` bundle served by the backend.

## Run

```bash
# backend (http://localhost:8000)
cd backend && pip install -r requirements.txt && uvicorn main:app --reload

# frontend (http://localhost:3000, proxies /api to the backend)
cd frontend && npm install && npm run dev

# single-origin production image
docker build -t app . && docker run -p 8080:8080 app
```

## Test / validate

```bash
cd backend && pytest -q
cd backend && ruff check .
cd frontend && npm run build
cd frontend && npm test        # once a frontend test runner is added
```

These are the exact commands every PRP's Validation gates section should use
unless a PRP introduces a new gate (e.g. a migration check) — in that case
add it here too, so it stays the project-wide source of truth.

## Feature work: plan → implement → pr

All feature and bugfix work goes through the PRP (Product Requirement
Prompt) pipeline — nothing gets implemented without a PRP first. See
`PRPs/README.md` for the full writeup. Short version:

1. `/prp-plan <gap-id> <one-line goal>` — writes `PRPs/<gap-id>-<slug>.md`,
   no code changes, commits only the PRP file on branch `prp/<gap-id>`.
2. `/prp-implement PRPs/<gap-id>-<slug>.md` — executes the PRP's task list
   test-first, one commit per task, running the validation gates above
   after every task.
3. `/prp-pr <gap-id>` — rebases `prp/<gap-id>` on `main`, runs the full
   validation gates, and merges (PR + squash-merge if a remote exists,
   local `--no-ff` merge into `main` if not).

## Branch naming

- `prp/<gap-id>` — a PRP's planning + implementation branch, e.g. `prp/g3`.
- `chore/<slug>` — repo-maintenance work that isn't a PRP (tooling, CI,
  formatting), e.g. this pipeline's own branch, `chore/prp-pipeline`.

## Law: no secrets in the repo

Never commit API keys, tokens, passwords, or `.env` files. `.gitignore`
already excludes `.env`, `.env.local`, and `*.db` — keep it that way, and
never add a secret value directly into a committed file, including PRPs,
commit messages, or comments.
