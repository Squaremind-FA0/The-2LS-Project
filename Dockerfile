# Single-origin image: build the Vite frontend, then serve API + built UI
# from one FastAPI process on ${PORT:-8080}.

FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
# npm install (not npm ci): the starter ships no package-lock.json.
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=frontend-build /app/frontend/dist frontend/dist

WORKDIR /app/backend
EXPOSE 8080
# Dev-only fallback so a bare `docker run` boots; override JWT_SECRET for any real deployment.
ENV JWT_SECRET=dev-insecure-change-me
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080}"]
