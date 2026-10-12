"""Verify and parse a Bitbucket push webhook.

Bitbucket signs a delivery the same way GitHub does once the webhook has a
secret: HMAC-SHA256 over the raw body, hex-encoded, as ``sha256=<hex>`` - in
``X-Hub-Signature`` rather than GitHub's ``X-Hub-Signature-256``
(https://support.atlassian.com/bitbucket-cloud/docs/manage-webhooks/#Secure-webhooks).
So the verification itself is GitHub's, reused rather than re-implemented.

The payload is the part that differs: one ``repo:push`` delivery can carry
several ref changes (``push.changes[]``), each with a ``new`` state that is
null when the branch was deleted.
"""

from __future__ import annotations

from qagent.modules.webhooks.github import PushEvent
from qagent.modules.webhooks.github import verify_signature as verify_signature

__all__ = ["parse_push_events", "verify_signature"]


def parse_push_events(body: dict) -> list[PushEvent]:
    """Every branch update in a push, as `PushEvent`s with GitHub-style refs.

    Tags are skipped - only a branch has a default to compare against - and a
    deleted branch is reported with ``deleted=True`` so the caller ignores it
    the same way it ignores a GitHub deletion.
    """
    repository = body.get("repository") or {}
    full_name = repository.get("full_name")
    push = body.get("push") or {}
    changes = push.get("changes") if isinstance(push, dict) else None
    if not isinstance(full_name, str) or not isinstance(changes, list):
        return []

    events: list[PushEvent] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        new = change.get("new")
        old = change.get("old")
        if new is None:
            if isinstance(old, dict) and old.get("type") == "branch" and old.get("name"):
                events.append(
                    PushEvent(
                        ref=f"refs/heads/{old['name']}",
                        commit_sha="0" * 40,
                        repo_full_name=full_name,
                        deleted=True,
                    )
                )
            continue
        if not isinstance(new, dict) or new.get("type") != "branch":
            continue
        name = new.get("name")
        commit_sha = (new.get("target") or {}).get("hash")
        if not isinstance(name, str) or not isinstance(commit_sha, str):
            continue
        events.append(
            PushEvent(
                ref=f"refs/heads/{name}",
                commit_sha=commit_sha,
                repo_full_name=full_name,
                deleted=False,
            )
        )
    return events
