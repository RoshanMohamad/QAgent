"""GitHub webhook signature verification and push-event parsing
(modules/webhooks/github.py) - no database, no network."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from qagent.modules.webhooks.github import parse_push_event, verify_signature

SECRET = "shh-its-a-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correctly_signed_payload_verifies() -> None:
    body = b'{"ref": "refs/heads/main"}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_a_payload_signed_with_the_wrong_secret_is_rejected() -> None:
    body = b'{"ref": "refs/heads/main"}'
    assert verify_signature(body, _sign(body, "wrong-secret"), SECRET) is False


def test_a_tampered_body_is_rejected_even_with_a_valid_looking_signature() -> None:
    original = b'{"ref": "refs/heads/main"}'
    signature = _sign(original)
    tampered = b'{"ref": "refs/heads/evil"}'
    assert verify_signature(tampered, signature, SECRET) is False


@pytest.mark.parametrize(
    "header",
    [None, "", "not-a-signature", "sha1=deadbeef", "sha256="],
)
def test_a_missing_or_malformed_header_is_rejected(header: str | None) -> None:
    assert verify_signature(b"{}", header, SECRET) is False


def test_parses_a_push_to_a_named_ref() -> None:
    body = {
        "ref": "refs/heads/main",
        "after": "d73fa1cd" * 5,
        "repository": {"full_name": "acme/widgets"},
    }
    event = parse_push_event(body)
    assert event is not None
    assert (event.ref, event.commit_sha, event.repo_full_name, event.deleted) == (
        "refs/heads/main",
        "d73fa1cd" * 5,
        "acme/widgets",
        False,
    )


def test_a_branch_deletion_is_flagged_even_without_the_zero_sha() -> None:
    body = {
        "ref": "refs/heads/old-feature",
        "after": "0" * 40,
        "repository": {"full_name": "acme/widgets"},
        "deleted": True,
    }
    assert parse_push_event(body).deleted is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ref": "refs/heads/main"},  # no after, no repository
        {"ref": "refs/heads/main", "after": "abc"},  # no repository
        {"ref": 1, "after": "abc", "repository": {"full_name": "a/b"}},  # wrong type
    ],
)
def test_a_payload_missing_the_fields_qagent_reads_is_unparseable(body: dict) -> None:
    assert parse_push_event(body) is None


def test_round_trips_a_realistic_github_payload() -> None:
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": "cafef00d" * 5,
            "repository": {"full_name": "acme/widgets", "default_branch": "main"},
            "pusher": {"name": "octocat"},
        }
    ).encode()

    assert verify_signature(body, _sign(body), SECRET) is True
    event = parse_push_event(json.loads(body))
    assert event.commit_sha == "cafef00d" * 5
