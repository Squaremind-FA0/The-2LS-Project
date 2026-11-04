import os

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production-use")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, engine, get_db, transactional_session
from app.main import app
from app.models import Base
from tests.factories import make_company


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create every ORM table once in the shared test database."""
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def transactional_db():
    with transactional_session() as session:
        yield session


@pytest.fixture
def client(transactional_db):
    """TestClient whose requests run on the test's rolled-back connection.

    Each request gets its own Session (like production) bound to the same
    connection in create_savepoint mode, so route-level commit() releases a
    SAVEPOINT instead of committing the outer test transaction.
    """
    connection = transactional_db.bind

    def _test_get_db():
        db = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _test_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def two_companies(transactional_db):
    """Company A and company B, each with a company_admin, a dispatcher and a
    worker (with WorkerProfile), all with known passwords."""
    a = make_company(transactional_db, "a", "Alpha Plumbing")
    b = make_company(transactional_db, "b", "Bravo Electrical")
    transactional_db.expunge_all()
    return a, b
