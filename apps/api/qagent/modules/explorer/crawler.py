"""Explorer agent, MVP scope.

CLAUDE.md §8-9 describes an agent that opens the app, identifies interactive
elements, chooses an action, observes the result, and updates a state graph —
repeated until it's covered the app. Filling forms and choosing which button to
click meaningfully is the hard, open part of that loop and isn't attempted here.

What this module does: a same-origin BFS crawl over `<a href>` links, building a
state graph of reachable pages. That's a real subset of the full vision — it's
exactly what turns "the operator lists every route by hand" into "QAgent finds
the routes nobody told it about" — and it composes directly with
``modules/browser/runner.py``: crawl first, then check every discovered page for
the JS errors and 5xxs that check already knows how to find.

Playwright is imported lazily, matching ``modules/browser/runner.py`` — the
optional ``e2e`` extra opts in, core QAgent installs stay browser-free.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger(__name__)

#: URI schemes that are links in the DOM sense but never navigable pages.
_SKIP_SCHEMES = {"mailto", "tel", "javascript"}


@dataclass
class StateNode:
    url: str
    depth: int
    title: str | None = None
    link_count: int = 0
    #: Set only by the interactive explorer (modules/explorer/interact.py); a
    #: plain link-crawl node never populates these. Kept here rather than on a
    #: subclass field so ``StateGraph``'s generic node handling doesn't need to
    #: know which explorer produced a node.
    dom_fingerprint: str | None = None
    actions_available: int = 0


@dataclass
class StateGraph:
    root: str
    nodes: dict[str, StateNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)

    def add_node(self, node: StateNode) -> None:
        self.nodes.setdefault(node.url, node)

    def add_edge(self, source: str, target: str) -> None:
        self.edges.append((source, target))

    @property
    def routes(self) -> list[str]:
        """Discovered pages as paths relative to the root, e.g. for feeding into
        ``run_browser_checks(routes=...)``."""
        out = []
        for url in self.nodes:
            if url == self.root:
                continue
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            out.append(path)
        return out

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "nodes": [
                {"url": n.url, "depth": n.depth, "title": n.title, "link_count": n.link_count}
                for n in self.nodes.values()
            ],
            "edges": [{"from": s, "to": t} for s, t in self.edges],
        }


def _normalize(url: str) -> str:
    """Drop the fragment; keep the query string, since distinct queries are
    routinely distinct pages in practice (pagination, filters, resource ids)."""
    return urlunparse(urlparse(url)._replace(fragment=""))


def _same_origin(url: str, root: str) -> bool:
    a, b = urlparse(url), urlparse(root)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


class _Page(Protocol):
    def goto(self, url: str, timeout: float, wait_until: str) -> Any: ...
    def title(self) -> str: ...
    def eval_on_selector_all(self, selector: str, script: str) -> list[str]: ...


def _extract_links(page: _Page, current_url: str) -> list[str]:
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    links = []
    for href in hrefs:
        if not href or href.startswith("#"):
            continue
        if urlparse(href).scheme in _SKIP_SCHEMES:
            continue
        links.append(_normalize(urljoin(current_url, href)))
    return links


def _crawl(
    page: _Page, *, root: str, max_pages: int, max_depth: int, timeout_ms: int
) -> StateGraph:
    graph = StateGraph(root=root)
    visited: set[str] = set()
    queued: set[str] = {root}
    queue: deque[tuple[str, int]] = deque([(root, 0)])

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        try:
            page.goto(url, timeout=timeout_ms, wait_until="load")
        except Exception as exc:  # noqa: BLE001 - an unreachable page is a dead-end node, not a crash
            logger.debug("could not load %s: %s", url, exc)
            graph.add_node(StateNode(url=url, depth=depth))
            continue

        try:
            title = page.title()
        except Exception:  # noqa: BLE001 - title is cosmetic; never worth aborting the crawl
            title = None

        links = _extract_links(page, url) if depth < max_depth else []
        graph.add_node(StateNode(url=url, depth=depth, title=title, link_count=len(links)))

        for link in links:
            if not _same_origin(link, root):
                continue
            graph.add_edge(url, link)
            if link not in queued and len(queued) < max_pages:
                queued.add(link)
                queue.append((link, depth + 1))

    return graph


def explore(
    *,
    base_url: str,
    max_pages: int = 25,
    max_depth: int = 3,
    timeout_seconds: float = 15.0,
    headless: bool = True,
) -> StateGraph:
    """Crawl same-origin links from ``base_url`` and return the discovered state graph.

    Requires the optional ``e2e`` extra (Playwright) and its browser binaries
    (``playwright install chromium``).
    """
    from playwright.sync_api import sync_playwright

    root = _normalize(base_url)
    timeout_ms = int(timeout_seconds * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_context().new_page()
            return _crawl(
                page, root=root, max_pages=max_pages, max_depth=max_depth, timeout_ms=timeout_ms
            )
        finally:
            browser.close()
