"""Authentication / authorization dependencies.

Every ``/api/*`` route except ``PUBLIC_API_PATHS`` must depend on
``require_role(...)`` and read tenant data through ``get_tenant_db``.
``tests/test_route_audit.py`` enforces both.
"""

from collections.abc import Callable, Iterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.service import get_user_for_token
from app.db import get_db
from app.models import Role, User
from app.security.tokens import decode_access_token
from app.tenancy import TENANT_INFO_KEY

PUBLIC_API_PATHS: frozenset[str] = frozenset({"/api/health", "/api/auth/login"})
ALL_ROLES: tuple[Role, ...] = (Role.company_admin, Role.dispatcher, Role.worker)
REQUIRE_ROLE_MARKER = "__require_role__"

bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to an active user, or raise 401.

    Role and company are taken from the database row, never from the token.
    """
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized()
    try:
        claims = decode_access_token(creds.credentials)
        user_id = int(claims["sub"])
        token_version = claims["tv"]
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized() from None
    if not isinstance(token_version, int) or isinstance(token_version, bool):
        raise _unauthorized()

    user = get_user_for_token(db, user_id)
    if user is None or not user.is_active or user.token_version != token_version:
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
