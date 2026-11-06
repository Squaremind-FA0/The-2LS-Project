import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.roster.router import router as roster_router
from app.routers import auth, dispatch, jobs, me, payruns, shifts, workers, ws
from app.services import dispatch as dispatch_service

logger = logging.getLogger(__name__)


async def _dispatch_sweep_loop() -> None:
    """Bounded background sweep: expire stale offers and re-offer, per
    company, every DISPATCH_SWEEP_INTERVAL_S. Sleeps first, so a short-lived
    app (e.g. a test's `with TestClient(app):` block) never runs one. Not a
    substitute for correctness: GET /api/offers/pending and the accept/
    decline guards already treat a past-expires_at offer as inert on their
    own, so a missed or delayed tick, or a restart, is never unsafe -- this
    loop only makes expiry and re-offering happen without a poller calling
    POST /api/dispatch/sweep.
    """
    while True:
        await asyncio.sleep(settings.DISPATCH_SWEEP_INTERVAL_S)
        try:
            await run_in_threadpool(dispatch_service.sweep_all_companies)
        except Exception:
            logger.exception("dispatch sweep loop: iteration failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_dispatch_sweep_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="API", lifespan=lifespan)
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
app.include_router(payruns.router)
app.include_router(roster_router)


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
