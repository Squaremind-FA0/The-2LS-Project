import os

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production-use")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient

from app.db import engine, transactional_session
from app.main import app
from app.models import Base


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
def client():
    return TestClient(app)
