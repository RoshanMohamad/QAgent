"""The two listings the dashboard's setup panel reads: a project's
environments (the "Run QA" picker) and its recent runs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from qagent.main import app
    from qagent.worker.tasks import run_scan

    monkeypatch.setattr(run_scan, "delay", lambda *args, **kwargs: None)
    return TestClient(app)


def _owner(client: TestClient, slug: str) -> dict[str, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": slug,
            "org_slug": slug,
            "email": f"owner@{slug}.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    return {"Authorization": f"Bearer {registered['access_token']}"}


def test_environments_are_listed_without_their_credentials(client) -> None:
    headers = _owner(client, "listings-env")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={
            "name": "staging",
            "base_url": "http://127.0.0.1:1",
            "default_headers": {"Authorization": "Bearer do-not-echo"},
        },
        headers=headers,
    )

    listed = client.get(f"/api/v1/projects/{project['id']}/environments", headers=headers)

    assert listed.status_code == 200
    [environment] = listed.json()
    assert environment["name"] == "staging"
    assert environment["has_credentials"] is True
    assert "do-not-echo" not in listed.text


def test_runs_are_listed_newest_first(client) -> None:
    headers = _owner(client, "listings-runs")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": "http://127.0.0.1:1"},
        headers=headers,
    ).json()
    started = [
        client.post(
            f"/api/v1/projects/{project['id']}/runs",
            json={"environment_id": environment["id"]},
            headers=headers,
        ).json()["id"]
        for _ in range(3)
    ]

    listed = client.get(f"/api/v1/projects/{project['id']}/runs?limit=2", headers=headers).json()

    assert [r["id"] for r in listed] == started[::-1][:2]
    assert listed[0]["status"] == "pending"
    assert listed[0]["environment_id"] == environment["id"]


def test_listings_404_for_an_unknown_project(client) -> None:
    headers = _owner(client, "listings-404")
    missing = "00000000-0000-0000-0000-000000000000"
    for path in ("environments", "runs"):
        assert client.get(f"/api/v1/projects/{missing}/{path}", headers=headers).status_code == 404
