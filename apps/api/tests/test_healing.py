"""Self-healing selectors: scoring and parsing logic is pure, so most of this
needs neither a browser nor the ``playwright`` package.
"""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any

import pytest

from qagent.modules.browser.healing import (
    ElementDescriptor,
    build_selector,
    extract_elements,
    find_replacements,
    parse_selector,
    propose_replacement,
    score_candidate,
)


def test_parse_selector_extracts_tag_id_classes_and_attrs() -> None:
    parsed = parse_selector('button#submit-btn.primary.large[data-testid="checkout"]')
    assert parsed.tag == "button"
    assert parsed.id == "submit-btn"
    assert set(parsed.classes) == {"primary", "large"}
    assert parsed.attrs == {"data-testid": "checkout"}


def test_parse_selector_handles_bare_tag() -> None:
    parsed = parse_selector("input")
    assert parsed.tag == "input"
    assert parsed.id is None
    assert parsed.classes == []
    assert parsed.attrs == {}


def test_score_candidate_matches_claude_md_example() -> None:
    # The CLAUDE.md §11 worked example: a data-testid attribute disappears, an
    # aria-label with the same semantic value replaces it.
    old = parse_selector('button[data-testid="checkout"]')
    candidate = ElementDescriptor(tag="button", attrs={"aria-label": "checkout"})

    score = score_candidate(old, candidate)
    assert score >= 0.7


def test_score_candidate_no_overlap_scores_low() -> None:
    old = parse_selector('button[data-testid="checkout"]')
    candidate = ElementDescriptor(tag="a", attrs={"aria-label": "logout"})

    assert score_candidate(old, candidate) < 0.3


def test_score_candidate_matching_id_is_strong_signal() -> None:
    old = parse_selector("#checkout-button")
    candidate = ElementDescriptor(tag="button", id="checkout-button")

    assert score_candidate(old, candidate) >= 0.8


def test_build_selector_prefers_id() -> None:
    element = ElementDescriptor(tag="button", id="submit", attrs={"aria-label": "Submit"})
    assert build_selector(element) == "#submit"


def test_build_selector_falls_back_to_semantic_attribute() -> None:
    element = ElementDescriptor(tag="button", attrs={"aria-label": "Checkout"})
    assert build_selector(element) == 'button[aria-label="Checkout"]'


def test_build_selector_falls_back_to_tag_and_class() -> None:
    element = ElementDescriptor(tag="div", classes=["card", "product"])
    assert build_selector(element) == "div.card.product"


def test_build_selector_last_resort_is_bare_tag() -> None:
    element = ElementDescriptor(tag="span")
    assert build_selector(element) == "span"


def test_propose_replacement_returns_best_match_above_threshold() -> None:
    elements = [
        ElementDescriptor(tag="a", attrs={"aria-label": "logout"}),
        ElementDescriptor(tag="button", attrs={"aria-label": "checkout"}),
    ]
    proposal = propose_replacement('button[data-testid="checkout"]', elements)
    assert proposal is not None
    assert proposal.new_selector == 'button[aria-label="checkout"]'
    assert proposal.requires_approval is True


def test_propose_replacement_returns_none_below_threshold() -> None:
    elements = [ElementDescriptor(tag="a", attrs={"aria-label": "logout"})]
    proposal = propose_replacement('button[data-testid="checkout"]', elements, threshold=0.7)
    assert proposal is None


def test_propose_replacement_empty_page_returns_none() -> None:
    assert propose_replacement('button[data-testid="checkout"]', []) is None


class _FakePage:
    def __init__(self, raw_elements: list[dict]):
        self._raw = raw_elements
        self.navigated_to: str | None = None

    def goto(self, url: str, timeout: float, wait_until: str) -> None:
        self.navigated_to = url

    def eval_on_selector_all(self, selector: str, script: str) -> list[dict]:
        return self._raw


def test_extract_elements_maps_raw_dicts_to_descriptors() -> None:
    page = _FakePage(
        [
            {
                "tag": "button",
                "id": None,
                "classes": ["btn", "btn-primary"],
                "attrs": {"aria-label": "Checkout"},
                "text": "Checkout",
            }
        ]
    )
    elements = extract_elements(page)
    assert len(elements) == 1
    assert elements[0].tag == "button"
    assert elements[0].attrs == {"aria-label": "Checkout"}
    assert elements[0].classes == ["btn", "btn-primary"]


class _FakeBrowser:
    def __init__(self, page: _FakePage):
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


def test_find_replacements_end_to_end_via_fake_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(
        [{"tag": "button", "id": None, "classes": [], "attrs": {"aria-label": "checkout"}, "text": None}]
    )
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser)

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightContext(chromium)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    proposals = find_replacements(
        base_url="http://example.test", old_selectors=['button[data-testid="checkout"]']
    )

    assert proposals['button[data-testid="checkout"]'].new_selector == 'button[aria-label="checkout"]'
    assert page.navigated_to == "http://example.test"
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


def test_find_replacements_missing_playwright_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_playwright_import(monkeypatch)
    with pytest.raises(ModuleNotFoundError):
        find_replacements(base_url="http://example.test", old_selectors=["#x"])
