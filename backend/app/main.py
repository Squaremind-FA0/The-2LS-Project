import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import auth, dispatch, jobs, me, payruns, shifts, time_entries, workers, ws

app = FastAPI(title="API")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(WebSocketDisconnect)
async def _client_left(websocket: WebSocket, exc: WebSocketDisconnect) -> None:
    """A client leaving is not a server error: without this, leaving during
    auth (or closing a socket that has just gone) logs an ASGI traceback."""


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# API routers — keep every include_router call in this one block, above the
# SPA catch-all below so /api/* matches first. Every /api/* route outside
# app.auth.deps.PUBLIC_API_PATHS must depend on require_role(...)/require_ws_role(...)
# and use get_tenant_db; tests/test_route_audit.py enforces that.
app.include_router(auth.router)
app.include_router(me.router)
app.include_router(workers.router)
app.include_router(jobs.router)
app.include_router(dispatch.router)
app.include_router(ws.router)
app.include_router(shifts.router)
app.include_router(time_entries.router)
app.include_router(payruns.router)


# Single-origin production: when the built frontend exists (docker image or
# preview), this process serves it too. In dev, vite serves the UI on :3000
# and proxies /api here — the block below simply never activates.
_DIST = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "dist")
)
if os.path.isdir(_DIST):
    _ASSETS = os.path.join(_DIST, "assets")
    if os.path.isdir(_ASSETS):
        app.mount("/assets", StaticFiles(directory=_ASSETS), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str) -> FileResponse:
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404)
    if not os.path.isdir(_DIST):
        raise HTTPException(status_code=404)
    candidate = os.path.normpath(os.path.join(_DIST, full_path))
    if full_path and candidate.startswith(_DIST + os.sep) and os.path.isfile(candidate):
        return FileResponse(candidate)
    index_path = os.path.join(_DIST, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404)
    return FileResponse(index_path)
