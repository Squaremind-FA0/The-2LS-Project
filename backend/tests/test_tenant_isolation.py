"""End-to-end company isolation over HTTP, using the two_companies fixture.

Each check runs from both sides (A looking at B, and B looking at A).
"""

import pytest

from tests.factories import auth_header

NOT_FOUND = {"detail": "Worker not found"}
SIDES = pytest.mark.parametrize("me, them", [(0, 1), (1, 0)], ids=["A->B", "B->A"])


def _new_worker(email):
    return {
        "email": email,
        "full_name": "Fresh Hire",
        "initial_password": "initial-password-123",
        "hourly_rate": "40.00",
    }


@SIDES
@pytest.mark.parametrize("who", ["admin", "dispatcher"])
def test_reading_other_companys_worker_returns_404(client, two_companies, me, them, who):
    mine, theirs = two_companies[me], two_companies[them]
    headers = auth_header(getattr(mine, who))

    cross = client.get(f"/api/workers/{theirs.worker.id}", headers=headers)
    missing = client.get("/api/workers/987654", headers=headers)

    assert cross.status_code == 404
    assert cross.json() == NOT_FOUND
    # Indistinguishable from an id that does not exist at all.
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)


@SIDES
def test_list_shows_only_own_company_workers(client, two_companies, me, them):
    mine, theirs = two_companies[me], two_companies[them]
    body = client.get("/api/workers", headers=auth_header(mine.admin)).json()

    assert [w["id"] for w in body] == [mine.worker.id]
    assert {w["company_id"] for w in body} == {mine.id}
    assert theirs.worker.email not in {w["email"] for w in body}


@SIDES
def test_create_is_isolated_to_callers_company(client, two_companies, me, them):
    mine, theirs = two_companies[me], two_companies[them]
    created = client.post(
        "/api/workers", json=_new_worker(f"fresh{me}@hire.test"), headers=auth_header(mine.admin)
    )
    assert created.status_code == 201
    assert created.json()["company_id"] == mine.id

    their_list = client.get("/api/workers", headers=auth_header(theirs.admin)).json()
    assert [w["id"] for w in their_list] == [theirs.worker.id]
    new_id = created.json()["id"]
    their_read = client.get(f"/api/workers/{new_id}", headers=auth_header(theirs.admin))
    assert their_read.status_code == 404

    my_list = client.get("/api/workers", headers=auth_header(mine.admin)).json()
    assert {w["id"] for w in my_list} == {mine.worker.id, new_id}


@SIDES
@pytest.mark.parametrize("who", ["admin", "dispatcher", "worker"])
def test_me_never_exposes_other_company(client, two_companies, me, them, who):
    mine, theirs = two_companies[me], two_companies[them]
    body = client.get("/api/me", headers=auth_header(getattr(mine, who))).json()

    assert body["company_id"] == mine.id
    assert body["company_name"] == mine.name
    flat = repr(body)
    assert theirs.name not in flat
    assert str(theirs.worker.email) not in flat
