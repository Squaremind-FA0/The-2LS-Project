.PHONY: check backend-check frontend-check

check: backend-check frontend-check

backend-check:
	cd backend && . .venv/bin/activate 2>/dev/null; ruff check . && pytest -q

frontend-check:
	cd frontend && npm test -- --run && npm run build
