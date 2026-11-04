import pytest
from sqlalchemy import select

from app.config import settings
from app.models import User
from app.security.tokens import decode_access_token
from app.services.users import deactivate_user
from app.tenancy import TenantContextMissing
from tests.factories import ADMIN_PASSWORD, WORKER_PASSWORD, tenant_context

INVALID = {"detail": "Invalid credentials"}


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_login_with_correct_credentials_returns_token(client, two_companies):
    a, _ = two_companies
    response = _login(client, a.admin.email, ADMIN_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.ACCESS_TOKEN_TTL_MIN * 60 == 3600
    claims = decode_access_token(body["access_token"])
    assert claims["sub"] == str(a.admin.id)
    assert claims["tv"] == 0


def test_login_with_bad_password_returns_401(client, two_companies):
    a, _ = two_companies
    response = _login(client, a.admin.email, "not-the-password")
    assert response.status_code == 401
    assert response.json() == INVALID


def test_login_with_another_users_password_returns_401(client, two_companies):
    a, _ = two_companies
    response = _login(client, a.admin.email, WORKER_PASSWORD)
    assert response.status_code == 401
    assert response.json() == INVALID


def test_login_with_unknown_email_returns_identical_401(client, two_companies):
    response = _login(client, "nobody@nowhere.test", ADMIN_PASSWORD)
    assert response.status_code == 401
    assert response.json() == INVALID


def test_unknown_email_still_runs_a_password_verify(client, two_companies, monkeypatch):
    # Timing-enumeration guard: the unknown-email path must pay the Argon2 cost.
    import app.auth.service as service

    calls = []
    real_verify = service.verify_password
    monkeypatch.setattr(
        service, "verify_password", lambda p, h: calls.append(h) or real_verify(p, h)
    )
    assert _login(client, "nobody@nowhere.test", "whatever").status_code == 401
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


def test_login_of_inactive_user_returns_401(client, two_companies, transactional_db):
    a, _ = two_companies
    with tenant_context(transactional_db, a.id):
        deactivate_user(transactional_db.get(User, a.admin.id))
        transactional_db.flush()

    response = _login(client, a.admin.email, ADMIN_PASSWORD)
    assert response.status_code == 401
    assert response.json() == INVALID


def test_login_email_is_case_insensitive(client, two_companies):
    a, _ = two_companies
    response = _login(client, f"  {a.admin.email.upper()} ", ADMIN_PASSWORD)
    assert response.status_code == 200


def test_login_rejects_malformed_bodies(client, two_companies):
    a, _ = two_companies
    assert client.post("/api/auth/login", json={"email": a.admin.email}).status_code == 422
    assert _login(client, a.admin.email, "").status_code == 422
    extra = {"email": a.admin.email, "password": ADMIN_PASSWORD, "company_id": 1}
    assert client.post("/api/auth/login", json=extra).status_code == 422


def test_login_does_not_leave_tenant_rows_visible_without_context(transactional_db, client):
    # Sanity: the fixture session stays fail-closed after app requests.
    _login(client, "x@y.test", "pw")
    with pytest.raises(TenantContextMissing):
        transactional_db.scalars(select(User)).all()
