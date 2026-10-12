"""GitLab and Bitbucket webhook verification and push parsing
(modules/webhooks/gitlab.py, modules/webhooks/bitbucket.py) - no database, no
network. Payloads are trimmed copies of each host's documented push body."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from qagent.modules.webhooks import bitbucket, gitlab

SECRET = "shh-its-a-secret"
SHA = "cafef00d" * 5


# --- GitLab -----------------------------------------------------------------


def _gitlab_push(**overrides) -> dict:
    body = {
        "object_kind": "push",
        "event_name": "push",
        "before": "0" * 40,
        "after": SHA,
        "ref": "refs/heads/main",
        "checkout_sha": SHA,
        "project": {"path_with_namespace": "acme/platform/widgets"},
    }
    body.update(overrides)
    return body


def test_gitlab_token_must_match_exactly() -> None:
    assert gitlab.verify_token(SECRET, SECRET) is True
    assert gitlab.verify_token("wrong", SECRET) is False
    assert gitlab.verify_token(SECRET + " ", SECRET) is False


@pytest.mark.parametrize("header", [None, ""])
def test_gitlab_delivery_without_a_token_is_refused(header: str | None) -> None:
    assert gitlab.verify_token(header, SECRET) is False


def test_gitlab_empty_secret_never_verifies() -> None:
    """An unset secret must not turn into "an empty token passes"."""
    assert gitlab.verify_token("", "") is False


def test_gitlab_parses_a_push_with_a_nested_group() -> None:
    event = gitlab.parse_push_event(_gitlab_push())
    assert event is not None
    assert (event.ref, event.commit_sha, event.repo_full_name, event.deleted) == (
        "refs/heads/main",
        SHA,
        "acme/platform/widgets",
        False,
    )


@pytest.mark.parametrize(
    "overrides",
    [{"after": "0" * 40, "checkout_sha": None}, {"checkout_sha": None}],
)
def test_gitlab_branch_deletion_is_flagged(overrides: dict) -> None:
    assert gitlab.parse_push_event(_gitlab_push(**overrides)).deleted is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        _gitlab_push(object_kind="tag_push"),
        _gitlab_push(object_kind="merge_request"),
        _gitlab_push(project={}),
        _gitlab_push(ref=1),
    ],
)
def test_gitlab_non_push_or_malformed_is_unparseable(body: dict) -> None:
    assert gitlab.parse_push_event(body) is None


# --- Bitbucket --------------------------------------------------------------


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _change(name: str | None, *, kind: str = "branch", sha: str = SHA, old: str | None = None):
    return {
        "new": None if name is None else {"type": kind, "name": name, "target": {"hash": sha}},
        "old": None if old is None else {"type": "branch", "name": old},
    }


def _bitbucket_push(*changes) -> dict:
    return {
        "push": {"changes": list(changes)},
        "repository": {"full_name": "acme/widgets"},
    }


def test_bitbucket_signature_is_the_github_hmac() -> None:
    body = json.dumps(_bitbucket_push(_change("main"))).encode()
    assert bitbucket.verify_signature(body, _sign(body), SECRET) is True
    assert bitbucket.verify_signature(body, _sign(body, "other"), SECRET) is False
    assert bitbucket.verify_signature(body + b" ", _sign(body), SECRET) is False
    assert bitbucket.verify_signature(body, None, SECRET) is False


def test_bitbucket_parses_every_branch_in_one_push() -> None:
    events = bitbucket.parse_push_events(
        _bitbucket_push(_change("feature", sha="1" * 40), _change("main"))
    )
    assert [(e.ref, e.commit_sha, e.deleted) for e in events] == [
        ("refs/heads/feature", "1" * 40, False),
        ("refs/heads/main", SHA, False),
    ]
    assert {e.repo_full_name for e in events} == {"acme/widgets"}


def test_bitbucket_deleted_branch_is_flagged_and_tags_are_skipped() -> None:
    events = bitbucket.parse_push_events(
        _bitbucket_push(_change(None, old="main"), _change("v1.0", kind="tag"))
    )
    assert [(e.ref, e.deleted) for e in events] == [("refs/heads/main", True)]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"push": {"changes": []}, "repository": {"full_name": "acme/widgets"}},
        {"push": {"changes": "nope"}, "repository": {"full_name": "acme/widgets"}},
        {"push": {"changes": [_change("main")]}, "repository": {}},
        {"push": {"changes": [{"new": {"type": "branch", "name": "main"}}]},
         "repository": {"full_name": "acme/widgets"}},
    ],
)
def test_bitbucket_malformed_push_yields_nothing(body: dict) -> None:
    assert bitbucket.parse_push_events(body) == []
