"""Session-level tenant isolation (app/tenancy.py listeners)."""

from decimal import Decimal

import pytest
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.orm import Session, aliased, joinedload, selectinload, sessionmaker

from app.models import Company, Role, User, WorkerProfile
from app.tenancy import CrossTenantWrite, TenantContextMissing
from tests.factories import tenant_context

SKIP = {"skip_tenant_filter": True}


@pytest.fixture
def tenants(transactional_db):
    """Two companies, each with an admin and a worker (with profile)."""
    db = transactional_db
    ids = {}
    for key, name in (("a", "Alpha Plumbing"), ("b", "Bravo Electrical")):
        company = Company(name=name)
        db.add(company)
        db.flush()
        with tenant_context(db, company.id):
            admin = User(
                email=f"admin@{key}.test", password_hash="x", full_name=f"{key} admin",
                role=Role.company_admin,
            )
            worker = User(
                email=f"worker@{key}.test", password_hash="x", full_name=f"{key} worker",
                role=Role.worker,
            )
            profile = WorkerProfile(user=worker, trades=[key], hourly_rate=Decimal("50.00"))
            db.add_all([admin, worker, profile])
            db.flush()
        ids[key] = {
            "company": company.id, "admin": admin.id, "worker": worker.id, "profile": profile.id
        }
    db.expunge_all()
    return ids


def _emails(db: Session, stmt=None) -> set[str]:
    return set(db.scalars(stmt if stmt is not None else select(User.email)))


# --- fail closed --------------------------------------------------------------------------


def test_select_without_tenant_context_raises(transactional_db, tenants):
    with pytest.raises(TenantContextMissing):
        transactional_db.scalars(select(User)).all()


@pytest.mark.parametrize(
    "stmt",
    [
        select(User.email),
        select(func.count(User.id)),
        select(Company.name).join(User, User.company_id == Company.id),
        update(User).values(full_name="x"),
        delete(WorkerProfile),
    ],
    ids=["column-select", "aggregate", "join-from-company", "bulk-update", "bulk-delete"],
)
def test_any_orm_statement_touching_tenant_data_without_context_raises(
    transactional_db, tenants, stmt
):
    with pytest.raises(TenantContextMissing):
        transactional_db.execute(stmt)


def test_session_get_without_tenant_context_raises(transactional_db, tenants):
    with pytest.raises(TenantContextMissing):
        transactional_db.get(User, tenants["a"]["admin"])


def test_skip_tenant_filter_bypasses_the_listener(transactional_db, tenants):
    users = transactional_db.scalars(select(User).execution_options(**SKIP)).all()
    assert len(users) == 4


def test_non_tenant_entity_is_not_blocked(transactional_db, tenants):
    names = set(transactional_db.scalars(select(Company.name)))
    assert names == {"Alpha Plumbing", "Bravo Electrical"}


def test_fresh_sessionmaker_session_is_also_fail_closed(transactional_db, tenants):
    # Listeners live on the Session class, so a session not built from
    # SessionLocal cannot silently skip tenant scoping.
    other = sessionmaker(bind=transactional_db.bind)()
    try:
        with pytest.raises(TenantContextMissing):
            other.scalars(select(User)).all()
    finally:
        other.close()


# --- read filtering -----------------------------------------------------------------------


def test_select_returns_only_own_company_rows(transactional_db, tenants):
    with tenant_context(transactional_db, tenants["a"]["company"]):
        assert _emails(transactional_db) == {"admin@a.test", "worker@a.test"}
        assert transactional_db.scalar(select(func.count(User.id))) == 2


def test_session_get_of_other_company_row_returns_none(transactional_db, tenants):
    with tenant_context(transactional_db, tenants["a"]["company"]):
        assert transactional_db.get(User, tenants["b"]["worker"]) is None
        assert transactional_db.get(WorkerProfile, tenants["b"]["profile"]) is None
        assert transactional_db.get(User, tenants["a"]["worker"]) is not None


def test_aliased_and_joined_queries_are_filtered(transactional_db, tenants):
    with tenant_context(transactional_db, tenants["a"]["company"]):
        u = aliased(User)
        assert _emails(transactional_db, select(u.email)) == {"admin@a.test", "worker@a.test"}

        joined = transactional_db.execute(
            select(User.email, WorkerProfile.trades).join(WorkerProfile.user)
        ).all()
        assert joined == [("worker@a.test", ["a"])]

        # Explicit join to a *different* company's id still yields nothing.
        cross = select(WorkerProfile).join(User, User.id == WorkerProfile.user_id).where(
            User.id == tenants["b"]["worker"]
        )
        assert transactional_db.scalars(cross).all() == []


def _reach_b_worker_from_non_tenant_entity(b_worker_id):
    ua = aliased(User)
    return {
        "join": select(Company.name).join(User, User.company_id == Company.id).where(
            User.id == b_worker_id
        ),
        "in-subquery": select(Company.name).where(
            Company.id.in_(select(User.company_id).where(User.id == b_worker_id))
        ),
        "exists": select(Company.name).where(
            exists().where(User.company_id == Company.id, User.id == b_worker_id)
        ),
        "aliased-join": select(Company.name).join(ua, Company.id == ua.company_id).where(
            ua.id == b_worker_id
        ),
        "relationship-has": select(WorkerProfile.id).where(
            WorkerProfile.user.has(User.id == b_worker_id)
        ),
    }


@pytest.mark.parametrize(
    "shape", ["join", "in-subquery", "exists", "aliased-join", "relationship-has"]
)
def test_tenant_tables_reached_indirectly_are_filtered(transactional_db, tenants, shape):
    stmt = _reach_b_worker_from_non_tenant_entity(tenants["b"]["worker"])[shape]
    with tenant_context(transactional_db, tenants["a"]["company"]):
        assert transactional_db.execute(stmt).all() == []
    # ...and without a context the same statement fails closed.
    with pytest.raises(TenantContextMissing):
        transactional_db.execute(stmt)


def test_scalar_subquery_on_tenant_table_is_filtered(transactional_db, tenants):
    email = select(User.email).where(User.id == tenants["b"]["worker"]).scalar_subquery()
    with tenant_context(transactional_db, tenants["a"]["company"]):
        rows = transactional_db.execute(select(Company.name, email)).all()
    assert {row[1] for row in rows} == {None}


def test_lambda_criteria_is_not_cached_across_tenants(transactional_db, tenants):
    db = transactional_db
    stmt = select(User.email)
    with tenant_context(db, tenants["a"]["company"]):
        assert _emails(db, stmt) == {"admin@a.test", "worker@a.test"}
    with tenant_context(db, tenants["b"]["company"]):
        assert _emails(db, stmt) == {"admin@b.test", "worker@b.test"}
    with tenant_context(db, tenants["a"]["company"]):
        assert _emails(db, stmt) == {"admin@a.test", "worker@a.test"}


@pytest.fixture
def cross_linked_profile(transactional_db, tenants):
    """A company-B WorkerProfile whose user_id points at company A's admin —
    corrupt data that loaders must never surface to company A."""
    db = transactional_db
    with tenant_context(db, tenants["b"]["company"]):
        bad = WorkerProfile(user_id=tenants["a"]["admin"], trades=["b"], hourly_rate=1)
        db.add(bad)
        db.flush()
    db.expunge_all()
    return bad.id


def test_relationship_lazy_load_is_filtered(transactional_db, tenants, cross_linked_profile):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        admin = db.get(User, tenants["a"]["admin"])
        assert admin.worker_profile is None


@pytest.mark.parametrize("loader", [joinedload, selectinload], ids=["joined", "selectin"])
def test_relationship_eager_loads_are_filtered(
    transactional_db, tenants, cross_linked_profile, loader
):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        admin = db.scalars(
            select(User).options(loader(User.worker_profile)).where(
                User.id == tenants["a"]["admin"]
            )
        ).unique().one()
        assert admin.worker_profile is None


def test_lazy_load_on_object_loaded_with_skip_is_still_filtered(
    transactional_db, tenants, cross_linked_profile
):
    # get_current_user loads the caller with skip_tenant_filter; relationships
    # traversed later in the request must not inherit that bypass.
    db = transactional_db
    admin = db.scalars(
        select(User).where(User.id == tenants["a"]["admin"]).execution_options(**SKIP)
    ).one()
    with tenant_context(db, tenants["a"]["company"]):
        assert admin.worker_profile is None


def test_raw_text_sql_is_not_filtered_documented_gap(transactional_db, tenants):
    # Documented limitation: raw SQL bypasses the ORM listener. Request code
    # must never use text() against tenant tables.
    with tenant_context(transactional_db, tenants["a"]["company"]):
        assert transactional_db.execute(text("SELECT count(*) FROM users")).scalar() == 4


# --- write filtering ----------------------------------------------------------------------


def test_bulk_update_only_touches_own_company(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        result = db.execute(update(User).values(full_name="renamed"))
        assert result.rowcount == 2
    names = dict(db.execute(select(User.email, User.full_name).execution_options(**SKIP)).all())
    assert names["admin@a.test"] == names["worker@a.test"] == "renamed"
    assert names["admin@b.test"] == "b admin"
    assert names["worker@b.test"] == "b worker"


def test_bulk_delete_only_touches_own_company(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        assert db.execute(delete(WorkerProfile)).rowcount == 1
        targeted = delete(WorkerProfile).where(WorkerProfile.id == tenants["b"]["profile"])
        assert db.execute(targeted).rowcount == 0
    remaining = db.scalars(select(WorkerProfile.id).execution_options(**SKIP)).all()
    assert remaining == [tenants["b"]["profile"]]


def test_before_flush_stamps_company_id_on_new_objects(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        user = User(email="new@a.test", password_hash="x", full_name="n", role=Role.worker)
        profile = WorkerProfile(user=user, hourly_rate=Decimal("1.00"))
        db.add(profile)
        db.flush()
        assert user.company_id == profile.company_id == tenants["a"]["company"]


def test_before_flush_rejects_new_object_for_another_company(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        db.add(
            WorkerProfile(
                company_id=tenants["b"]["company"], user_id=tenants["a"]["admin"], hourly_rate=1
            )
        )
        with pytest.raises(CrossTenantWrite):
            db.flush()


def test_before_flush_rejects_moving_a_row_to_another_company(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        worker = db.get(User, tenants["a"]["worker"])
        worker.company_id = tenants["b"]["company"]
        with pytest.raises(CrossTenantWrite):
            db.flush()


def test_flush_of_tenant_objects_without_context_raises(transactional_db, tenants):
    db = transactional_db
    db.add(
        User(
            company_id=tenants["a"]["company"], email="ctx@a.test", password_hash="x",
            full_name="n", role=Role.worker,
        )
    )
    with pytest.raises(TenantContextMissing):
        db.flush()


def test_delete_of_tenant_object_without_context_raises(transactional_db, tenants):
    db = transactional_db
    with tenant_context(db, tenants["a"]["company"]):
        profile = db.get(WorkerProfile, tenants["a"]["profile"])
    db.delete(profile)
    with pytest.raises(TenantContextMissing):
        db.flush()
