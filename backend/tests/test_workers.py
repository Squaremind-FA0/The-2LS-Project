import pytest
from sqlalchemy import select

from app.models import Role, User, WorkerProfile
from tests.factories import auth_header, tenant_context

NEW_PASSWORD = "initial-password-123"


def _payload(**overrides):
    body = {
        "email": "New.Hire@Alpha.test",
        "full_name": "New Hire",
        "initial_password": NEW_PASSWORD,
        "trades": ["plumbing", "gas"],
        "hourly_rate": "62.50",
    }
    body.update(overrides)
    return body


def test_admin_creates_worker_in_own_company(client, two_companies, transactional_db):
    a, _ = two_companies
    response = client.post("/api/workers", json=_payload(), headers=auth_header(a.admin))

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "email": "new.hire@alpha.test",
        "full_name": "New Hire",
        "role": "worker",
        "company_id": a.id,
        "is_active": True,
        "worker_profile": {"trades": ["plumbing", "gas"], "hourly_rate": "62.50", "active": True},
    }
    with tenant_context(transactional_db, a.id):
        user = transactional_db.get(User, body["id"])
        assert user.role is Role.worker
        assert user.password_hash.startswith("$argon2id$")
        profile = transactional_db.scalars(
            select(WorkerProfile).where(WorkerProfile.user_id == user.id)
        ).one()
        assert profile.company_id == a.id


def test_new_worker_can_log_in(client, two_companies):
    a, _ = two_companies
    assert client.post("/api/workers", json=_payload(), headers=auth_header(a.admin)).is_success
    response = client.post(
        "/api/auth/login", json={"email": "new.hire@alpha.test", "password": NEW_PASSWORD}
    )
    assert response.status_code == 200


def test_smuggled_company_id_is_rejected(client, two_companies, transactional_db):
    a, b = two_companies
    response = client.post(
        "/api/workers", json=_payload(company_id=b.id), headers=auth_header(a.admin)
    )
    assert response.status_code == 422
    assert transactional_db.scalars(
        select(User).where(User.email == "new.hire@alpha.test").execution_options(
            skip_tenant_filter=True
        )
    ).all() == []


@pytest.mark.parametrize("who", ["worker", "dispatcher"])
def test_non_admin_cannot_create_worker(client, two_companies, who):
    a, _ = two_companies
    response = client.post("/api/workers", json=_payload(), headers=auth_header(getattr(a, who)))
    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient role"}


@pytest.mark.parametrize("company", [0, 1], ids=["same-company", "other-company"])
def test_duplicate_email_returns_409(client, two_companies, company):
    a, _ = two_companies
    existing = two_companies[company].worker.email
    response = client.post(
        "/api/workers", json=_payload(email=existing.upper()), headers=auth_header(a.admin)
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Email already registered"}


def test_after_409_the_admin_can_still_create_a_worker(client, two_companies):
    # The failed insert is rolled back cleanly; the session stays usable.
    a, b = two_companies
    headers = auth_header(a.admin)
    assert client.post(
        "/api/workers", json=_payload(email=b.admin.email), headers=headers
    ).status_code == 409
    assert client.post("/api/workers", json=_payload(), headers=headers).status_code == 201


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_password": "short-pw"},
        {"initial_password": "x" * 11},
        {"full_name": ""},
        {"hourly_rate": "-1"},
        {"hourly_rate": "1.234"},
        {"email": "not-an-email"},
        {"trades": ["t"] * 21},
        {"role": "company_admin"},
    ],
    ids=["short-password", "11-char-password", "empty-name", "negative-rate",
         "3dp-rate", "no-at-sign", "too-many-trades", "smuggled-role"],
)
def test_invalid_payload_returns_422(client, two_companies, overrides):
    a, _ = two_companies
    response = client.post("/api/workers", json=_payload(**overrides), headers=auth_header(a.admin))
    assert response.status_code == 422


def test_unauthenticated_create_returns_401(client, two_companies):
    assert client.post("/api/workers", json=_payload()).status_code == 401


@pytest.mark.parametrize("who", ["admin", "dispatcher"])
def test_admin_and_dispatcher_can_list_and_read_workers(client, two_companies, who):
    a, _ = two_companies
    headers = auth_header(getattr(a, who))
    listing = client.get("/api/workers", headers=headers)
    assert listing.status_code == 200
    assert [w["id"] for w in listing.json()] == [a.worker.id]

    one = client.get(f"/api/workers/{a.worker.id}", headers=headers)
    assert one.status_code == 200
    assert one.json()["worker_profile"]["trades"] == ["a"]


def test_worker_cannot_list_workers(client, two_companies):
    a, _ = two_companies
    headers = auth_header(a.worker)
    assert client.get("/api/workers", headers=headers).status_code == 403
    assert client.get(f"/api/workers/{a.worker.id}", headers=headers).status_code == 403


def test_reading_a_non_worker_user_returns_404(client, two_companies):
    a, _ = two_companies
    response = client.get(f"/api/workers/{a.dispatcher.id}", headers=auth_header(a.admin))
    assert response.status_code == 404
    assert response.json() == {"detail": "Worker not found"}
