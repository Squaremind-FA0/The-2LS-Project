# Full-Stack (React + FastAPI)

## Layout

- `backend/app/` — the FastAPI package: `main.py` (app + routes), `config.py`
  (pydantic-settings), `db.py` (SQLAlchemy engine/session), `models/` (ORM models).
  `backend/main.py` is a one-line shim (`from app.main import app`) so
  `uvicorn main:app` keeps working.
- `backend/migrations/` — Alembic migrations. Run `alembic upgrade head` from
  `backend/` before starting the app against a new database.
- `frontend/src/` — React app with `react-router-dom` routes (`/login`,
  `/worker/*`, `/admin/*` placeholders) and `api/client.js`, a `fetch` wrapper
  that attaches a bearer token from `localStorage`.

## Environment

Backend settings (see `backend/.env.example`):

- `DATABASE_URL` — defaults to `sqlite:///./2ls.db`
- `JWT_SECRET` — **required**, no committed default. Set it in your shell or
  `.env` for local dev; the Dockerfile sets a dev-only fallback
  (`dev-insecure-change-me`) so a bare `docker run` still boots — override it
  for any real deployment.
- `CORS_ORIGINS` — comma-separated list, defaults to `http://localhost:3000`

## Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # edit JWT_SECRET
alembic upgrade head
uvicorn main:app --reload
```

Lint and test:

```bash
cd backend && ruff check . && pytest -q
```

## Frontend

```bash
cd frontend && npm install && npm run dev
```

Lint, test, and build:

```bash
cd frontend && npm run lint && npm test -- --run && npm run build
```

## Full check

```bash
make check
```

## Single-origin (docker / preview)

```bash
docker build -t app . && docker run -e JWT_SECRET=your-secret -p 8080:8080 app
```

The container runs `alembic upgrade head` before starting uvicorn.
