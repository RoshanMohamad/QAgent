"""Verify and parse a GitHub push webhook.

GitHub signs every delivery with HMAC-SHA256 over the raw request body, keyed
by a secret the repository owner also pasted into GitHub's webhook settings
(https://docs.github.com/webhooks/using-webhooks/validating-webhook-deliveries).
Verifying that signature is the *entire* authentication story here: the
endpoint takes no bearer token, because GitHub cannot send one. Everything
downstream of a failed check must refuse before it is read as JSON - a webhook
body is untrusted input exactly like repository or response content
(ADR-0004), and here it is unauthenticated untrusted input until proven
otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

_SIGNATURE_PREFIX = "sha256="


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time comparison, as every HMAC verification must be - a
    timing side-channel here would leak the secret one byte at a time."""
    if not signature_header or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len(_SIGNATURE_PREFIX):])


@dataclass(frozen=True)
class PushEvent:
    ref: str
    commit_sha: str
    repo_full_name: str
    deleted: bool


def parse_push_event(body: dict) -> PushEvent | None:
    """The fields QAgent acts on, or ``None`` for a payload that is not a
    push carrying a commit (a branch deletion, or a malformed body).

    Deliberately narrow: this reads three fields from a JSON object GitHub
    itself will keep extending, and picking out a few named keys is what
    keeps a future GitHub payload change from breaking this parser the way an
    exhaustive schema would.
    """
    ref = body.get("ref")
    commit_sha = body.get("after")
    repo = body.get("repository") or {}
    full_name = repo.get("full_name")

    if not isinstance(ref, str) or not isinstance(commit_sha, str) or not isinstance(
        full_name, str
    ):
        return None
    # "0000...0000": GitHub's sentinel for a deleted ref - there is no commit
    # to scan, and treating it as one would either error or scan whatever the
    # last real commit happened to leave checked out.
    deleted = bool(body.get("deleted")) or set(commit_sha) == {"0"}
    return PushEvent(ref=ref, commit_sha=commit_sha, repo_full_name=full_name, deleted=deleted)
