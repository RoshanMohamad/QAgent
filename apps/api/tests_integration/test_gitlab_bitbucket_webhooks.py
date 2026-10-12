"""GitLab and Bitbucket push webhooks queue scans, against a real database.

The receivers share `_queue_push_scans` with the GitHub one, which
test_github_webhook.py drives all the way through `run_scan`; this suite stops
at "queued" and spends its assertions on what differs per host - how a
delivery proves itself, and that a repository only answers on its own host's
endpoint.
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

WEBHOOK_SECRET = "host-webhook-secret"
SHA = "d" * 40


@pytest.fixture
def client(monkeypatch):
    from qagent.main import app
    from qagent.worker.tasks import run_scan

    monkeypatch.setattr(run_scan, "delay", lambda *args, **kwargs: None)
    return TestClient(app)


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


def _setup(client, Session, monkeypatch, slug: str, provider: str, url: str, base_url: str):
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
    org_id = registered["org_id"]
    project = client.post("/api/v1/projects", json={"name": "p"}, headers=headers).json()
    client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={"base_url": base_url},
        headers=headers,
    )
    with Session() as session:
        set_tenant(session, org_id)
        repository = models.Repository(
            org_id=UUID(org_id),
            project_id=UUID(project["id"]),
            provider=provider,
            url=url,
            default_branch="main",
        )
        session.add(repository)
        session.commit()
        repository_id = str(repository.id)

    configured = client.put(
        f"/api/v1/projects/{project['id']}/repositories/{repository_id}/webhook",
        json={"secret_ref": "host-webhook"},
        headers=headers,
    )
    assert configured.status_code == 200, configured.text
    monkeypatch.setenv(env_var_name("host-webhook", org_id=UUID(org_id)), WEBHOOK_SECRET)
    return org_id, repository_id, configured.json()


def _gitlab_body(ref: str = "refs/heads/main") -> bytes:
    return json.dumps(
        {
            "object_kind": "push",
            "ref": ref,
            "after": SHA,
            "checkout_sha": SHA,
            "project": {"path_with_namespace": "acme/platform/widgets"},
        }
    ).encode()


def _bitbucket_body(*branches: str) -> bytes:
    return json.dumps(
        {
            "push": {
                "changes": [
                    {"new": {"type": "branch", "name": b, "target": {"hash": SHA}}, "old": None}
                    for b in branches
                ]
            },
            "repository": {"full_name": "acme/widgets"},
        }
    ).encode()


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_gitlab_push_with_the_right_token_queues_a_scan(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    org_id, repository_id, configured = _setup(
        client, Session, monkeypatch, "gl-push", "gitlab",
        "https://gitlab.com/acme/platform/widgets.git", fixture_app_url,
    )
    assert configured["callback_url"] == f"/api/v1/webhooks/gitlab/{org_id}/{repository_id}"
    assert configured["events"] == ["Push events"]

    response = client.post(
        configured["callback_url"],
        content=_gitlab_body(),
        headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["commit_sha"] == SHA


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_gitlab_push_without_the_right_token_is_401(
    client, Session, fixture_app_url, monkeypatch, token
) -> None:
    org_id, repository_id, configured = _setup(
        client, Session, monkeypatch, f"gl-bad-{'token' if token else 'none'}", "gitlab",
        "https://gitlab.com/acme/widgets.git", fixture_app_url,
    )
    headers = {"X-Gitlab-Event": "Push Hook"}
    if token:
        headers["X-Gitlab-Token"] = token

    response = client.post(configured["callback_url"], content=_gitlab_body(), headers=headers)

    assert response.status_code == 401


def test_bitbucket_push_queues_only_the_default_branch(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    org_id, repository_id, configured = _setup(
        client, Session, monkeypatch, "bb-push", "bitbucket",
        "https://bitbucket.org/acme/widgets.git", fixture_app_url,
    )
    assert configured["callback_url"] == f"/api/v1/webhooks/bitbucket/{org_id}/{repository_id}"
    body = _bitbucket_body("feature-x", "main")

    response = client.post(
        configured["callback_url"],
        content=body,
        headers={"X-Event-Key": "repo:push", "X-Hub-Signature": _sign(body)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"

    only_feature = _bitbucket_body("feature-x")
    ignored = client.post(
        configured["callback_url"],
        content=only_feature,
        headers={"X-Event-Key": "repo:push", "X-Hub-Signature": _sign(only_feature)},
    )
    assert ignored.json() == {"status": "ignored", "reason": "not the default branch"}


def test_bitbucket_bad_signature_is_401(client, Session, fixture_app_url, monkeypatch) -> None:
    _, _, configured = _setup(
        client, Session, monkeypatch, "bb-badsig", "bitbucket",
        "https://bitbucket.org/acme/widgets.git", fixture_app_url,
    )
    body = _bitbucket_body("main")

    response = client.post(
        configured["callback_url"],
        content=body,
        headers={"X-Event-Key": "repo:push", "X-Hub-Signature": _sign(body, "wrong")},
    )

    assert response.status_code == 401


def test_a_repository_only_answers_on_its_own_hosts_endpoint(
    client, Session, fixture_app_url, monkeypatch
) -> None:
    """A GitLab repository's secret is a bare token; the GitHub receiver must
    not even look it up, let alone try it as an HMAC key."""
    org_id, repository_id, _ = _setup(
        client, Session, monkeypatch, "gl-wrong-host", "gitlab",
        "https://gitlab.com/acme/widgets.git", fixture_app_url,
    )
    body = _gitlab_body()

    for path, headers in [
        ("github", {"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(body)}),
        ("bitbucket", {"X-Event-Key": "repo:push", "X-Hub-Signature": _sign(body)}),
    ]:
        response = client.post(
            f"/api/v1/webhooks/{path}/{org_id}/{repository_id}", content=body, headers=headers
        )
        assert response.status_code == 404, path
