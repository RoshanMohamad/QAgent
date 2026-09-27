"""An environment's GraphQL protected-fields declaration survives the API, the
database (migration b7d2e41c9a15) and the worker, and reaches the pipeline -
the only way a dashboard-triggered scan gets GraphQL authentication checks,
since introspection cannot say which fields are protected.
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


def test_declaration_reaches_the_pipeline(client, monkeypatch) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": "gql-env",
            "org_slug": "gql-env",
            "email": "owner@gql-env.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": "http://127.0.0.1:1", "graphql_protected": ["patrons", "addBook"]},
        headers=headers,
    )
    assert environment.status_code == 201, environment.text
    run = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"environment_id": environment.json()["id"], "trigger": "manual"},
        headers=headers,
    ).json()

    import qagent.worker.tasks as tasks

    seen: dict = {}

    def fake_run_pipeline(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capturing arguments")

    monkeypatch.setattr(tasks, "run_pipeline", fake_run_pipeline)
    tasks.run_scan.apply(args=[registered["org_id"], project["id"], run["id"]])

    assert seen["graphql_protected"] == ["patrons", "addBook"]


def test_omitting_it_means_no_declaration(client, monkeypatch) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": "gql-env-none",
            "org_slug": "gql-env-none",
            "email": "owner@gql-env-none.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": "http://127.0.0.1:1"},
        headers=headers,
    ).json()
    run = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"environment_id": environment["id"], "trigger": "manual"},
        headers=headers,
    ).json()

    import qagent.worker.tasks as tasks

    seen: dict = {}

    def fake_run_pipeline(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capturing arguments")

    monkeypatch.setattr(tasks, "run_pipeline", fake_run_pipeline)
    tasks.run_scan.apply(args=[registered["org_id"], project["id"], run["id"]])

    assert seen["graphql_protected"] is None
