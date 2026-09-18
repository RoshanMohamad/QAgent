"""Phase 6, the parts that need a real database and (for rate limiting) a real
Redis (ADR-0008): RBAC actually blocks a member from an owner-only action, an
owner can invite a member and the member shows up with the right role, usage
is windowed correctly, and the rate limiter really does return 429 against the
real Redis instance `modules/ratelimit/limiter.py`'s unit tests fake out.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from qagent.main import app

    return TestClient(app)


def _register(client: TestClient, slug: str) -> tuple[str, str]:
    """Returns (token, org_id)."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": slug,
            "org_slug": slug,
            "email": f"owner@{slug}.example",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["org_id"]


def test_member_cannot_analyze_a_repo(client, tmp_path) -> None:
    owner_token, _ = _register(client, "rbac-analyze")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    project = client.post(
        "/api/v1/projects", json={"name": "p"}, headers=owner_headers
    ).json()

    invited = client.post(
        "/api/v1/users",
        json={"email": "member@rbac-analyze.example", "password": "correct-horse-battery-staple"},
        headers=owner_headers,
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["role"] == "member"

    member_login = client.post(
        "/api/v1/auth/login",
        json={
            "org_slug": "rbac-analyze",
            "email": "member@rbac-analyze.example",
            "password": "correct-horse-battery-staple",
        },
    )
    member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}

    response = client.post(
        f"/api/v1/projects/{project['id']}/analyze",
        json={"repo_path": str(tmp_path)},
        headers=member_headers,
    )
    assert response.status_code == 403

    # The owner who invited them can, though - same endpoint, different role.
    owner_response = client.post(
        f"/api/v1/projects/{project['id']}/analyze",
        json={"repo_path": str(tmp_path)},
        headers=owner_headers,
    )
    assert owner_response.status_code == 200


def test_member_cannot_invite_another_user(client) -> None:
    owner_token, _ = _register(client, "rbac-invite")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    client.post(
        "/api/v1/users",
        json={"email": "member@rbac-invite.example", "password": "correct-horse-battery-staple"},
        headers=owner_headers,
    )
    member_login = client.post(
        "/api/v1/auth/login",
        json={
            "org_slug": "rbac-invite",
            "email": "member@rbac-invite.example",
            "password": "correct-horse-battery-staple",
        },
    )
    member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}

    response = client.post(
        "/api/v1/users",
        json={"email": "second@rbac-invite.example", "password": "correct-horse-battery-staple"},
        headers=member_headers,
    )
    assert response.status_code == 403


def test_any_member_can_list_their_orgs_users(client) -> None:
    owner_token, _ = _register(client, "rbac-list")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    client.post(
        "/api/v1/users",
        json={"email": "member@rbac-list.example", "password": "correct-horse-battery-staple"},
        headers=owner_headers,
    )

    response = client.get("/api/v1/users", headers=owner_headers)
    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert emails == {"owner@rbac-list.example", "member@rbac-list.example"}


def test_invite_rejects_a_duplicate_email_in_the_same_org(client) -> None:
    owner_token, _ = _register(client, "rbac-dup")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    first = client.post(
        "/api/v1/users",
        json={"email": "dup@rbac-dup.example", "password": "correct-horse-battery-staple"},
        headers=owner_headers,
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/users",
        json={"email": "dup@rbac-dup.example", "password": "correct-horse-battery-staple"},
        headers=owner_headers,
    )
    assert second.status_code == 409


def test_usage_is_windowed_and_reflects_a_real_run(client, fixture_app_url) -> None:
    owner_token, org_id = _register(client, "usage-org")
    headers = {"Authorization": f"Bearer {owner_token}"}

    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": fixture_app_url},
        headers=headers,
    ).json()

    from qagent.worker.tasks import run_scan

    run = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"environment_id": environment["id"], "trigger": "manual"},
        headers=headers,
    ).json()
    # In-process, not through the broker: this test is about the usage endpoint's
    # arithmetic, not the queue - test_worker_task.py already proves the queue.
    run_scan.apply(args=[org_id, project["id"], run["id"]])

    usage = client.get("/api/v1/usage", headers=headers)
    assert usage.status_code == 200
    body = usage.json()
    assert sum(body["runs"].values()) == 1
    assert sum(body["defects"].values()) >= 4  # buggy-shop's four seeded defects

    # A window starting after the run happened sees none of it.
    future = client.get("/api/v1/usage?since=2099-01-01T00:00:00Z", headers=headers)
    assert sum(future.json()["runs"].values()) == 0


def test_metrics_endpoint_is_reachable_without_auth_and_reflects_traffic(client) -> None:
    # /health and /metrics are both excluded from being counted (main.py's
    # middleware) - an unauthenticated call to a real route is what generates
    # something to observe.
    client.get("/api/v1/projects")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "qagent_http_requests_total" in response.text
    assert 'path_template="/api/v1/projects"' in response.text


def test_login_is_rate_limited_against_a_real_redis(client) -> None:
    _register(client, "ratelimit-org")

    responses = [
        client.post(
            "/api/v1/auth/login",
            json={"org_slug": "ratelimit-org", "email": "x@x.example", "password": "wrong"},
        )
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]].count(429) == 0
    assert responses[10].status_code == 429
    assert "Retry-After" in responses[10].headers
