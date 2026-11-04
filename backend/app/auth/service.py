"""Pre-authentication user lookups.

This module is the ONLY production code allowed to use
``execution_options(skip_tenant_filter=True)``. Before a caller is
authenticated there is no tenant context to scope by: login has only an
email, and token validation has only a user id. Each bypass is set on one
statement, never on the session, so nothing else in the request inherits it.
Keep it that way: ``grep -rn skip_tenant_filter backend/app`` should find
only this file.
"""

import secrets
from functools import cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.security.passwords import hash_password, verify_password


def normalize_email(email: str) -> str:
    return email.strip().lower()


@cache
def _dummy_hash() -> str:
    # A real Argon2 hash of a random value, computed once, so the
    # unknown-email path costs the same as a wrong password.
    return hash_password(secrets.token_urlsafe(32))


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the active user matching ``email`` / ``password``, else None.

    Unknown email, wrong password and inactive user are indistinguishable
    to the caller, and each runs exactly one Argon2 verify.
    """
    stmt = (
        select(User)
        .where(User.email == normalize_email(email))
        .execution_options(skip_tenant_filter=True)  # pre-auth: no tenant yet
    )
    user = db.scalars(stmt).one_or_none()
    if user is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


def get_user_for_token(db: Session, user_id: int) -> User | None:
    """Load the user named by a verified token's ``sub`` claim."""
    stmt = (
        select(User)
        .where(User.id == user_id)
        .execution_options(skip_tenant_filter=True)  # pre-auth: tenant comes from this row
    )
    return db.scalars(stmt).one_or_none()
