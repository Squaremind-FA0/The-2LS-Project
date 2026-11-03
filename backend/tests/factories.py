"""Test-data helpers.

Tenant-scoped rows can only be flushed inside a tenant context (the
before_flush listener fails closed), so helpers set
``session.info["company_id"]`` while they work.
"""

from contextlib import contextmanager

from sqlalchemy.orm import Session


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
