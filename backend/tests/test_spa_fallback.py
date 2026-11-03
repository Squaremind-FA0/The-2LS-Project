import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import _DIST, app


@pytest.fixture
def frontend_dist():
    created_dist = not os.path.isdir(_DIST)
    if created_dist:
        os.makedirs(_DIST)
    index_path = os.path.join(_DIST, "index.html")
    had_index = os.path.isfile(index_path)
    original = Path(index_path).read_text() if had_index else None
    Path(index_path).write_text("<html>spa-fallback-marker</html>")
    try:
        yield
    finally:
        if original is not None:
            Path(index_path).write_text(original)
        else:
            os.remove(index_path)
        if created_dist:
            os.rmdir(_DIST)


def test_spa_fallback_serves_index_for_non_api_routes(frontend_dist):
    client = TestClient(app)
    response = client.get("/some/deep/client-route")
    assert response.status_code == 200
    assert "spa-fallback-marker" in response.text


def test_spa_fallback_returns_404_for_unknown_api_routes(frontend_dist):
    client = TestClient(app)
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
