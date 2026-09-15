"""Interactive explorer: form filling, clicking, and inferred state transitions.

Builds on ``modules/explorer/crawler.py``'s same-origin BFS (which stays
link-only — see that module's docstring) by also extracting and ranking
actionable elements per page (``elements.py``, ``actions.py``) and executing
the top-ranked one before continuing the crawl.

State identity is ``(path, dom_fingerprint)`` rather than bare URL, because an
action can change the page without changing the URL (a form submits into a
"thanks" state, a modal opens, a validation error appears). Treating that as
the same node would silently stop exploring it the moment an action changed
anything; treating every load as a new node would loop forever on a page that
resets on reload. The fingerprint is a structural signature of the page's
interactive elements — not their text — so two loads of the identical state
still hash identically. See ADR-0005.

Every click/fill/select is wrapped so a failed interaction becomes a recorded
``ActionOutcome``, never a crash — the same contract ``crawler._crawl`` already
has for an unreachable page.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from qagent.modules.explorer.actions import (
    Action,
    ActionOutcome,
    ActionType,
    InteractionPolicy,
    choose_action,
    enumerate_actions,
)
from qagent.modules.explorer.crawler import (
    StateGraph,
    StateNode,
    _extract_links,
    _normalize,
    _same_origin,
)
from qagent.modules.explorer.elements import ActionableElement, extract_actionable_elements
from qagent.modules.llm.client import LlmClient

logger = logging.getLogger(__name__)


def fingerprint(url: str, elements: list[ActionableElement]) -> str:
    """A stable hash of (normalized path, structural element signature).

    Deliberately excludes free text (labels, prices, timestamps): two loads of
    the same logical page state must hash identically, or dedup never fires.
    A different state at the *same* URL (a submitted form, an opened modal)
    hashes differently because the set/shape of interactive elements changed.
    """
    path = urlparse(url).path or "/"
    signature = sorted(
        ":".join(
            [
                el.tag,
                el.attrs.get("type", ""),
                el.attrs.get("data-testid") or el.attrs.get("aria-label") or el.attrs.get("name")
                or el.id or "",
                el.attrs.get("role", ""),
            ]
        )
        for el in elements
    )
    digest = hashlib.sha256("\n".join([path, *signature]).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass
class InteractionNode(StateNode):
    candidate_actions: list[Action] = field(default_factory=list)
    actions_taken: list[ActionOutcome] = field(default_factory=list)


def _state_key(node: StateNode) -> str:
    return f"{urlparse(node.url).path or '/'}#{node.dom_fingerprint or ''}"


@dataclass
class InteractionGraph(StateGraph):
    """Same shape as ``StateGraph``, keyed by ``(path, fingerprint)`` instead of
    bare URL so a same-URL state change is a distinct node. ``.routes`` is
    overridden to read ``node.url`` (the dict key is no longer a URL)."""

    def add_node(self, node: StateNode) -> None:
        self.nodes.setdefault(_state_key(node), node)

    @property
    def routes(self) -> list[str]:
        out: list[str] = []
        for node in self.nodes.values():
            if node.url == self.root:
                continue
            parsed = urlparse(node.url)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            if path not in out:
                out.append(path)
        return out

    def to_dict(self) -> dict:
        # Zipped by position, not by url: unlike the plain crawler's StateGraph,
        # several nodes here can share one url (distinct fingerprints), so a
        # url-keyed lookup would silently collapse them onto the wrong node.
        base = super().to_dict()
        for entry, node in zip(base["nodes"], self.nodes.values(), strict=True):
            if not isinstance(node, InteractionNode):
                continue
            entry["dom_fingerprint"] = node.dom_fingerprint
            entry["actions_available"] = node.actions_available
            entry["actions_taken"] = [
                {
                    "type": o.action.type.value,
                    "selector": o.action.target.selector,
                    "value": o.action.value,
                    "source": o.action.source,
                    "ok": o.ok,
                    "resulting_url": o.resulting_url,
                    "error": o.error,
                    "console_errors": o.console_errors,
                    "page_errors": o.page_errors,
                }
                for o in node.actions_taken
            ]
        return base


class _InteractivePage(Protocol):
    def goto(self, url: str, timeout: float, wait_until: str) -> Any: ...
    def title(self) -> str: ...
    #: A property on a real Playwright ``Page``, not a method - deliberately
    #: typed that way here too so a fake page that gets this wrong is caught
    #: by type-checking rather than silently swallowed by the try/except below.
    @property
    def url(self) -> str: ...
    def on(self, event: str, handler: Any) -> None: ...
    def eval_on_selector_all(self, selector: str, script: str) -> list[Any]: ...
    def click(self, selector: str, timeout: float) -> Any: ...
    def fill(self, selector: str, value: str, timeout: float) -> Any: ...
    def select_option(self, selector: str, value: str, timeout: float) -> Any: ...


def _execute_action(
    page: _InteractivePage,
    action: Action,
    *,
    timeout_ms: int,
    console_buffer: list[str],
    page_error_buffer: list[str],
) -> ActionOutcome:
    console_buffer.clear()
    page_error_buffer.clear()

    try:
        if action.type == ActionType.FILL:
            page.fill(action.target.selector, action.value or "", timeout=timeout_ms)
        elif action.type == ActionType.SELECT:
            page.select_option(action.target.selector, action.value or "", timeout=timeout_ms)
        else:  # CLICK or SUBMIT
            page.click(action.target.selector, timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - a failed interaction is a recorded outcome
        return ActionOutcome(
            action=action, ok=False, resulting_url=None, resulting_fingerprint=None, error=str(exc)
        )

    try:
        resulting_url = page.url
    except Exception:  # noqa: BLE001 - reading .url is cosmetic; never worth aborting on
        resulting_url = None

    return ActionOutcome(
        action=action,
        ok=True,
        resulting_url=resulting_url,
        resulting_fingerprint=None,
        console_errors=list(console_buffer),
        page_errors=list(page_error_buffer),
    )


def _interact(
    page: _InteractivePage,
    *,
    root: str,
    policy: InteractionPolicy,
    llm: LlmClient,
    max_pages: int,
    max_depth: int,
    max_total_actions: int,
    timeout_ms: int,
) -> InteractionGraph:
    graph = InteractionGraph(root=root)
    visited_keys: set[str] = set()
    queued_urls: set[str] = {root}
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    total_actions = 0

    while queue and len(visited_keys) < max_pages:
        url, depth = queue.popleft()

        try:
            page.goto(url, timeout=timeout_ms, wait_until="load")
        except Exception as exc:  # noqa: BLE001 - an unreachable page is a dead-end node
            logger.debug("could not load %s: %s", url, exc)
            node = InteractionNode(url=url, depth=depth)
            key = _state_key(node)
            if key not in visited_keys:
                visited_keys.add(key)
                graph.add_node(node)
            continue

        console_buffer: list[str] = []
        page_error_buffer: list[str] = []

        def _on_console(msg: Any, _buf: list[str] = console_buffer) -> None:
            if msg.type == "error":
                _buf.append(msg.text)

        def _on_page_error(exc: Any, _buf: list[str] = page_error_buffer) -> None:
            _buf.append(str(exc))

        page.on("console", _on_console)
        page.on("pageerror", _on_page_error)

        current_url = url
        # Selectors already attempted during this page visit. A structural
        # fingerprint deliberately can't tell "email filled in" apart from
        # "email empty" (see `fingerprint`'s docstring), so relying on the
        # fingerprint alone to decide whether there's anything left to try
        # would stop after the very first field of a multi-field form. This
        # tracks progress explicitly instead, and doubles as the guard against
        # looping forever on an action that turns out to be a no-op.
        attempted_selectors: set[str] = set()

        # Drill into this page: extract -> fingerprint -> act -> repeat on the
        # same live page (no reload) as long as the action stays on this URL
        # and produces progress (a state nobody has recorded, or an action
        # nobody has tried yet).
        while True:
            try:
                title = page.title()
            except Exception:  # noqa: BLE001 - title is cosmetic
                title = None

            elements = extract_actionable_elements(page)
            fp = fingerprint(current_url, elements)
            key = _state_key(InteractionNode(url=current_url, depth=depth, dom_fingerprint=fp))

            if key in visited_keys:
                # Same structural state as one already recorded - reuse that
                # node (filling a field doesn't change the fingerprint on
                # purpose, so this is the common case mid-form, not an error).
                node = graph.nodes[key]
            else:
                visited_keys.add(key)
                node = InteractionNode(
                    url=current_url, depth=depth, title=title, dom_fingerprint=fp
                )
                graph.add_node(node)

                links = _extract_links(page, current_url) if depth < max_depth else []
                node.link_count = len(links)
                for link in links:
                    if not _same_origin(link, root):
                        continue
                    graph.add_edge(key, link)
                    if link not in queued_urls and len(queued_urls) < max_pages:
                        queued_urls.add(link)
                        queue.append((link, depth + 1))

            can_act = depth < max_depth and total_actions < max_total_actions
            candidates = enumerate_actions(elements, policy) if can_act else []
            candidates = [a for a in candidates if a.target.selector not in attempted_selectors]

            # Don't submit a form ahead of its own required fields: a SUBMIT
            # ranked highest by testid/CTA-text alone would otherwise fire
            # before anything is filled in, and real browsers routinely block
            # that submission via native constraint validation anyway -
            # leaving nothing for the (fingerprint-based) dedup above to tell
            # apart from the untouched page, ending the drill right there.
            has_pending_required_fill = any(
                a.required and a.type in {ActionType.FILL, ActionType.SELECT} for a in candidates
            )
            if has_pending_required_fill:
                candidates = [a for a in candidates if a.type is not ActionType.SUBMIT]

            node.candidate_actions = candidates
            node.actions_available = len(candidates)

            if not candidates:
                break

            chosen = choose_action(
                candidates, page_context={"url": current_url, "title": title or ""}, llm=llm
            )
            if chosen is None:
                break

            outcome = _execute_action(
                page,
                chosen,
                timeout_ms=timeout_ms,
                console_buffer=console_buffer,
                page_error_buffer=page_error_buffer,
            )
            node.actions_taken.append(outcome)
            attempted_selectors.add(chosen.target.selector)
            total_actions += 1

            if not outcome.ok or not outcome.resulting_url:
                break

            if not _same_origin(outcome.resulting_url, root):
                break  # navigated off-site; nothing more to explore from here

            if outcome.resulting_url != current_url:
                # Navigated to a new URL: hand it to the outer BFS like a
                # discovered link, so it gets its own depth budget and a fresh
                # `goto` rather than looping in place on the wrong page.
                graph.add_edge(key, outcome.resulting_url)
                if outcome.resulting_url not in queued_urls and len(queued_urls) < max_pages:
                    queued_urls.add(outcome.resulting_url)
                    queue.append((outcome.resulting_url, depth + 1))
                break

            # Same URL, new DOM state (validation message, item added, ...):
            # loop again without reloading, so the next iteration's fingerprint
            # reflects the change instead of reverting it.

    return graph


def explore_interactive(
    *,
    base_url: str,
    max_pages: int = 15,
    max_depth: int = 3,
    max_total_actions: int = 40,
    timeout_seconds: float = 15.0,
    headless: bool = True,
    policy: InteractionPolicy | None = None,
    llm: LlmClient | None = None,
) -> InteractionGraph:
    """Crawl same-origin pages from ``base_url``, filling forms and clicking
    through them instead of only following links.

    Requires the optional ``e2e`` extra (Playwright) and its browser binaries
    (``playwright install chromium``). Degrades to the top heuristic-ranked
    action on every page when ``llm`` has no real provider configured -
    nothing here requires a model to produce a useful result.
    """
    from playwright.sync_api import sync_playwright

    root = _normalize(base_url)
    timeout_ms = int(timeout_seconds * 1000)
    policy = policy or InteractionPolicy()
    llm = llm or LlmClient.from_settings()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_context().new_page()
            return _interact(
                page,
                root=root,
                policy=policy,
                llm=llm,
                max_pages=max_pages,
                max_depth=max_depth,
                max_total_actions=max_total_actions,
                timeout_ms=timeout_ms,
            )
        finally:
            browser.close()
