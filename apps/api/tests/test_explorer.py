"""Explorer agent: BFS link-crawl logic tested against fake Playwright-shaped
pages, so no real browser or ``playwright`` package is required.
"""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any

import pytest

from qagent.modules.explorer.crawler import StateGraph, StateNode, _crawl, explore


class FakePage:
    """A tiny site graph, keyed by URL, of {links: [...], title: str, raises: exc|None}."""

    def __init__(self, site: dict[str, dict[str, Any]]):
        self._site = site
        self._current: str | None = None

    def goto(self, url: str, timeout: float, wait_until: str) -> None:
        page = self._site.get(url)
        if page and page.get("raises"):
            raise page["raises"]
        self._current = url

    def title(self) -> str:
        return self._site.get(self._current, {}).get("title", "")

    def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
        return self._site.get(self._current, {}).get("links", [])


def test_crawl_discovers_same_origin_pages_by_bfs() -> None:
    site = {
        "http://example.test/": {"title": "Home", "links": ["/about", "/products"]},
        "http://example.test/about": {"title": "About", "links": ["/"]},
        "http://example.test/products": {"title": "Products", "links": ["/products/1"]},
        "http://example.test/products/1": {"title": "Product 1", "links": []},
    }
    page = FakePage(site)

    graph = _crawl(page, root="http://example.test/", max_pages=25, max_depth=5, timeout_ms=5000)

    assert set(graph.nodes) == set(site)
    assert ("http://example.test/", "http://example.test/about") in graph.edges


def test_crawl_ignores_offsite_and_non_navigable_links() -> None:
    site = {
        "http://example.test/": {
            "title": "Home",
            "links": [
                "https://other.test/evil",
                "mailto:a@b.com",
                "javascript:void(0)",
                "#section",
                "/contact",
            ],
        },
        "http://example.test/contact": {"title": "Contact", "links": []},
    }
    page = FakePage(site)

    graph = _crawl(page, root="http://example.test/", max_pages=25, max_depth=5, timeout_ms=5000)

    assert set(graph.nodes) == {"http://example.test/", "http://example.test/contact"}


def test_crawl_respects_max_pages() -> None:
    site = {
        "http://example.test/": {"title": "Home", "links": ["/a", "/b", "/c"]},
        "http://example.test/a": {"title": "A", "links": []},
        "http://example.test/b": {"title": "B", "links": []},
        "http://example.test/c": {"title": "C", "links": []},
    }
    page = FakePage(site)

    graph = _crawl(page, root="http://example.test/", max_pages=2, max_depth=5, timeout_ms=5000)

    assert len(graph.nodes) <= 2


def test_crawl_respects_max_depth() -> None:
    site = {
        "http://example.test/": {"title": "Home", "links": ["/level1"]},
        "http://example.test/level1": {"title": "L1", "links": ["/level2"]},
        "http://example.test/level2": {"title": "L2", "links": []},
    }
    page = FakePage(site)

    # depth 0 is root; max_depth=0 means don't even look at root's links
    graph = _crawl(page, root="http://example.test/", max_pages=25, max_depth=0, timeout_ms=5000)

    assert set(graph.nodes) == {"http://example.test/"}


def test_crawl_records_unreachable_page_as_dead_end_node() -> None:
    site = {
        "http://example.test/": {"title": "Home", "links": ["/broken"]},
        "http://example.test/broken": {"raises": ConnectionError("refused")},
    }
    page = FakePage(site)

    graph = _crawl(page, root="http://example.test/", max_pages=25, max_depth=5, timeout_ms=5000)

    node = graph.nodes["http://example.test/broken"]
    assert node.title is None
    assert node.link_count == 0


def test_graph_routes_excludes_root_and_is_path_only() -> None:
    graph = StateGraph(root="http://example.test/")
    graph.add_node(StateNode(url="http://example.test/", depth=0))
    graph.add_node(StateNode(url="http://example.test/products?page=2", depth=1))

    assert graph.routes == ["/products?page=2"]


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

    def launch(self, headless: bool):
        return self._browser


class _FakePlaywrightContext:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_explore_end_to_end_via_fake_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    site = {
        "http://example.test/": {"title": "Home", "links": ["/about"]},
        "http://example.test/about": {"title": "About", "links": []},
    }
    page = FakePage(site)
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser)

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightContext(chromium)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    graph = explore(base_url="http://example.test/")

    assert set(graph.nodes) == set(site)
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


def test_explore_missing_playwright_raises_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_playwright_import(monkeypatch)
    with pytest.raises(ModuleNotFoundError):
        explore(base_url="http://example.test/")
