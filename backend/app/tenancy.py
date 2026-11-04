"""Company tenancy, enforced structurally and fail-closed.

Every tenant-owned table inherits ``TenantScoped`` (a non-null, indexed
``company_id`` FK to ``companies.id``). Two ORM session listeners enforce
isolation using ``session.info["company_id"]`` as the tenant context:

* ``do_orm_execute``: every ORM SELECT / UPDATE / DELETE that touches a
  ``TenantScoped`` entity gets ``with_loader_criteria(TenantScoped,
  company_id == <context>)``. That covers ``session.get``, aliases, joins,
  relationship lazy/eager loads and ORM bulk update/delete. With no tenant
  context the statement raises ``TenantContextMissing``.
* ``before_flush``: new tenant objects get ``company_id`` stamped from the
  context. Any new, modified or deleted tenant object whose ``company_id``
  differs from the context raises ``CrossTenantWrite``. With no context,
  ``TenantContextMissing``.

The only bypass is ``.execution_options(skip_tenant_filter=True)`` on a
single statement, reserved for the pre-auth lookups in
``app/auth/service.py``.

Not covered: raw ``text()`` SQL and Core ``connection.execute()``. Never use
them against tenant tables in request code.

Listeners are installed on the ``Session`` *class*, so every session in the
process is covered, including sessions not built from ``SessionLocal``.
"""

from sqlalchemy import ForeignKey, event, inspect
from sqlalchemy.orm import (
    Mapped,
    ORMExecuteState,
    Session,
    declared_attr,
    mapped_column,
    with_loader_criteria,
)
from sqlalchemy.sql.util import find_tables

TENANT_INFO_KEY = "company_id"
SKIP_OPTION = "skip_tenant_filter"


class TenantContextMissing(RuntimeError):
    """A tenant-scoped ORM operation ran on a session without a tenant."""


class CrossTenantWrite(RuntimeError):
    """A flush tried to write a row belonging to a different company."""


class TenantScoped:
    @declared_attr
    def company_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
        )


def _tenant_tables() -> set:
    tables, pending = set(), list(TenantScoped.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if table is not None:
            tables.add(table)
    return tables


def _touches_tenant_data(state: ORMExecuteState) -> bool:
    if any(issubclass(mapper.class_, TenantScoped) for mapper in state.all_mappers):
        return True
    # all_mappers only lists the selected entities; a tenant table reached via
    # join(), a subquery or EXISTS from a non-tenant entity must count too.
    tenant_tables = _tenant_tables()
    found = find_tables(
        state.statement, include_joins=True, include_aliases=True, include_crud=True,
        check_columns=True,
    )
    return any(table in tenant_tables for table in found)


def _scope_orm_execute(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    # Read the option from the statement itself, not the merged execution
    # options, so a bypass on one statement never leaks into the lazy loads
    # of objects it returned.
    if state.statement.get_execution_options().get(SKIP_OPTION, False):
        return
    if not _touches_tenant_data(state):
        return
    company_id = state.session.info.get(TENANT_INFO_KEY)
    if company_id is None:
        raise TenantContextMissing(
            "ORM statement on tenant-scoped data without session.info['company_id']"
        )
    # The lambda closes over company_id; SQLAlchemy tracks closure variables
    # so the cached statement is re-parameterised per tenant.
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.company_id == company_id,
            include_aliases=True,
        )
    )


def _guard_flush(session: Session, flush_context, instances) -> None:
    company_id = session.info.get(TENANT_INFO_KEY)
    for obj in session.new:
        if not isinstance(obj, TenantScoped):
            continue
        if company_id is None:
            raise TenantContextMissing("flush of a new tenant-scoped object without a tenant")
        if obj.company_id is None:
            obj.company_id = company_id
        elif obj.company_id != company_id:
            raise CrossTenantWrite(
                f"new {type(obj).__name__} belongs to another company than the session"
            )
    for obj in (*session.dirty, *session.deleted):
        if not isinstance(obj, TenantScoped):
            continue
        if company_id is None:
            raise TenantContextMissing("flush of a tenant-scoped change without a tenant")
        history = inspect(obj).attrs.company_id.history
        original = history.deleted[0] if history.deleted else obj.company_id
        if obj.company_id != company_id or original != company_id:
            raise CrossTenantWrite(
                f"{type(obj).__name__} change crosses the session's company boundary"
            )


def install_tenant_listeners(target=Session) -> None:
    """Register the tenancy listeners on ``target`` (idempotent)."""
    if not event.contains(target, "do_orm_execute", _scope_orm_execute):
        event.listen(target, "do_orm_execute", _scope_orm_execute)
    if not event.contains(target, "before_flush", _guard_flush):
        event.listen(target, "before_flush", _guard_flush)
