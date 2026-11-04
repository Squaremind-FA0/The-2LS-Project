"""G3 — jobs and dispatch engine.

Concurrency tests run against a real file-based SQLite database (WAL, real
commits, one connection per request) because the shared in-memory test
connection serialises everything and would hide lock contention.
"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.db import _build_engine, get_db
from app.main import app
from app.models import Base, Company, User
from tests.factories import auth_header, make_company

# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def db_file_based(tmp_path):
    """A file-backed SQLite engine (WAL, real commits) wired into the app.

    Yields a session factory; every request gets its own session and
    connection, exactly like production.
    """
    from sqlalchemy.orm import sessionmaker

    engine = _build_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _file_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _file_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.fixture
def file_company(db_file_based):
    with db_file_based() as db:
        company = make_company(db, "a", "Alpha Plumbing")
        db.commit()
    return company


async def _gather(requests):
    """Fire ``requests`` (method, url, kwargs) concurrently against the app.

    Sync routes and dependencies run in AnyIO's threadpool, so these
    genuinely overlap on separate DB connections.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.gather(
            *(client.request(method, url, **kwargs) for method, url, kwargs in requests)
        )


def fire(requests):
    return asyncio.run(_gather(requests))


def _worker_payload(email: str) -> dict:
    return {
        "email": email,
        "full_name": "Parallel Hire",
        "initial_password": "initial-password-123",
        "trades": ["plumbing"],
        "hourly_rate": "50.00",
    }


# --- Phase 1: SQLite concurrency (tasks 1-2) ------------------------------------------------


def test_sqlite_concurrent_writes_never_500(file_company):
    headers = auth_header(file_company.admin)
    responses = fire(
        [
            ("POST", "/api/workers", {"json": _worker_payload(f"p{i}@a.test"), "headers": headers})
            for i in range(10)
        ]
    )
    codes = [r.status_code for r in responses]
    assert 500 not in codes
    assert set(codes) <= {201, 409}
    assert codes.count(201) == 10


def test_sqlite_concurrent_duplicate_email_one_wins(file_company, db_file_based):
    headers = auth_header(file_company.admin)
    responses = fire(
        [
            ("POST", "/api/workers", {"json": _worker_payload("same@a.test"), "headers": headers})
            for _ in range(5)
        ]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409]
    with db_file_based() as db:
        db.info["company_id"] = file_company.id
        assert db.scalar(select(func.count(User.id)).where(User.email == "same@a.test")) == 1


def test_sqlite_engine_uses_wal_and_busy_timeout(db_file_based):
    with db_file_based() as db:
        assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert db.connection().exec_driver_sql("PRAGMA busy_timeout").scalar() >= 10_000
        db.add(Company(name="x"))
        db.commit()
