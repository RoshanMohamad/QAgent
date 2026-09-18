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


def _button(testid: str, text: str, form_index: int | None = None) -> dict:
    return {
        "tag": "button",
        "id": None,
        "classes": [],
        "attrs": {"data-testid": testid, "type": "submit"} if form_index is not None else {
            "data-testid": testid
        },
        "text": text,
        "required": False,
        "disabled": False,
        "visible": True,
        "in_form": form_index is not None,
        "form_index": form_index,
        "label": None,
        "options": [],
    }


def _input(name: str, form_index: int, testid: str | None = None) -> dict:
    attrs = {"type": "text", "name": name}
    if testid:
        attrs["data-testid"] = testid
    return {
        "tag": "input",
        "id": None,
        "classes": [],
        "attrs": attrs,
        "text": None,
        "required": True,
        "disabled": False,
        "visible": True,
        "in_form": True,
        "form_index": form_index,
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


def test_max_actions_per_page_caps_actions_on_one_url() -> None:
    """`max_actions_per_page` bounds the *total* actions spent on one URL
    across every branch attempt, not just the size of a single ranking call."""
    states = {
        "start": {
            "url": ROOT,
            "elements": [_button("a-btn", "A"), _button("b-btn", "B"), _button("c-btn", "C")],
        },
    }
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(max_actions_per_page=2),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    total_actions_recorded = sum(len(n.actions_taken) for n in graph.nodes.values())
    assert total_actions_recorded == 2


def test_two_independent_click_candidates_are_both_eventually_tried() -> None:
    """The fix for the "only one branch per page" gap: a page offering two
    independent actions must not abandon the second the moment the first one
    is chosen and explored - both get their own reload-and-try attempt."""
    states = {
        "start": {
            "url": ROOT,
            "elements": [_button("a-btn", "A"), _button("b-btn", "B")],
        },
        "dead_a": {"url": ROOT, "elements": []},
        "dead_b": {"url": ROOT, "elements": []},
    }
    states["start"]["transitions"] = {
        'button[data-testid="a-btn"]': "dead_a",
        'button[data-testid="b-btn"]': "dead_b",
    }
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

    start_node = next(n for n in graph.nodes.values() if n.actions_available == 2 or n.actions_taken)
    tried_testids = {o.action.target.testid for o in start_node.actions_taken}
    assert tried_testids == {"a-btn", "b-btn"}
    # dead_a and dead_b are structurally identical (both just "no elements"),
    # so they correctly collapse onto the same fingerprint - the point of this
    # test is that *both* buttons were tried, not that every destination is
    # distinguishable from a genuinely different one.
    assert len(graph.nodes) == 2


def test_required_field_gating_is_scoped_per_form() -> None:
    """A required field belonging to one form must never block a *different*
    form's submit button on the same page (ADR-0005)."""
    f0_field = _input("f0field", form_index=0)
    f1_field = _input("f1field", form_index=1, testid="f1field-input")
    submit0 = _button("submit0", "Save", form_index=0)
    submit1 = _button("submit1", "Login", form_index=1)
    states = {
        "start": {"url": ROOT, "elements": [f1_field, submit1, f0_field, submit0]},
        "done1": {"url": ROOT, "elements": []},
    }
    f1_selector = 'input[data-testid="f1field-input"]'
    submit1_selector = 'button[data-testid="submit1"]'
    states["start"]["transitions"] = {
        f1_selector: "start",  # filling stays on the same (pristine) state
        submit1_selector: "done1",
    }
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    graph = _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(max_actions_per_page=10),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    # Form 1's submit fired and reached its own state, even though form 0's
    # required field (f0field) was never filled.
    assert any(n.dom_fingerprint and n.url == ROOT and n.actions_available == 0 for n in graph.nodes.values())
    all_outcomes = [o for n in graph.nodes.values() for o in n.actions_taken]
    submit1_outcomes = [o for o in all_outcomes if o.action.target.selector == submit1_selector]
    assert submit1_outcomes and submit1_outcomes[0].ok is True
    assert submit1_outcomes[0].resulting_url == ROOT


def test_dialog_handler_is_registered_and_dismisses() -> None:
    states = {
        "start": {"url": ROOT, "elements": [_button("alert-btn", "Trigger")]},
        "after": {"url": ROOT, "elements": []},
    }
    states["start"]["transitions"] = {'button[data-testid="alert-btn"]': "after"}
    page = FakeInteractivePage(states, entry_by_url={ROOT: "start"})

    _interact(
        page,
        root=ROOT,
        policy=InteractionPolicy(),
        llm=_null_llm(),
        max_pages=25,
        max_depth=3,
        max_total_actions=40,
        timeout_ms=5000,
    )

    assert "dialog" in page._handlers
    fake_dialog = types.SimpleNamespace(dismissed=False)
    fake_dialog.dismiss = lambda: setattr(fake_dialog, "dismissed", True)
    page._handlers["dialog"](fake_dialog)
    assert fake_dialog.dismissed is True


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
