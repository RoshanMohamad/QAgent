"""The distributed path actually works: a real HTTP request queues a real Celery
task onto a real Redis broker, a separate `celery worker` process picks it up,
scans a real target over the network, and persists the result into Postgres
through the restricted app role RLS governs (ADR-0007).

This is the exact manual sequence used to first verify the fix, turned into a
repeatable test: register -> create project -> create environment pointing at
the buggy-shop fixture -> start a run -> poll until the out-of-process worker
finishes it -> read the persisted bugs back through the same API.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from qagent.main import app

    return TestClient(app)


def _register(client: TestClient, slug: str) -> str:
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
    return response.json()["access_token"]


def test_scan_runs_end_to_end_through_the_real_worker(
    client, fixture_app_url, live_worker
) -> None:
    token = _register(client, "verify-co")
    headers = {"Authorization": f"Bearer {token}"}

    project = client.post("/api/v1/projects", json={"name": "buggy-shop"}, headers=headers)
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]

    environment = client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"base_url": fixture_app_url},
        headers=headers,
    )
    assert environment.status_code == 201, environment.text
    environment_id = environment.json()["id"]

    run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"environment_id": environment_id, "trigger": "manual"},
        headers=headers,
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["id"]

    deadline = time.monotonic() + 90
    status = "pending"
    while time.monotonic() < deadline:
        got = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert got.status_code == 200
        status = got.json()["status"]
        if status in ("passed", "failed", "error"):
            break
        time.sleep(1.0)

    if status not in ("passed", "failed", "error"):
        output = live_worker.log_path.read_text(encoding="utf-8", errors="replace")
        pytest.fail(f"run never finished (status={status!r}); worker log:\n{output}")
    assert status == "failed", f"expected the seeded defects to fail the run, got {status!r}"

    finished = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    # buggy-shop seeds 4 real defects, several of which more than one generated
    # case reaches (an unauthenticated request and a forged token both land on
    # the same unenforced-auth endpoint) - so "at least 4" is the honest floor,
    # not "exactly 4".
    assert finished["classifications"].get("real_bug", 0) >= 4

    bugs = client.get(f"/api/v1/projects/{project_id}/bugs", headers=headers)
    assert bugs.status_code == 200
    assert len(bugs.json()) >= 4


def test_second_org_cannot_see_the_first_orgs_run(client, fixture_app_url, live_worker) -> None:
    """The same isolation `test_rls_isolation.py` proves at the session level,
    now proven through the actual HTTP surface a browser or CI job would use."""
    owner_token = _register(client, "org-a-http")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    project = client.post(
        "/api/v1/projects", json={"name": "private"}, headers=owner_headers
    ).json()

    other_token = _register(client, "org-b-http")
    other_headers = {"Authorization": f"Bearer {other_token}"}

    listing = client.get("/api/v1/projects", headers=other_headers)
    assert listing.status_code == 200
    assert listing.json() == []

    bugs = client.get(f"/api/v1/projects/{project['id']}/bugs", headers=other_headers)
    assert bugs.status_code == 200
    assert bugs.json() == []
