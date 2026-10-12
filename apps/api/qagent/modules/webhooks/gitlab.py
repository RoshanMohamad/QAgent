"""Verify and parse a GitLab push webhook.

GitLab does not sign deliveries. It sends the secret token configured on the
webhook verbatim in ``X-Gitlab-Token``
(https://docs.gitlab.com/user/project/integrations/webhooks/#validate-requests-with-a-secret-token),
so verification is a constant-time comparison of that header against the
stored secret. That is weaker than GitHub's HMAC - a token read off the wire
authenticates any body - which is acceptable only because deliveries arrive
over TLS, and is why this module never accepts a delivery with no token at
all. Same rule as the GitHub receiver otherwise: nothing is parsed before the
check passes (ADR-0004).
"""

from __future__ import annotations

import hmac

from qagent.modules.webhooks.github import PushEvent


def verify_token(token_header: str | None, secret: str) -> bool:
    if not token_header or not secret:
        return False
    return hmac.compare_digest(token_header.encode("utf-8"), secret.encode("utf-8"))


def parse_push_event(body: dict) -> PushEvent | None:
    """The fields QAgent acts on, or ``None`` for anything that is not a push.

    GitLab marks a branch deletion with an all-zero ``after`` and a null
    ``checkout_sha``; either is treated as a deletion.
    """
    if body.get("object_kind") != "push":
        return None
    ref = body.get("ref")
    commit_sha = body.get("after")
    project = body.get("project") or {}
    full_name = project.get("path_with_namespace")

    if not isinstance(ref, str) or not isinstance(commit_sha, str) or not isinstance(
        full_name, str
    ):
        return None
    deleted = set(commit_sha) == {"0"} or body.get("checkout_sha", commit_sha) is None
    return PushEvent(ref=ref, commit_sha=commit_sha, repo_full_name=full_name, deleted=deleted)
