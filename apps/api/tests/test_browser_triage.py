"""Mapping a page check to a triage verdict (qagent.modules.browser.triage)."""

from __future__ import annotations

from qagent.modules.browser.runner import PageCheckResult
from qagent.modules.browser.triage import classify_page_check
from qagent.modules.triage.classifier import FailureClass


def test_passed_check_has_no_verdict() -> None:
    check = PageCheckResult(url="http://x/", status="passed", http_status=200)
    assert classify_page_check(check) is None


def test_navigation_error_is_environment_not_a_bug() -> None:
    check = PageCheckResult(
        url="http://x/", status="error", failure_message="navigation failed: timeout"
    )
    verdict = classify_page_check(check)
    assert verdict is not None
    assert verdict.failure_class is FailureClass.ENVIRONMENT


def test_server_error_page_is_a_real_bug() -> None:
    check = PageCheckResult(
        url="http://x/",
        status="failed",
        http_status=500,
        failure_message="page responded 500",
    )
    verdict = classify_page_check(check)
    assert verdict is not None
    assert verdict.failure_class is FailureClass.REAL_BUG
    assert "page responded 500" in verdict.evidence


def test_uncaught_exception_is_a_real_bug_with_evidence() -> None:
    check = PageCheckResult(
        url="http://x/",
        status="failed",
        http_status=200,
        page_errors=["ReferenceError: foo is not defined"],
        failure_message="1 uncaught JS exception(s)",
    )
    verdict = classify_page_check(check)
    assert verdict is not None
    assert verdict.failure_class is FailureClass.REAL_BUG
    assert "ReferenceError: foo is not defined" in verdict.evidence
