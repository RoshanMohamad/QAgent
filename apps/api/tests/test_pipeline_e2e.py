"""The E2E stage folded into run_pipeline: crawl + page-check outcomes joining the
same outcomes list as API results, so they share one persistence path.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any

import pytest

from qagent.modules.browser.runner import PageCheckResult
from qagent.modules.llm.client import LlmClient
from qagent.pipeline import PipelineResult, _page_check_to_outcome, _run_e2e_stage


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
