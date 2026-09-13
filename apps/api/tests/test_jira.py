"""Jira issue sync: exercised against httpx.MockTransport, no real network."""

from __future__ import annotations

import httpx
import pytest

from qagent.modules.integrations.jira import (
    JiraSyncError,
    _text_to_adf,
    render_issue_body,
    sync_bugs_to_jira,
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
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.atlassian.net")


def test_text_to_adf_one_paragraph_per_line() -> None:
    doc = _text_to_adf("line one\n\nline two")
    assert doc["type"] == "doc"
    assert len(doc["content"]) == 3
    assert doc["content"][0]["content"][0]["text"] == "line one"
    assert doc["content"][1]["content"] == []  # blank line -> empty paragraph
    assert doc["content"][2]["content"][0]["text"] == "line two"


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
        if request.url.path == "/rest/api/3/search":
            return httpx.Response(200, json={"issues": []})
        if request.url.path == "/rest/api/3/issue" and request.method == "POST":
            created_payloads.append(request)
            return httpx.Response(201, json={"key": "QA-42"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    results = sync_bugs_to_jira(
        bugs=[_outcome("Checkout crashes on deleted product")],
        base_url="https://fake.atlassian.net",
        project_key="QA",
        email="bot@example.test",
        api_token="fake-token",
        client=_client(handler),
    )

    assert len(results) == 1
    assert results[0].action == "created"
    assert results[0].issue_key == "QA-42"
    assert results[0].issue_url == "https://fake.atlassian.net/browse/QA-42"
    assert len(created_payloads) == 1


def test_skips_when_issue_already_exists_by_exact_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/search":
            return httpx.Response(
                200,
                json={
                    "issues": [
                        {"key": "QA-7", "fields": {"summary": "Checkout crashes on deleted product"}}
                    ]
                },
            )
        raise AssertionError("should not create a duplicate issue")

    results = sync_bugs_to_jira(
        bugs=[_outcome("Checkout crashes on deleted product")],
        base_url="https://fake.atlassian.net",
        project_key="QA",
        email="bot@example.test",
        api_token="fake-token",
        client=_client(handler),
    )

    assert results[0].action == "skipped_existing"
    assert results[0].issue_key == "QA-7"


def test_fuzzy_jql_match_is_filtered_to_exact_title() -> None:
    """JQL's ~ operator is a fuzzy text match; a similar-but-different summary
    must not be treated as the same bug."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/search":
            return httpx.Response(
                200, json={"issues": [{"key": "QA-1", "fields": {"summary": "Checkout is slow"}}]}
            )
        if request.url.path == "/rest/api/3/issue":
            return httpx.Response(201, json={"key": "QA-2"})
        raise AssertionError("unexpected request")

    results = sync_bugs_to_jira(
        bugs=[_outcome("Checkout crashes on deleted product")],
        base_url="https://fake.atlassian.net",
        project_key="QA",
        email="bot@example.test",
        api_token="fake-token",
        client=_client(handler),
    )

    assert results[0].action == "created"


def test_dry_run_never_calls_create() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/search":
            return httpx.Response(200, json={"issues": []})
        raise AssertionError("dry-run must not create issues")

    results = sync_bugs_to_jira(
        bugs=[_outcome("Some defect")],
        base_url="https://fake.atlassian.net",
        project_key="QA",
        email="bot@example.test",
        api_token="fake-token",
        dry_run=True,
        client=_client(handler),
    )

    assert results[0].action == "dry_run"


def test_search_failure_raises_jira_sync_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["unauthorized"]})

    with pytest.raises(JiraSyncError):
        sync_bugs_to_jira(
            bugs=[_outcome("Some defect")],
            base_url="https://fake.atlassian.net",
            project_key="QA",
            email="bot@example.test",
            api_token="fake-token",
            client=_client(handler),
        )


def test_create_failure_raises_jira_sync_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/search":
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(400, json={"errorMessages": ["invalid issue type"]})

    with pytest.raises(JiraSyncError):
        sync_bugs_to_jira(
            bugs=[_outcome("Some defect")],
            base_url="https://fake.atlassian.net",
            project_key="QA",
            email="bot@example.test",
            api_token="fake-token",
            client=_client(handler),
        )
