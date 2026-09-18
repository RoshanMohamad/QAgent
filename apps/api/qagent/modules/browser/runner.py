"""Browser E2E checks.

API tests came first (ADR-0002) because they're deterministic: no selectors, no
flake, a `500` is unambiguously a defect. This module stays deliberately narrow
for the same reason — it does not click anything or generate interaction
sequences (that's the Explorer Agent, later, once there is a state graph to
explore). It navigates to a fixed list of URLs and checks for the one class of
failure that genuinely needs a browser to observe at all: the page never loads,
it responds with a server error, or it throws in the browser. That's still
unambiguous evidence of a defect, so it doesn't reintroduce the selector-flake
ambiguity ADR-0002 rejected — there is no selector here to break.

Playwright is imported lazily inside ``run_browser_checks`` so installing
QAgent's core dependencies never pulls in a browser download; the optional
``e2e`` extra plus ``playwright install chromium`` opts in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class PageCheckResult:
    url: str
    status: str  # "passed" | "failed" | "error"
    http_status: int | None = None
    load_time_ms: int = 0
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failure_message: str | None = None
    # In-memory only - this module has no database import (see module docstring)
    # and never writes to disk itself. A failing check's screenshot is evidence
    # for a bug report, so it's captured here and handed up the call chain;
    # `pipeline.py` decides whether the check became a bug, and `persistence.py`
    # is the only place that ever touches storage (CLAUDE.md section 15).
    screenshot_png: bytes | None = None


@dataclass
class BrowserRunResult:
    base_url: str
    started_at: datetime
    finished_at: datetime | None = None
    checks: list[PageCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "passed")

    @property
    def failed(self) -> list[PageCheckResult]:
        return [c for c in self.checks if c.status != "passed"]

    def summary(self) -> dict:
        return {
            "base_url": self.base_url,
            "total": len(self.checks),
            "passed": self.passed,
            "failed": len(self.failed),
            "duration_s": round(
                ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds(), 2
            ),
        }


class _Page(Protocol):
    def on(self, event: str, handler: Any) -> None: ...
    def goto(self, url: str, timeout: float, wait_until: str) -> Any: ...


def _capture_screenshot(page: Any, url: str) -> bytes | None:
    """Best-effort evidence, never a reason to fail the check itself.

    A page bad enough to be worth a screenshot is also a page that can crash
    the screenshot call - `page.screenshot()` can itself time out against a
    hung renderer. That failure is not new information (the check already
    knows the page is broken), so it's logged and swallowed rather than
    turning "the app is broken" into "the check errored."
    """
    try:
        return page.screenshot(type="png", timeout=5000)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("screenshot capture failed for %s: %s", url, exc)
        return None


def _check_page(page: _Page, url: str, timeout_ms: int) -> PageCheckResult:
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: Any) -> None:
        if msg.type == "error":
            console_errors.append(msg.text)

    def _on_page_error(exc: Any) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_page_error)

    started = datetime.now(UTC)
    try:
        response = page.goto(url, timeout=timeout_ms, wait_until="load")
    except Exception as exc:  # noqa: BLE001 - a navigation failure is the signal itself
        return PageCheckResult(
            url=url,
            status="error",
            failure_message=f"navigation failed: {exc}",
            load_time_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        )

    load_time_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    http_status = response.status if response is not None else None

    failures = []
    if http_status is not None and http_status >= 500:
        failures.append(f"page responded {http_status}")
    if page_errors:
        failures.append(f"{len(page_errors)} uncaught JS exception(s)")
    if console_errors:
        failures.append(f"{len(console_errors)} console error(s)")

    screenshot_png = _capture_screenshot(page, url) if failures else None

    return PageCheckResult(
        url=url,
        status="passed" if not failures else "failed",
        http_status=http_status,
        load_time_ms=load_time_ms,
        console_errors=console_errors,
        screenshot_png=screenshot_png,
        page_errors=page_errors,
        failure_message="; ".join(failures) or None,
    )


def run_browser_checks(
    *,
    base_url: str,
    routes: list[str] | None = None,
    timeout_seconds: float = 15.0,
    headless: bool = True,
) -> BrowserRunResult:
    """Navigate to ``base_url`` plus each of ``routes`` and check each page loads clean.

    Requires the optional ``e2e`` extra (Playwright) and its browser binaries
    (``playwright install chromium``) — neither is a dependency of core QAgent.
    """
    from playwright.sync_api import sync_playwright

    result = BrowserRunResult(base_url=base_url, started_at=datetime.now(UTC))
    urls = [base_url] + [base_url.rstrip("/") + route for route in (routes or [])]
    timeout_ms = int(timeout_seconds * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_context().new_page()
            for url in urls:
                logger.info("checking %s", url)
                result.checks.append(_check_page(page, url, timeout_ms))
        finally:
            browser.close()

    result.finished_at = datetime.now(UTC)
    return result
