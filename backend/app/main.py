import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import auth

app = FastAPI(title="API")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# API routers. Must stay above the SPA catch-all below so /api/* matches first.
app.include_router(auth.router)


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
