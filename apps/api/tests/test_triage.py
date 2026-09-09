"""Triage regression tests.

The auth-bypass case exists because the classifier originally reported it as
`bad_assertion`: it saw a 2xx and concluded the test was over-specified, which hid
the most severe defect class the platform can find. The evaluation harness caught it.
"""

from __future__ import annotations

import pytest

from qagent.modules.triage.classifier import FailureClass, classify, extract_signals

AUTH_PROBE_SPEC = {
    "assertions": [
        {"type": "status_in", "value": [401, 403]},
        {"type": "status_not_in", "value": [500, 502, 503, 504]},
    ],
    "expectation": "An unauthenticated request is rejected.",
}

NEGATIVE_SPEC = {
    "assertions": [
        {"type": "status_in", "value": [400, 409, 422]},
        {"type": "status_not_in", "value": [500, 502, 503, 504]},
    ],
    "expectation": "Invalid input returns a 4xx.",
}

POSITIVE_SPEC = {
    "assertions": [{"type": "status_in", "value": [200, 201]}],
    "expectation": "A valid request succeeds.",
}


def build(spec, request, response, message=None, **kwargs):
    return extract_signals(
        spec=spec, request=request, response=response, failure_message=message, **kwargs
    )


def test_served_auth_probe_is_a_real_bug_not_a_bad_assertion():
    signals = build(
        AUTH_PROBE_SPEC,
        {"method": "GET", "path": "/admin/users", "auth": "none"},
        {"status": 200, "body_text": '[{"email":"ada@example.com"}]', "duration_ms": 3},
    )
    verdict = classify(signals)

    assert verdict.failure_class is FailureClass.REAL_BUG
    assert verdict.confidence >= 0.9
    assert "credentials" in verdict.reason.lower()


def test_rejected_auth_probe_is_not_reported_at_all():
    """A correctly protected endpoint passes, so triage never sees it."""
    signals = build(
        AUTH_PROBE_SPEC,
        {"method": "GET", "path": "/me", "auth": "none"},
        {"status": 401, "body_text": '{"detail":"Unauthorized"}', "duration_ms": 2},
    )
    assert signals.is_auth_probe is True


def test_server_error_with_trace_is_a_real_bug():
    signals = build(
        NEGATIVE_SPEC,
        {"method": "POST", "path": "/orders", "auth": "default"},
        {
            "status": 500,
            "body_text": 'Traceback (most recent call last):\n  File "app.py", line 9',
            "duration_ms": 20,
        },
    )
    verdict = classify(signals)
    assert verdict.failure_class is FailureClass.REAL_BUG
    assert verdict.confidence >= 0.9


def test_invalid_input_causing_500_is_a_real_bug():
    signals = build(
        NEGATIVE_SPEC,
        {"method": "GET", "path": "/products/abc", "auth": "default"},
        {"status": 500, "body_text": "Internal Server Error", "duration_ms": 12},
    )
    verdict = classify(signals)
    assert verdict.failure_class is FailureClass.REAL_BUG


def test_missing_credentials_is_environment_not_a_bug():
    """The regression that produced 'unknown' verdicts on protected endpoints."""
    signals = build(
        NEGATIVE_SPEC,
        {"method": "GET", "path": "/orders/xyz", "auth": "default"},
        {"status": 403, "body_text": '{"detail":"Not authenticated"}', "duration_ms": 2},
        auth_configured=False,
    )
    verdict = classify(signals)
    assert verdict.failure_class is FailureClass.ENVIRONMENT


def test_connection_failure_is_environment():
    signals = build(
        POSITIVE_SPEC,
        {"method": "GET", "path": "/products", "auth": "default"},
        {"error": "ConnectError", "duration_ms": 0},
    )
    verdict = classify(signals)
    assert verdict.failure_class is FailureClass.ENVIRONMENT


def test_timeout_is_network():
    signals = build(
        POSITIVE_SPEC,
        {"method": "GET", "path": "/products", "auth": "default"},
        {"error": "timeout", "duration_ms": 30000},
    )
    assert classify(signals).failure_class is FailureClass.NETWORK


def test_dependency_outage_is_not_blamed_on_the_handler():
    signals = build(
        POSITIVE_SPEC,
        {"method": "GET", "path": "/products", "auth": "default"},
        {"status": 503, "body_text": "psycopg.OperationalError: connection refused", "duration_ms": 8},
    )
    assert classify(signals).failure_class is FailureClass.DEPENDENCY


def test_history_flip_is_flakiness():
    signals = build(
        POSITIVE_SPEC,
        {"method": "GET", "path": "/products", "auth": "default"},
        {"status": 404, "body_text": "", "duration_ms": 5},
        recent_history=["passed", "failed", "passed", "failed"],
    )
    assert classify(signals).failure_class is FailureClass.FLAKY_TEST


def test_wrong_success_code_is_an_over_specified_test():
    signals = build(
        POSITIVE_SPEC,
        {"method": "POST", "path": "/orders", "auth": "default"},
        {"status": 202, "body_text": "{}", "duration_ms": 5},
    )
    assert classify(signals).failure_class is FailureClass.BAD_ASSERTION


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_never_classified_as_test_problems(status):
    signals = build(
        NEGATIVE_SPEC,
        {"method": "POST", "path": "/users", "auth": "default"},
        {"status": status, "body_text": "Internal Server Error", "duration_ms": 5},
    )
    verdict = classify(signals)
    assert verdict.failure_class in {FailureClass.REAL_BUG, FailureClass.DEPENDENCY}
