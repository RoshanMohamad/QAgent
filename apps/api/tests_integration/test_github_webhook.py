"""A GitHub push webhook queues a real scan, against a real database.

register -> create project + environment -> connect a repository -> configure
its webhook secret -> POST a correctly signed push -> the returned run reaches
a terminal state through the real (in-process) `run_scan` task, same as a
manually started run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from qagent import models
from qagent.db import set_tenant
from qagent.modules.secrets.store import env_var_name

WEBHOOK_SECRET = "gh-webhook-secret"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    from qagent.main import app
    from qagent.worker.tasks import run_scan

    # Queued in-process below; a live worker from another module must not race it.
    monkeypatch.setattr(run_scan, "delay", lambda *args, **kwargs: None)
    return TestClient(app)


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


def _register(client: TestClient, slug: str) -> tuple[dict, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "org_name": slug,
            "org_slug": slug,
            "email": f"owner@{slug}.example",
            "password": "correct-horse-battery-staple",
        },
    ).json()
    return {"Authorization": f"Bearer {registered['access_token']}"}, registered["org_id"]


def _add_repository(Session, org_id: str, project_id: str, default_branch: str = "main") -> str:
    """Inserted directly rather than through `/connect`, which really clones -
    this suite is about the webhook receiver, not GitHub reachability."""
    with Session() as session:
        set_tenant(session, org_id)
        repository = models.Repository(
            org_id=UUID(org_id),
            project_id=UUID(project_id),
            url="https://github.com/acme/widgets",
            default_branch=default_branch,
        )
        session.add(repository)
        session.commit()
        return str(repository.id)


def _setup(
    client: TestClient, Session, slug: str, base_url: str, default_branch: str = "main"
) -> tuple[dict, str, str, str]:
    """Returns (headers, org_id, project_id, repository_id), webhook configured."""
    headers, org_id = _register(client, slug)
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": base_url},
        headers=headers,
    )
    repository_id = _add_repository(Session, org_id, project["id"], default_branch)

    configured = client.put(
        f"/api/v1/projects/{project['id']}/repositories/{repository_id}/webhook",
        json={"secret_ref": "gh-webhook"},
        headers=headers,
    )
    assert configured.status_code == 200, configured.text

    return headers, org_id, project["id"], repository_id


def test_a_correctly_signed_push_queues_a_scan(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    from qagent.worker.tasks import run_scan

    headers, org_id, project_id, repository_id = _setup(
        client, Session, "webhook-push", fixture_app_url
    )
    monkeypatch.setenv(env_var_name("gh-webhook", org_id=UUID(org_id)), WEBHOOK_SECRET)

    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "d" * 40,
            "repository": {"full_name": "acme/widgets"},
        }
    ).encode()

    response = client.post(
        f"/api/v1/webhooks/github/{org_id}/{repository_id}",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    (run_id,) = payload["run_ids"]

    run_scan.apply(args=[org_id, project_id, run_id])

    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["status"] == "failed"  # the fixture app's seeded defects
    assert run["commit_sha"] is not None


def test_an_incorrectly_signed_push_is_rejected_and_nothing_is_queued(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    headers, org_id, project_id, repository_id = _setup(
        client, Session, "webhook-badsig", fixture_app_url
    )
    monkeypatch.setenv(env_var_name("gh-webhook", org_id=UUID(org_id)), WEBHOOK_SECRET)
    body = json.dumps({"ref": "refs/heads/main", "after": "d" * 40,
                        "repository": {"full_name": "acme/widgets"}}).encode()

    response = client.post(
        f"/api/v1/webhooks/github/{org_id}/{repository_id}",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
        },
    )

    assert response.status_code == 401

    runs = client.get(f"/api/v1/projects/{project_id}/bugs", headers=headers)
    assert runs.status_code == 200  # the org itself is untouched, sanity check


def test_a_push_before_the_webhook_is_configured_is_404(client, Session, fixture_app_url) -> None:
    headers, org_id = _register(client, "webhook-unconfigured")
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    repository_id = _add_repository(Session, org_id, project["id"])

    response = client.post(
        f"/api/v1/webhooks/github/{org_id}/{repository_id}",
        content=b"{}",
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(b"{}")},
    )

    assert response.status_code == 404


def test_a_push_to_a_non_default_branch_is_ignored(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    headers, org_id, project_id, repository_id = _setup(
        client, Session, "webhook-branch", fixture_app_url
    )
    monkeypatch.setenv(env_var_name("gh-webhook", org_id=UUID(org_id)), WEBHOOK_SECRET)
    body = json.dumps(
        {
            "ref": "refs/heads/feature-x",
            "after": "d" * 40,
            "repository": {"full_name": "acme/widgets"},
        }
    ).encode()

    response = client.post(
        f"/api/v1/webhooks/github/{org_id}/{repository_id}",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_a_ping_event_is_answered_without_a_repository_lookup_failure(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    headers, org_id, project_id, repository_id = _setup(
        client, Session, "webhook-ping", fixture_app_url
    )
    monkeypatch.setenv(env_var_name("gh-webhook", org_id=UUID(org_id)), WEBHOOK_SECRET)

    response = client.post(
        f"/api/v1/webhooks/github/{org_id}/{repository_id}",
        content=b"{}",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sign(b"{}")},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "pong"}
