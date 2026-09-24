"""Verifying a fix end to end: a real scan of buggy-shop files real defects,
then `POST /bugs/{id}/verify` replays one of them against the still-broken app
through the real task. The seeded defect is not fixed, so the only correct
verdict is `still_failing` - a verifier that says "fixed" here is lying.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from qagent.main import app
    from qagent.worker.tasks import run_scan, verify_bug

    # Executed in-process below; a live worker from another module must not race it.
    monkeypatch.setattr(run_scan, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(verify_bug, "delay", lambda *args, **kwargs: None)
    return TestClient(app)


def _register(client: TestClient, slug: str) -> tuple[str, dict]:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": slug,
            "org_slug": slug,
            "email": f"owner@{slug}.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    return registered["org_id"], {"Authorization": f"Bearer {registered['access_token']}"}


def test_verifying_an_unfixed_defect_reports_still_failing(client, fixture_app_url) -> None:
    org_id, headers = _register(client, "verify-fix")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    environment = client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": fixture_app_url},
        headers=headers,
    ).json()
    run = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"environment_id": environment["id"], "trigger": "manual"},
        headers=headers,
    ).json()

    from qagent.worker.tasks import run_scan, verify_bug

    run_scan.apply(args=[org_id, project["id"], run["id"]])

    bugs = client.get(f"/api/v1/projects/{project['id']}/bugs", headers=headers).json()
    assert bugs, "the seeded defects should have been filed"

    verified = None
    for bug in bugs:
        response = client.post(f"/api/v1/bugs/{bug['id']}/verify", json={}, headers=headers)
        if response.status_code == 202:
            verified = bug, response.json()
            break
        assert response.status_code == 409, response.text  # not replayable, e.g. IDOR
    assert verified is not None, "at least one API defect should be replayable"
    bug, queued = verified

    summary = verify_bug.apply(args=[org_id, bug["id"], queued["run_id"], 3]).get()

    assert summary["verdict"] == "still_failing", summary
    history = client.get(f"/api/v1/bugs/{bug['id']}/history", headers=headers).json()
    assert history["bug"]["status"] == "open"
    assert history["events"][-1]["event"] == "verification_failed"
    assert history["events"][-1]["detail"]["attempts"] == 3
    run_status = client.get(f"/api/v1/runs/{queued['run_id']}", headers=headers).json()["status"]
    assert run_status == "failed"


def test_verifying_an_unknown_bug_is_404(client) -> None:
    _, headers = _register(client, "verify-404")

    response = client.post(
        "/api/v1/bugs/00000000-0000-0000-0000-000000000000/verify", json={}, headers=headers
    )

    assert response.status_code == 404
