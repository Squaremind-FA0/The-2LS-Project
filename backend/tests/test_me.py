from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.models import Role, User
from app.services.users import deactivate_user, set_password, set_role
from tests.factories import auth_header, tenant_context


def _expect_401(response):
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def _mutate(db, ref, fn):
    with tenant_context(db, ref.company_id):
        fn(db.get(User, ref.id))
        db.flush()


def test_me_for_worker_includes_profile_and_company(client, two_companies):
    a, _ = two_companies
    response = client.get("/api/me", headers=auth_header(a.worker))

    assert response.status_code == 200
    assert response.json() == {
        "id": a.worker.id,
        "email": a.worker.email,
        "full_name": "Worker",
        "role": "worker",
        "company_id": a.id,
        "company_name": "Alpha Plumbing",
        "worker_profile": {"trades": ["a"], "hourly_rate": "55.00", "active": True},
    }


@pytest.mark.parametrize("who", ["admin", "dispatcher"])
def test_me_for_non_worker_has_null_profile(client, two_companies, who):
    a, _ = two_companies
    ref = getattr(a, who)
    body = client.get("/api/me", headers=auth_header(ref)).json()
    assert body["role"] == ref.role.value
    assert body["company_id"] == a.id
    assert body["worker_profile"] is None


def test_me_without_authorization_header_returns_401(client, two_companies):
    _expect_401(client.get("/api/me"))


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "bearer"],
    ids=["empty-bearer", "garbage", "basic-scheme", "lowercase-no-token"],
)
def test_me_with_malformed_authorization_returns_401(client, two_companies, header):
    _expect_401(client.get("/api/me", headers={"Authorization": header}))


def test_me_with_expired_token_returns_401(client, two_companies):
    a, _ = two_companies
    stale = datetime.now(UTC) - timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN + 1)
    _expect_401(client.get("/api/me", headers=auth_header(a.admin, now=stale)))


def test_me_with_token_signed_by_wrong_secret_returns_401(client, two_companies):
    a, _ = two_companies
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(a.admin.id), "tv": 0, "iat": now, "exp": now + timedelta(minutes=5)},
        "wrong-secret-wrong-secret-wrong-secret",
        algorithm="HS256",
    )
    _expect_401(client.get("/api/me", headers={"Authorization": f"Bearer {token}"}))


@pytest.mark.parametrize("sub", ["999999", "not-an-int"])
def test_me_with_token_for_unknown_user_returns_401(client, two_companies, sub):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": sub, "tv": 0, "iat": now, "exp": now + timedelta(minutes=5)},
        settings.JWT_SECRET,
        algorithm="HS256",
    )
    _expect_401(client.get("/api/me", headers={"Authorization": f"Bearer {token}"}))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda u: set_password(u, "a-completely-new-password"),
        lambda u: set_role(u, Role.dispatcher),
        lambda u: u.bump_token_version(),
    ],
    ids=["set_password", "set_role", "bump"],
)
def test_me_with_stale_token_version_returns_401(
    client, two_companies, transactional_db, mutation
):
    a, _ = two_companies
    header = auth_header(a.admin)
    assert client.get("/api/me", headers=header).status_code == 200

    _mutate(transactional_db, a.admin, mutation)
    _expect_401(client.get("/api/me", headers=header))


def test_me_for_deactivated_user_returns_401(client, two_companies, transactional_db):
    a, _ = two_companies
    header = auth_header(a.worker)
    _mutate(transactional_db, a.worker, deactivate_user)
    _expect_401(client.get("/api/me", headers=header))


def test_me_inactive_user_with_current_token_version_returns_401(
    client, two_companies, transactional_db
):
    # is_active is checked independently of tv (e.g. a raw DB deactivation).
    a, _ = two_companies

    def _deactivate_without_bump(user):
        user.is_active = False

    _mutate(transactional_db, a.worker, _deactivate_without_bump)
    _expect_401(client.get("/api/me", headers=auth_header(a.worker)))


def test_role_change_takes_effect_without_relogin(client, two_companies, transactional_db):
    # Role is read from the DB, never the token: demoting the admin to worker
    # revokes the admin's existing token immediately (tv bumped).
    a, _ = two_companies
    header = auth_header(a.admin)
    assert client.get("/api/me", headers=header).json()["role"] == "company_admin"

    _mutate(transactional_db, a.admin, lambda u: set_role(u, Role.worker))
    _expect_401(client.get("/api/me", headers=header))


def test_role_is_read_from_db_not_token(client, two_companies, transactional_db):
    # Even without a tv bump, /api/me reports the DB role.
    a, _ = two_companies

    def _set_role_raw(user):
        user.role = Role.dispatcher

    _mutate(transactional_db, a.admin, _set_role_raw)
    assert client.get("/api/me", headers=auth_header(a.admin)).json()["role"] == "dispatcher"
