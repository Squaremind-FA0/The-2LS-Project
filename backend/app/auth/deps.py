"""Authentication / authorization dependencies.

Every ``/api/*`` route except ``PUBLIC_API_PATHS`` must depend on
``require_role(...)`` and read tenant data through ``get_tenant_db``.
``tests/test_route_audit.py`` enforces both.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import jwt
from fastapi import (
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.service import get_user_for_token
from app.config import settings
from app.db import get_db
from app.models import Role, User
from app.security.tokens import decode_access_token
from app.tenancy import TENANT_INFO_KEY

PUBLIC_API_PATHS: frozenset[str] = frozenset({"/api/health", "/api/auth/login"})
ALL_ROLES: tuple[Role, ...] = (Role.company_admin, Role.dispatcher, Role.worker)
REQUIRE_ROLE_MARKER = "__require_role__"
REQUIRE_WS_ROLE_MARKER = "__require_ws_role__"
WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN = 4403

bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def user_for_token(db: Session, token: str) -> User | None:
    """The active user ``token`` names, or None. The one token check: HTTP and
    WebSocket auth both call it. Role and company come from the database row,
    never from the token."""
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
        token_version = claims["tv"]
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(token_version, int) or isinstance(token_version, bool):
        return None

    user = get_user_for_token(db, user_id)
    if user is None or not user.is_active or user.token_version != token_version:
        return None
    return user


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to an active user, or raise 401."""
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized()
    user = user_for_token(db, creds.credentials)
    if user is None:
        raise _unauthorized()
    return user


def require_role(*roles: Role) -> Callable[..., User]:
    """Dependency factory: 403 unless the current user has one of ``roles``."""
    if not roles:
        raise ValueError("require_role() needs at least one role")
    allowed = frozenset(roles)

    def _require_role(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    setattr(_require_role, REQUIRE_ROLE_MARKER, tuple(roles))
    return _require_role


def current_company_id(user: User = Depends(get_current_user)) -> int:
    return user.company_id


def get_tenant_db(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Iterator[Session]:
    """The request session, scoped to the caller's company.

    FastAPI caches ``get_db`` per request, so this is the same session that
    ``get_current_user`` used. The tenant context is removed again on exit.
    """
    db.info[TENANT_INFO_KEY] = user.company_id
    try:
        yield db
    finally:
        db.info.pop(TENANT_INFO_KEY, None)


@dataclass(frozen=True)
class WsPrincipal:
    user_id: int
    company_id: int
    role: Role
    token: str  # kept to re-validate on every heartbeat


def ws_principal(db: Session, token: str) -> WsPrincipal | None:
    """Sync: run in the threadpool. Always closes ``db``: on SQLite every
    transaction is BEGIN IMMEDIATE (app/db.py), so a session left open would
    hold the database's single write lock for as long as the socket lives."""
    try:
        user = user_for_token(db, token)
        return None if user is None else WsPrincipal(user.id, user.company_id, user.role, token)
    finally:
        db.close()


async def _first_message_token(websocket: WebSocket) -> str | None:
    """The token from a ``{"type":"auth","token":...}`` text frame, else None.
    ``receive()``, not ``receive_text()``: a binary frame raises KeyError there."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))  # see app/main.py
    text = message.get("text")
    if text is None:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("type") != "auth":
        return None
    token = body.get("token")
    return token if isinstance(token, str) and token else None


def require_ws_role(*roles: Role) -> Callable[..., object]:
    """WebSocket counterpart of ``require_role``: accept, then first-message auth.

    Accept comes first: closing before accept is an HTTP 403 with no close code
    on a real server. ``tests/test_route_audit.py`` requires this on every
    WebSocket route under /api.
    """
    if not roles:
        raise ValueError("require_ws_role() needs at least one role")
    allowed = frozenset(roles)

    async def _require_ws_role(websocket: WebSocket, db: Session = Depends(get_db)) -> WsPrincipal:
        await websocket.accept()
        try:
            token = await asyncio.wait_for(
                _first_message_token(websocket), settings.WS_AUTH_TIMEOUT_S
            )
        except TimeoutError:
            token = None
        principal = await run_in_threadpool(ws_principal, db, token) if token else None
        if principal is None:
            raise WebSocketException(WS_CLOSE_UNAUTHENTICATED, "Not authenticated")
        if principal.role not in allowed:
            raise WebSocketException(WS_CLOSE_FORBIDDEN, "Insufficient role")
        return principal

    setattr(_require_ws_role, REQUIRE_WS_ROLE_MARKER, tuple(roles))
    return _require_ws_role
