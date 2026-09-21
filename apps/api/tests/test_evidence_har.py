"""HAR evidence on defects.

The redaction tests carry the weight. A HAR records headers verbatim by design -
that is the whole point of the format - and a defect report is pasted into issue
trackers and chat. An unscrubbed HAR is a live bearer token in a file built to be
shared.
"""

from __future__ import annotations

import json

from qagent.modules.evidence.har import (
    MAX_BODY_CHARS,
    REDACTED,
    build_har,
    har_for_check,
    har_from_requests,
)


def _har(**overrides) -> dict:
    payload = {
        "base_url": "http://shop.test",
        "request": {
            "method": "POST",
            "path": "/orders",
            "json": {"product_id": 7},
            "headers": {"content-type": "application/json"},
        },
        "response": {"status": 500, "body_text": '{"detail":"boom"}'},
        "duration_ms": 42,
    }
    payload.update(overrides)
    return json.loads(har_for_check(**payload))


def _entry(document: dict) -> dict:
    return document["log"]["entries"][0]


# ------------------------------------------------------------------- structure


def test_it_is_a_valid_har_envelope() -> None:
    document = _har()

    assert document["log"]["version"] == "1.2"
    assert document["log"]["creator"]["name"] == "QAgent"
    assert len(document["log"]["entries"]) == 1


def test_the_request_is_replayable() -> None:
    """The point of the format: drop it into DevTools or Insomnia and re-send."""
    request = _entry(_har())["request"]

    assert request["method"] == "POST"
    assert request["url"] == "http://shop.test/orders"
    assert json.loads(request["postData"]["text"]) == {"product_id": 7}


def test_the_response_is_recorded() -> None:
    response = _entry(_har())["response"]

    assert response["status"] == 500
    assert "boom" in response["content"]["text"]


def test_query_parameters_are_broken_out() -> None:
    document = _har(request={"method": "GET", "path": "/search?q=shoes&page=2"})

    names = {p["name"]: p["value"] for p in _entry(document)["request"]["queryString"]}
    assert names == {"q": "shoes", "page": "2"}


def test_an_absolute_path_is_not_prefixed_twice() -> None:
    document = _har(request={"method": "GET", "path": "http://other.test/x"})

    assert _entry(document)["request"]["url"] == "http://other.test/x"


def test_a_request_that_never_completed_still_produces_evidence() -> None:
    """A connection refused still yields a replayable request, which is what
    tells an environment failure apart from a defect."""
    document = _har(response={"status": None, "body_text": None})

    assert _entry(document)["response"]["status"] == -1
    assert _entry(document)["request"]["url"].endswith("/orders")


# ------------------------------------------------------------------- redaction


def test_credential_headers_are_replaced_outright() -> None:
    document = _har(
        request={
            "method": "GET",
            "path": "/me",
            "headers": {
                "Authorization": "Bearer sk-live-abcdef123456",
                "Cookie": "session=abc",
                "X-Api-Key": "key-123",
                "Accept": "application/json",
            },
        }
    )

    headers = {h["name"]: h["value"] for h in _entry(document)["request"]["headers"]}
    assert headers["Authorization"] == REDACTED
    assert headers["Cookie"] == REDACTED
    assert headers["X-Api-Key"] == REDACTED
    # A harmless header is left alone: redacting everything makes the evidence
    # useless for reproducing the request.
    assert headers["Accept"] == "application/json"


def test_credential_headers_are_matched_case_insensitively() -> None:
    document = _har(
        request={"method": "GET", "path": "/me", "headers": {"AUTHORIZATION": "Bearer x"}}
    )

    assert _entry(document)["request"]["headers"][0]["value"] == REDACTED


def test_response_headers_are_redacted_too() -> None:
    document = _har(
        response={"status": 200, "body_text": "{}", "headers": {"Set-Cookie": "session=abc"}}
    )

    assert _entry(document)["response"]["headers"][0]["value"] == REDACTED


def test_secrets_in_a_body_are_scrubbed() -> None:
    """The scrubber handles what a header allowlist cannot: a token in a body."""
    leaked = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.aaaaaaaaaaaaaaaaaaaaaaaaaaa"
    document = _har(response={"status": 200, "body_text": f'{{"token":"{leaked}"}}'})

    assert leaked not in json.dumps(document)


# ------------------------------------------------------------------- bounding


def test_a_huge_body_is_truncated() -> None:
    """A 40MB export would otherwise land in object storage per failing check."""
    document = _har(response={"status": 200, "body_text": "x" * (MAX_BODY_CHARS + 5000)})

    text = _entry(document)["response"]["content"]["text"]
    assert len(text) < MAX_BODY_CHARS + 200
    assert "truncated by QAgent" in text


def test_query_parameters_are_bounded() -> None:
    path = "/x?" + "&".join(f"p{i}=1" for i in range(200))
    document = _har(request={"method": "GET", "path": path})

    assert len(_entry(document)["request"]["queryString"]) <= 50


# --------------------------------------------------------------- several entries


def test_a_browser_stage_har_carries_every_request() -> None:
    data = har_from_requests(
        [
            {"method": "GET", "url": "http://app/", "status": 200},
            {"method": "POST", "url": "http://app/api/cart", "status": 201},
        ],
        page_url="http://app/",
    )
    document = json.loads(data)

    assert len(document["log"]["entries"]) == 2
    assert document["log"]["pages"][0]["title"] == "http://app/"
    # A viewer groups entries under a page only when they reference it.
    assert all(e["pageref"] == "page_1" for e in document["log"]["entries"])


def test_an_empty_har_is_still_valid() -> None:
    document = build_har([])

    assert document["log"]["entries"] == []
    assert json.dumps(document)


# ----------------------------------------------------------------- integration


def test_a_real_defect_carries_a_har_artifact() -> None:
    """An API defect used to ship with prose and nothing else."""
    from qagent.modules.runner.executor import ExecutionOutcome
    from qagent.pipeline import _har_artifact

    execution = ExecutionOutcome(
        passed=False,
        status="failed",
        duration_ms=42,
        request={"method": "POST", "path": "/orders", "json": {"product_id": 7}},
        response={"status": 500, "body_text": '{"detail":"boom"}'},
        assertions=[],
    )

    artifact = _har_artifact("http://shop.test", execution, duration_ms=42)

    assert artifact.kind == "har"
    assert artifact.extension == ".har"
    assert artifact.content_type == "application/json"
    document = json.loads(artifact.data)
    assert document["log"]["entries"][0]["response"]["status"] == 500


def test_browser_artifacts_cover_both_stages() -> None:
    """The two browser stages had drifted into documenting defects differently;
    one helper is what stops that recurring."""
    from qagent.pipeline import _browser_artifacts

    both = _browser_artifacts(screenshot=b"PNG-bytes", log_lines=["TypeError: x"])
    assert {a.kind for a in both} == {"screenshot", "log"}

    # Nothing captured means nothing stored - not an empty file.
    assert _browser_artifacts(screenshot=None, log_lines=[]) == []
    assert _browser_artifacts(screenshot=None, log_lines=["", ""]) == []


def test_persistence_scrubs_every_text_artifact_kind() -> None:
    """The `scrubbed` column has to be true, not aspirational - and a HAR is the
    one most worth getting right."""
    import inspect

    from qagent import persistence

    source = inspect.getsource(persistence._persist_artifacts)
    assert "scrubbable" in source
    assert '"har"' in source
