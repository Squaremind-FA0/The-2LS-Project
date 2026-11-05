"""Worker accounts. Every function takes a tenant-scoped session
(``get_tenant_db``); company_id is stamped by the before_flush listener."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager

from app.models import Role, User, WorkerProfile
from app.schemas.users import WorkerCreateRequest
from app.security.passwords import hash_password


class EmailAlreadyRegistered(Exception):
    pass


def create_worker(db: Session, payload: WorkerCreateRequest) -> User:
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.initial_password),
        full_name=payload.full_name,
        role=Role.worker,
    )
    db.add(user)
    db.add(WorkerProfile(user=user, trades=list(payload.trades), hourly_rate=payload.hourly_rate))
    try:
        # Emails are globally unique, but this session can only see its own
        # company, so the unique index is the authoritative duplicate check.
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "email" in str(exc.orig).lower():
            raise EmailAlreadyRegistered from exc
        raise
    return user


def _workers_query():
    return (
        select(User)
        .join(User.worker_profile)
        .options(contains_eager(User.worker_profile))
        .where(User.role == Role.worker)
    )


def list_workers(db: Session) -> Sequence[User]:
    return db.scalars(_workers_query().order_by(User.id)).all()


def get_worker(db: Session, user_id: int) -> User | None:
    return db.scalars(_workers_query().where(User.id == user_id)).one_or_none()


def set_availability(db: Session, user_id: int, is_available: bool) -> WorkerProfile | None:
    profile = db.get(WorkerProfile, user_id)
    if profile is None:
        return None
    profile.is_available = is_available
    db.commit()
    db.refresh(profile)
    return profile
