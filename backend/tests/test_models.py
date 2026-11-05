from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Company, Role, TenantScoped, User, WorkerProfile
from app.security.passwords import verify_password
from app.services.users import deactivate_user, set_password, set_role
from tests.factories import tenant_context


def _company(db, name="Acme Plumbing") -> Company:
    company = Company(name=name)
    db.add(company)
    db.flush()
    return company


def _user(db, company, email="admin@acme.test", role=Role.company_admin) -> User:
    user = User(
        company_id=company.id,
        email=email,
        password_hash="x",
        full_name="Ada Admin",
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def test_company_user_and_worker_profile_persist(transactional_db):
    db = transactional_db
    company = _company(db)
    with tenant_context(db, company.id):
        user = _user(db, company, email="worker@acme.test", role=Role.worker)
        db.add(WorkerProfile(company_id=company.id, user=user, hourly_rate=Decimal("45.50")))
        db.flush()
        db.expire_all()

        loaded = db.scalars(select(User).where(User.email == "worker@acme.test")).one()
        assert loaded.role is Role.worker
        assert loaded.company_id == company.id
        assert loaded.worker_profile.hourly_rate == Decimal("45.50")
        assert loaded.worker_profile.trades == []
        assert loaded.created_at is not None
    assert db.get(Company, company.id).created_at is not None


def test_tenant_models_inherit_tenant_scoped_with_indexed_non_null_company_id():
    assert issubclass(User, TenantScoped)
    assert issubclass(WorkerProfile, TenantScoped)
    assert not issubclass(Company, TenantScoped)
    for model in (User, WorkerProfile):
        column = model.__table__.c.company_id
        assert column.nullable is False
        assert column.index is True
        assert {fk.target_fullname for fk in column.foreign_keys} == {"companies.id"}


def test_user_email_is_unique(transactional_db):
    db = transactional_db
    company = _company(db)
    with tenant_context(db, company.id):
        _user(db, company, email="dup@acme.test")
        with pytest.raises(IntegrityError):
            _user(db, company, email="dup@acme.test")


def test_user_defaults(transactional_db):
    db = transactional_db
    company = _company(db)
    with tenant_context(db, company.id):
        user = _user(db, company)
        db.refresh(user)
        assert user.token_version == 0
        assert user.is_active is True


def test_worker_profile_trades_round_trip_a_list(transactional_db):
    db = transactional_db
    company = _company(db)
    with tenant_context(db, company.id):
        user = _user(db, company, email="w@acme.test", role=Role.worker)
        profile = WorkerProfile(
            company_id=company.id,
            user=user,
            trades=["plumbing", "gas"],
            hourly_rate=Decimal("60.00"),
        )
        db.add(profile)
        db.flush()
        db.expire_all()
        assert db.get(WorkerProfile, profile.user_id).trades == ["plumbing", "gas"]


def test_revocation_helpers_bump_token_version(transactional_db):
    db = transactional_db
    company = _company(db)
    with tenant_context(db, company.id):
        user = _user(db, company)
        db.refresh(user)

        user.bump_token_version()
        assert user.token_version == 1

        set_role(user, Role.dispatcher)
        assert (user.role, user.token_version) == (Role.dispatcher, 2)

        set_role(user, Role.dispatcher)  # no-op change does not revoke
        assert user.token_version == 2

        set_password(user, "a-brand-new-password")
        assert user.token_version == 3
        assert verify_password("a-brand-new-password", user.password_hash)

        deactivate_user(user)
        assert (user.is_active, user.token_version) == (False, 4)
        db.flush()
