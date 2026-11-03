import os

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient

from app.db import transactional_session
from app.main import app


@pytest.fixture
def transactional_db():
    with transactional_session() as session:
        yield session


@pytest.fixture
def client():
    return TestClient(app)
