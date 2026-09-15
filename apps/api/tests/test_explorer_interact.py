"""Interactive explorer loop: driven against a fake Playwright-shaped page
with distinct DOM "states" per URL, so no real browser or ``playwright``
package is required.
"""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any

import pytest

from qagent.config import get_settings
from qagent.modules.explorer.actions import InteractionPolicy
from qagent.modules.explorer.interact import (
    InteractionGraph,
    _interact,
    explore_interactive,
    fingerprint,
)
from qagent.modules.llm.budget import Budget
from qagent.modules.llm.client import LlmClient
from qagent.modules.llm.providers import NullProvider


def _null_llm() -> LlmClient:
    return LlmClient(settings=get_settings(), provider=NullProvider(), budget=Budget(10, 1000, 1.0))


def _button(testid: str, text: str) -> dict:
    return {
        "tag": "button",
        "id": None,
        "classes": [],
        "attrs": {"data-testid": testid},
        "text": text,
        "required": False,
        "disabled": False,
        "visible": True,
        "in_form": False,
        "label": None,
        "options": [],
    }


class FakeInteractivePage:
    """Models a site as named DOM *states*, not just URLs, so a click can move
    the "current page" to a different state at the same URL - exactly the case
    fingerprint-based dedup exists to handle.

    ``states``: state_key -> {url, title, links, elements, transitions, raises,
    fire_console_error, fire_page_error}. ``transitions`` maps a built selector
    string to the state_key reached by clicking/filling/selecting it.
    """

    def __init__(self, states: dict[str, dict], entry_by_url: dict[str, str]):
        self._states = states
        self._entry_by_url = entry_by_url
        self._current: str | None = None
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    def goto(self, url: str, timeout: float, wait_until: str) -> None:
        state_key = self._entry_by_url.get(url)
        if state_key is None:
            raise ConnectionError(f"no such url: {url}")
        state = self._states[state_key]
        if state.get("raises"):
            raise state["raises"]
        self._current = state_key

    def title(self) -> str:
        return self._states[self._current].get("title", "")

    @property
    def url(self) -> str:
        # A property on a real Playwright Page, not a method - matched here on
        # purpose so a `.url()` call-site bug fails the same way it would
        # against a real browser instead of being silently swallowed.
        return self._states[self._current]["url"]

    def eval_on_selector_all(self, selector: str, script: str) -> list[Any]:
        state = self._states[self._current]
        if selector == "a[href]":
            return state.get("links", [])
        return state.get("elements", [])

    def _apply_transition(self, selector: str) -> None:
        state = self._states[self._current]
        transitions = state.get("transitions", {})
        if selector not in transitions:
            raise RuntimeError(f"no element matches selector {selector!r}")
        self._current = transitions[selector]
        new_state = self._states[self._current]
        if new_state.get("fire_page_error") and "pageerror" in self._handlers:
            self._handlers["pageerror"](new_state["fire_page_error"])
        if new_state.get("fire_console_error") and "console" in self._handlers:
            self._handlers["console"](
                types.SimpleNamespace(type="error", text=new_state["fire_console_error"])
            )

    def click(self, selector: str, timeout: float) -> None:
        self._apply_transition(selector)

    def fill(self, selector: str, value: str, timeout: float) -> None:
        self._apply_transition(selector)

    def select_option(self, selector: str, value: str, timeout: float) -> None:
        self._apply_transition(selector)


ROOT = "http://example.test/"


def test_action_that_changes_dom_without_changing_url_creates_a_new_node() -> None:
    states = {
        "start": {"url": ROOT, "elements": [_button("next-btn", "Next")]},
        "step2": {
            "url": ROOT,
            "elements": [_button("finish-btn", "Finish")],
            "transitions": {},
        },
        "done": {"url": ROOT, "elements": []},
    }
    states["start"]["transitions"] = {'button[data-testid="next-btn"]': "step2"}
    states["step2"]["transitions"] = {'button[data-testid="finish-btn"]': "done"}
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    assert isinstance(graph, InteractionGraph)
    # Three distinct states at the same URL: one node each, not collapsed into one.
    assert len({n.dom_fingerprint for n in graph.nodes.values()}) == 3
    assert all(n.url == ROOT for n in graph.nodes.values())
    outcomes = [o for n in graph.nodes.values() for o in n.actions_taken]
    assert len(outcomes) == 2
    # Regression guard: page.url is a property on a real Playwright Page, not
    # a method - a `page.url()` call-site bug gets swallowed by a try/except
    # and silently reports every resulting_url as None instead of failing loudly.
    assert all(o.resulting_url == ROOT for o in outcomes)


def test_max_total_actions_caps_actions_taken() -> None:
    states = {
        "start": {"url": ROOT, "elements": [_button("next-btn", "Next")]},
        "step2": {"url": ROOT, "elements": [_button("finish-btn", "Finish")]},
        "done": {"url": ROOT, "elements": []},
    }
    states["start"]["transitions"] = {'button[data-testid="next-btn"]': "step2"}
    states["step2"]["transitions"] = {'button[data-testid="finish-btn"]': "done"}
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=1,
        timeout_ms=5000,
    )

    total_actions_recorded = sum(len(n.actions_taken) for n in graph.nodes.values())
    assert total_actions_recorded == 1


def test_dead_end_page_is_recorded_and_bfs_continues_via_links() -> None:
    about_url = ROOT + "about"
    states = {
        "start": {"url": ROOT, "elements": [], "links": [about_url]},
        "about": {"url": about_url, "elements": [], "links": []},
    }
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start", about_url: "about"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    urls = {n.url for n in graph.nodes.values()}
    assert urls == {ROOT, about_url}
    root_node = next(n for n in graph.nodes.values() if n.url == ROOT)
    assert root_node.actions_available == 0


def test_action_causing_console_error_is_recorded_on_the_outcome() -> None:
    states = {
        "start": {"url": ROOT, "elements": [_button("add-btn", "Add to cart")]},
        "broken": {
            "url": ROOT,
            "elements": [],
            "fire_console_error": "TypeError: cannot read property 'price' of undefined",
        },
    }
    states["start"]["transitions"] = {'button[data-testid="add-btn"]': "broken"}
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    outcomes = [o for n in graph.nodes.values() for o in n.actions_taken]
    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert outcomes[0].console_errors == ["TypeError: cannot read property 'price' of undefined"]


def test_failed_interaction_is_recorded_not_raised() -> None:
    states = {"start": {"url": ROOT, "elements": [_button("ghost-btn", "Ghost")]}}
    # No transition registered for the built selector -> click raises.
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    outcomes = [o for n in graph.nodes.values() for o in n.actions_taken]
    assert len(outcomes) == 1
    assert outcomes[0].ok is False
    assert outcomes[0].error is not None


def test_fingerprint_ignores_free_text_but_not_structure() -> None:
    from qagent.modules.explorer.elements import ActionableElement

    a = [ActionableElement(tag="button", attrs={"data-testid": "x"}, text="Click me")]
    b = [ActionableElement(tag="button", attrs={"data-testid": "x"}, text="Different label")]
    c = [ActionableElement(tag="button", attrs={"data-testid": "y"}, text="Click me")]

    assert fingerprint(ROOT, a) == fingerprint(ROOT, b)
    assert fingerprint(ROOT, a) != fingerprint(ROOT, c)


class _FakeBrowser:
    def __init__(self, page: FakeInteractivePage):
        self._page = page
        self.closed = False

    def new_context(self):
        return types.SimpleNamespace(new_page=lambda: self._page)

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser

    def launch(self, headless: bool):
        return self._browser


class _FakePlaywrightContext:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_explore_interactive_end_to_end_via_fake_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    states = {"start": {"url": ROOT, "elements": []}}
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser)

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightContext(chromium)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    graph = explore_interactive(base_url=ROOT, llm=_null_llm())

    assert isinstance(graph, InteractionGraph)
    assert browser.closed is True


def _block_playwright_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import playwright...`` raise ModuleNotFoundError regardless of
    whether the package is actually installed in this environment."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_explore_interactive_missing_playwright_raises_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_playwright_import(monkeypatch)
    with pytest.raises(ModuleNotFoundError):
        explore_interactive(base_url=ROOT, llm=_null_llm())
