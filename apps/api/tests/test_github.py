"""GitHub issue sync: exercised against httpx.MockTransport, no real network."""

from __future__ import annotations

import httpx
import pytest

from qagent.modules.integrations.github import (
    GithubSyncError,
    render_issue_body,
    sync_bugs_to_github,
)


def _outcome(title: str, **bug_overrides) -> dict:
    return {
        "name": f"check for {title}",
        "endpoint": "POST /orders",
        "bug": {
            "title": title,
            "severity": "high",
            "expected": "400",
            "actual": "500",
            "root_cause": "missing null check",
            "suggested_fix": "validate before use",
            **bug_overrides,
        },
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_render_issue_body_includes_all_fields() -> None:
    body = render_issue_body(
        case_name="checkout crashes",
        endpoint="POST /checkout",
        bug={
            "severity": "critical",
            "expected": "400",
            "actual": "500",
            "root_cause": "null product",
            "suggested_fix": "guard the lookup",
        },
    )
    assert "critical" in body
    assert "checkout crashes" in body
    assert "POST /checkout" in body
    assert "null product" in body
    assert "guard the lookup" in body


def test_creates_issue_when_none_exists() -> None:
    created_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/repos/acme/shop/issues" and request.method == "POST":
            created_payloads.append(request)
            return httpx.Response(201, json={"number": 42, "html_url": "https://github.com/x/42"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    results = sync_bugs_to_github(
        bugs=[_outcome("Checkout crashes on deleted product")],
        repo="acme/shop",
        token="fake-token",
        client=_client(handler),
    )

    assert len(results) == 1
    assert results[0].action == "created"
    assert results[0].issue_number == 42
    assert len(created_payloads) == 1


def test_skips_when_issue_already_exists_by_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "title": "Checkout crashes on deleted product",
                            "number": 7,
                            "html_url": "https://github.com/x/7",
                        }
                    ]
                },
            )
        raise AssertionError("should not create a duplicate issue")

    results = sync_bugs_to_github(
        bugs=[_outcome("Checkout crashes on deleted product")],
        repo="acme/shop",
        token="fake-token",
        client=_client(handler),
    )

    assert results[0].action == "skipped_existing"
    assert results[0].issue_number == 7


def test_dry_run_never_calls_create() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": []})
        raise AssertionError("dry-run must not create issues")

    results = sync_bugs_to_github(
        bugs=[_outcome("Some defect")],
        repo="acme/shop",
        token="fake-token",
        dry_run=True,
        client=_client(handler),
    )

    assert results[0].action == "dry_run"


def test_search_failure_raises_github_sync_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"})

    with pytest.raises(GithubSyncError):
        sync_bugs_to_github(
            bugs=[_outcome("Some defect")],
            repo="acme/shop",
            token="fake-token",
            client=_client(handler),
        )


def test_create_failure_raises_github_sync_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(422, json={"message": "validation failed"})

    with pytest.raises(GithubSyncError):
        sync_bugs_to_github(
            bugs=[_outcome("Some defect")],
            repo="acme/shop",
            token="fake-token",
            client=_client(handler),
        )


def test_falls_back_to_check_name_when_bug_has_no_title() -> None:
    outcome = _outcome("placeholder")
    del outcome["bug"]["title"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": []})
        body = request.read()
        assert b"check for placeholder" in body
        return httpx.Response(201, json={"number": 1, "html_url": "https://github.com/x/1"})

    results = sync_bugs_to_github(
        bugs=[outcome], repo="acme/shop", token="fake-token", client=_client(handler)
    )
    assert results[0].bug_title == "check for placeholder"
