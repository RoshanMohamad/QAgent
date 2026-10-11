"""Section 20's browser_sessions / recorded_actions / ai_analysis tables,
through the API that writes them - including that RLS covers all three.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from qagent.main import app

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


_SESSION = {
    "version": 1,
    "start_url": "http://shop.test/login",
    "actions": [
        {"type": "fill", "target": {"selector": "#email", "tag": "input"}, "value": "a@b.c"},
        {"type": "click", "target": {"selector": "#checkout", "tag": "button"}},
        {"type": "console_error", "message": "TypeError: price is undefined"},
    ],
    "requests": [
        {"method": "POST", "url": "http://shop.test/api/orders", "status": 201},
        {"method": "GET", "url": "https://analytics.example.com/t", "status": 200},
    ],
}


def test_a_recording_is_stored_with_ordered_actions_and_derived_checks(client) -> None:
    headers = _owner(client, "rec-store")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()

    created = client.post(
        f"/api/v1/projects/{project['id']}/recordings",
        json={"document": _SESSION, "name": "checkout", "base_url": "http://shop.test"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["api_cases"] == 1  # analytics traffic filtered by base_url
    assert any("TypeError" in d for d in body["diagnostics"])

    [listed] = client.get(f"/api/v1/projects/{project['id']}/recordings", headers=headers).json()
    assert listed["name"] == "checkout"

    detail = client.get(f"/api/v1/recordings/{body['id']}", headers=headers).json()
    assert [a["selector"] for a in detail["actions"]] == ["#email", "#checkout"]
    assert [a["sequence"] for a in detail["actions"]] == [0, 1]
    assert detail["api_cases"][0]["spec"]["request"]["path"] == "/api/orders"
    assert detail["ui_flow"]


def test_an_empty_session_is_refused(client) -> None:
    headers = _owner(client, "rec-empty")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()

    response = client.post(
        f"/api/v1/projects/{project['id']}/recordings",
        json={"document": {"actions": "not-a-list"}},
        headers=headers,
    )

    assert response.status_code == 422


def test_recordings_are_invisible_to_another_organization(client) -> None:
    owner = _owner(client, "rec-owner")
    other = _owner(client, "rec-other")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=owner).json()
    recording = client.post(
        f"/api/v1/projects/{project['id']}/recordings",
        json={"document": _SESSION},
        headers=owner,
    ).json()

    assert client.get(f"/api/v1/recordings/{recording['id']}", headers=other).status_code == 404
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/recordings",
            json={"document": _SESSION},
            headers=other,
        ).status_code
        == 404
    )


def test_every_analysis_is_kept_while_the_project_holds_only_the_latest(
    client, tmp_path
) -> None:
    headers = _owner(client, "analysis-history")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()

    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    first = client.post(
        f"/api/v1/projects/{project['id']}/analyze",
        json={"repo_path": str(tmp_path)},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    (tmp_path / "requirements.txt").write_text("fastapi\nredis\n", encoding="utf-8")
    client.post(
        f"/api/v1/projects/{project['id']}/analyze",
        json={"repo_path": str(tmp_path)},
        headers=headers,
    )

    analyses = client.get(
        f"/api/v1/projects/{project['id']}/analyses?kind=project_analysis", headers=headers
    ).json()

    assert len(analyses) == 2
    assert analyses[0]["agent"] == "project_analyst"
    assert analyses[0]["source"] == str(tmp_path)
    assert analyses[0]["content"] != analyses[1]["content"]
