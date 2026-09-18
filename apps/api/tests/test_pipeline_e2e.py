"""The E2E stage folded into run_pipeline: crawl + page-check outcomes joining the
same outcomes list as API results, so they share one persistence path.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any

import pytest

from qagent.modules.browser.runner import PageCheckResult
from qagent.modules.explorer.actions import Action, ActionOutcome, ActionType, ElementRef
from qagent.modules.llm.client import LlmClient
from qagent.pipeline import (
    PipelineResult,
    _action_outcome_to_case_outcome,
    _page_check_to_outcome,
    _run_e2e_stage,
    _run_interactive_stage,
)


def _null_llm() -> LlmClient:
    return LlmClient.from_settings()


def test_passed_page_check_becomes_a_clean_outcome() -> None:
    check = PageCheckResult(url="http://x/", status="passed", http_status=200, load_time_ms=42)
    outcome = _page_check_to_outcome(check, _null_llm())

    assert outcome.kind == "e2e"
    assert outcome.status == "passed"
    assert outcome.verdict is None
    assert outcome.bug is None


def test_server_error_page_check_becomes_a_bug() -> None:
    check = PageCheckResult(
        url="http://x/checkout",
        status="failed",
        http_status=500,
        failure_message="page responded 500",
    )
    outcome = _page_check_to_outcome(check, _null_llm())

    assert outcome.verdict["failure_class"] == "real_bug"
    assert outcome.bug is not None
    assert outcome.bug["severity"] in {"high", "critical"}


def test_navigation_error_page_check_is_not_a_bug() -> None:
    check = PageCheckResult(
        url="http://x/gone", status="error", failure_message="navigation failed: timeout"
    )
    outcome = _page_check_to_outcome(check, _null_llm())

    assert outcome.verdict["failure_class"] == "environment"
    assert outcome.bug is None


def test_bug_page_check_attaches_screenshot_and_log_artifacts() -> None:
    check = PageCheckResult(
        url="http://x/checkout",
        status="failed",
        http_status=500,
        failure_message="page responded 500",
        console_errors=["Uncaught TypeError: x is not a function"],
        screenshot_png=b"fake-png-bytes",
    )
    outcome = _page_check_to_outcome(check, _null_llm())

    assert outcome.bug is not None
    kinds = {a.kind for a in outcome.artifacts}
    assert kinds == {"screenshot", "log"}
    screenshot = next(a for a in outcome.artifacts if a.kind == "screenshot")
    assert screenshot.data == b"fake-png-bytes"
    assert screenshot.content_type == "image/png"
    log = next(a for a in outcome.artifacts if a.kind == "log")
    assert b"Uncaught TypeError" in log.data


def test_bug_page_check_without_screenshot_has_no_screenshot_artifact() -> None:
    """Screenshot capture is best-effort (browser/runner.py) - a bug with no
    captured screenshot must still get its log artifact, not neither."""
    check = PageCheckResult(
        url="http://x/checkout",
        status="failed",
        http_status=500,
        failure_message="page responded 500",
        console_errors=["boom"],
        screenshot_png=None,
    )
    outcome = _page_check_to_outcome(check, _null_llm())

    kinds = {a.kind for a in outcome.artifacts}
    assert kinds == {"log"}


def test_passed_page_check_has_no_artifacts() -> None:
    check = PageCheckResult(url="http://x/", status="passed", http_status=200)
    outcome = _page_check_to_outcome(check, _null_llm())

    assert outcome.artifacts == []


def _block_playwright_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_e2e_stage_records_a_skip_when_playwright_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_playwright_import(monkeypatch)
    result = PipelineResult(base_url="http://x", started_at=datetime.now(UTC))

    _run_e2e_stage(result, base_url="http://x", max_pages=5, timeout_seconds=5.0, llm=_null_llm())

    assert result.outcomes == []
    assert any("playwright is not installed" in e for e in result.errors)


def _ref() -> ElementRef:
    return ElementRef(
        selector='button[data-testid="add-btn"]', tag="button", role=None, input_type=None,
        text="Add to cart", testid="add-btn",
    )


def test_interactive_stage_records_a_skip_when_playwright_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_playwright_import(monkeypatch)
    result = PipelineResult(base_url="http://x", started_at=datetime.now(UTC))

    _run_interactive_stage(
        result,
        base_url="http://x",
        max_pages=5,
        max_total_actions=10,
        policy=None,
        timeout_seconds=5.0,
        llm=_null_llm(),
    )

    assert result.outcomes == []
    assert any("playwright is not installed" in e for e in result.errors)


def test_clean_action_outcome_becomes_a_passed_case() -> None:
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()), ok=True, resulting_url="http://x/next"
    )

    case_outcome = _action_outcome_to_case_outcome(outcome, _null_llm())

    assert case_outcome.kind == "e2e_interactive"
    assert case_outcome.status == "passed"
    assert case_outcome.verdict is None
    assert case_outcome.bug is None


def test_action_outcome_with_page_error_becomes_a_bug() -> None:
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()),
        ok=True,
        resulting_url="http://x/next",
        page_errors=["TypeError: cannot read property 'price' of undefined"],
    )

    case_outcome = _action_outcome_to_case_outcome(outcome, _null_llm())

    assert case_outcome.kind == "e2e_interactive"
    assert case_outcome.verdict["failure_class"] == "real_bug"
    assert case_outcome.bug is not None


def test_failed_action_outcome_is_not_a_bug() -> None:
    """A selector that couldn't be found/clicked is evidence about the
    heuristic, not the application (ADR-0002's reasoning against
    selector-based flakiness) - it must never become a defect report."""
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()),
        ok=False,
        resulting_url=None,
        error="element not found",
    )

    case_outcome = _action_outcome_to_case_outcome(outcome, _null_llm())

    assert case_outcome.verdict is None
    assert case_outcome.bug is None
