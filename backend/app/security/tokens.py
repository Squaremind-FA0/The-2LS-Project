"""HS256 access tokens.

Claims are deliberately minimal: ``sub`` (user id as a string), ``tv``
(the user's token_version), ``iat`` and ``exp``. Role and company are never
put in the token; they are re-read from the database on every request so a
role change or deactivation takes effect immediately.
"""

from datetime import UTC, datetime, timedelta

import jwt

from app.config import settings

ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ["sub", "tv", "iat", "exp"]


def create_access_token(user_id: int, token_version: int, *, now: datetime | None = None) -> str:
    issued_at = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "tv": token_version,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN),
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and verify ``token``. Raises ``jwt.InvalidTokenError`` (or a
    subclass such as ``ExpiredSignatureError``) on any failure."""
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[ALGORITHM],
        options={"require": _REQUIRED_CLAIMS},
    )
