"""An environment's `secret_ref` is resolved by the worker at run time
(modules/secrets/, ADR-0004) - against the real database and the real task.

Both halves matter: a secret that exists lets the scan run, and one that does
not fails the run as a configuration error instead of scanning anonymously.
Scanning anonymously would look like success while testing none of the
authenticated surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from qagent.main import app
    from qagent.worker.tasks import run_scan

    # Only the in-process .apply() below may execute the run: a live worker left
    # running by another module would not see this test's monkeypatched secret.
    monkeypatch.setattr(run_scan, "delay", lambda *args, **kwargs: None)
    return TestClient(app)


def _setup(client: TestClient, slug: str, base_url: str, secret_ref: str) -> tuple[dict, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": slug,
            "org_slug": slug,
            "email": f"owner@{slug}.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": base_url, "secret_ref": secret_ref},
        headers=headers,
    )
    assert environment.status_code == 201, environment.text
    run = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"environment_id": environment.json()["id"], "trigger": "manual"},
        headers=headers,
    ).json()
    return headers, f"{registered['org_id']}|{project['id']}|{run['id']}"


def _var(ids: str) -> str:
    from uuid import UUID

    from qagent.modules.secrets.store import env_var_name

    return env_var_name("shop-staging", org_id=UUID(ids.split("|")[0]))


def _run(ids: str) -> dict:
    from qagent.worker.tasks import run_scan

    return run_scan.apply(args=ids.split("|")).get()


def test_a_missing_secret_errors_the_run_instead_of_scanning_anonymously(
    client, fixture_app_url, monkeypatch
) -> None:
    headers, ids = _setup(client, "secret-missing", fixture_app_url, "shop-staging")
    monkeypatch.delenv(_var(ids), raising=False)

    result = _run(ids)

    assert "credentials unavailable" in result["error"]
    run_id = ids.split("|")[2]
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "error"


def test_a_configured_secret_lets_the_scan_run(client, fixture_app_url, monkeypatch) -> None:
    headers, ids = _setup(client, "secret-present", fixture_app_url, "shop-staging")
    monkeypatch.setenv(_var(ids), "not-a-real-token")

    result = _run(ids)

    assert "error" not in result
    run_id = ids.split("|")[2]
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "failed"


def test_a_path_is_refused_as_a_secret_ref(client) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": "secret-path",
            "org_slug": "secret-path",
            "email": "owner@secret-path.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()

    response = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": "http://127.0.0.1:1", "secret_ref": "../../etc/passwd"},
        headers=headers,
    )

    assert response.status_code == 422
