"""Test-data helpers.

Tenant-scoped rows can only be flushed inside a tenant context (the
before_flush listener fails closed), so helpers set
``session.info["company_id"]`` while they work.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import cache

from sqlalchemy.orm import Session

from app.models import Company, Role, User, WorkerProfile
from app.security.passwords import hash_password
from app.security.tokens import create_access_token


@contextmanager
def tenant_context(session: Session, company_id: int):
    previous = session.info.get("company_id")
    session.info["company_id"] = company_id
    try:
        yield session
    finally:
        if previous is None:
            session.info.pop("company_id", None)
        else:
            session.info["company_id"] = previous


# --- two-company fixture data ---------------------------------------------------------------



ADMIN_PASSWORD = "admin-password-for-tests"
WORKER_PASSWORD = "worker-password-for-tests"
DISPATCHER_PASSWORD = "dispatcher-password-for-tests"


@cache
def cached_hash(plain: str) -> str:
    """Argon2 is deliberately slow; hash each known test password once."""
    return hash_password(plain)


@dataclass(frozen=True)
class UserRef:
    id: int
    email: str
    password: str
    role: Role
    company_id: int
    token_version: int = 0


@dataclass(frozen=True)
class CompanyRef:
    id: int
    name: str
    admin: UserRef
    worker: UserRef
    dispatcher: UserRef


def make_user(
    db: Session, company_id: int, email: str, password: str, role: Role, **profile
) -> UserRef:
    with tenant_context(db, company_id):
        user = User(
            email=email,
            password_hash=cached_hash(password),
            full_name=email.split("@")[0].replace(".", " ").title(),
            role=role,
        )
        db.add(user)
        if role is Role.worker:
            db.add(
                WorkerProfile(
                    user=user,
                    trades=profile.get("trades", ["plumbing"]),
                    hourly_rate=profile.get("hourly_rate", Decimal("55.00")),
                )
            )
        db.flush()
        return UserRef(user.id, email, password, role, company_id, user.token_version)


def make_company(db: Session, key: str, name: str) -> CompanyRef:
    company = Company(name=name)
    db.add(company)
    db.flush()
    return CompanyRef(
        id=company.id,
        name=name,
        admin=make_user(db, company.id, f"admin@{key}.test", ADMIN_PASSWORD, Role.company_admin),
        worker=make_user(
            db, company.id, f"worker@{key}.test", WORKER_PASSWORD, Role.worker, trades=[key]
        ),
        dispatcher=make_user(
            db, company.id, f"dispatch@{key}.test", DISPATCHER_PASSWORD, Role.dispatcher
        ),
    )


def auth_header(user: UserRef, **token_kwargs) -> dict[str, str]:
    token = create_access_token(user.id, user.token_version, **token_kwargs)
    return {"Authorization": f"Bearer {token}"}
