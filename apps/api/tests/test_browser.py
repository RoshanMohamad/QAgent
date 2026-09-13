"""Browser E2E checks: page-load logic tested against fake Playwright-shaped
objects, so these tests need neither the ``playwright`` package nor a browser
binary installed.
"""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any

import pytest

from qagent.modules.browser.runner import PageCheckResult, _check_page, run_browser_checks


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    """Enough of Playwright's Page API for ``_check_page`` to exercise: ``on()``
    registers handlers, ``goto()`` can fire them before returning."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self._handlers: dict[str, Any] = {}
        self.fired_console: list[Any] = []
        self.fired_page_errors: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    def fire_console_error(self, text: str) -> None:
        self._handlers["console"](types.SimpleNamespace(type="error", text=text))

    def fire_console_log(self, text: str) -> None:
        self._handlers["console"](types.SimpleNamespace(type="log", text=text))

    def fire_page_error(self, message: str) -> None:
        self._handlers["pageerror"](message)

    def goto(self, url: str, timeout: float, wait_until: str):
        if self._raises:
            raise self._raises
        return self._response


def test_clean_page_passes() -> None:
    page = FakePage(response=FakeResponse(200))
    result = _check_page(page, "http://example.test/", timeout_ms=5000)
    assert result.status == "passed"
    assert result.http_status == 200
    assert result.failure_message is None


def test_server_error_status_fails() -> None:
    page = FakePage(response=FakeResponse(500))
    result = _check_page(page, "http://example.test/", timeout_ms=5000)
    assert result.status == "failed"
    assert "500" in result.failure_message


def test_console_error_fails_but_console_log_does_not() -> None:
    page = FakePage(response=FakeResponse(200))
    page.on("console", lambda msg: None)  # placeholder to be overwritten by _check_page

    original_goto = page.goto

    def goto_and_fire(url, timeout, wait_until):
        page.fire_console_log("just a log line")
        page.fire_console_error("Uncaught TypeError: x is not a function")
        return original_goto(url, timeout=timeout, wait_until=wait_until)

    page.goto = goto_and_fire
    result = _check_page(page, "http://example.test/", timeout_ms=5000)
    assert result.status == "failed"
    assert result.console_errors == ["Uncaught TypeError: x is not a function"]
    assert "1 console error" in result.failure_message


def test_page_error_fails() -> None:
    page = FakePage(response=FakeResponse(200))
    original_goto = page.goto

    def goto_and_fire(url, timeout, wait_until):
        page.fire_page_error("ReferenceError: foo is not defined")
        return original_goto(url, timeout=timeout, wait_until=wait_until)

    page.goto = goto_and_fire
    result = _check_page(page, "http://example.test/", timeout_ms=5000)
    assert result.status == "failed"
    assert result.page_errors == ["ReferenceError: foo is not defined"]


def test_navigation_exception_is_an_error_not_a_failure() -> None:
    page = FakePage(raises=TimeoutError("navigation timed out"))
    result = _check_page(page, "http://example.test/", timeout_ms=5000)
    assert result.status == "error"
    assert "navigation failed" in result.failure_message


def test_result_dataclass_defaults() -> None:
    result = PageCheckResult(url="http://x", status="passed")
    assert result.console_errors == []
    assert result.page_errors == []


class _FakeBrowser:
    def __init__(self, page: FakePage):
        self._page = page
        self.closed = False

    def new_context(self):
        return types.SimpleNamespace(new_page=lambda: self._page)

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser
        self.launch_kwargs: dict[str, Any] = {}

    def launch(self, headless: bool):
        self.launch_kwargs["headless"] = headless
        return self._browser


class _FakePlaywrightContext:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, page: FakePage) -> _FakeBrowser:
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser)
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightContext(chromium)  # type: ignore[attr-defined]
    playwright_pkg = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return browser


def _block_playwright_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import playwright...`` raise ModuleNotFoundError regardless of
    whether the package is actually installed in this environment."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_run_browser_checks_visits_base_url_and_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage(response=FakeResponse(200))
    browser = _install_fake_playwright(monkeypatch, page)

    result = run_browser_checks(
        base_url="http://example.test", routes=["/login", "/dashboard"], timeout_seconds=5.0
    )

    assert [c.url for c in result.checks] == [
        "http://example.test",
        "http://example.test/login",
        "http://example.test/dashboard",
    ]
    assert result.passed == 3
    assert browser.closed is True


def test_run_browser_checks_closes_browser_even_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage(response=FakeResponse(503))
    browser = _install_fake_playwright(monkeypatch, page)

    result = run_browser_checks(base_url="http://example.test")

    assert result.failed
    assert browser.closed is True


def test_run_browser_checks_missing_playwright_raises_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_playwright_import(monkeypatch)
    with pytest.raises(ModuleNotFoundError):
        run_browser_checks(base_url="http://example.test")
