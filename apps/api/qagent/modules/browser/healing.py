"""Self-healing selectors (CLAUDE.md §11).

When a selector a test relies on disappears, the honest options are "fail" or
"propose a replacement for a human to approve" — never rewrite the test
silently. This module only ever does the former's homework: it scores which
remaining elements plausibly replace a vanished one and reports a confidence,
and leaves applying that change to a human. Nothing here writes to a test file.

Matching is attribute- and text-based, not DOM-position-based, because the
CLAUDE.md example (``[data-testid="checkout"]`` replaced by
``[aria-label="Checkout"]``) is exactly the case a position or CSS-path
heuristic would miss: the element moved, but what it *means* didn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Attributes worth comparing across old and new selectors/elements, most
#: semantically stable first. ``id``/``data-testid`` are handled separately
#: since they get first refusal when building a *new* selector.
_SEMANTIC_ATTRS = ("data-testid", "aria-label", "name", "role", "placeholder", "title", "type")

_ATTR_RE = re.compile(r"\[([a-zA-Z0-9_:-]+)(?:=(\"([^\"]*)\"|'([^']*)'|([^\]]+)))?\]")
_TAG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*")
_ID_RE = re.compile(r"#([a-zA-Z0-9_-]+)")
_CLASS_RE = re.compile(r"\.([a-zA-Z0-9_-]+)")


@dataclass
class ParsedSelector:
    tag: str | None = None
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)

    def tokens(self) -> set[str]:
        """Every value this selector asserts, lowercased, for fuzzy comparison."""
        out = {v.lower() for v in self.attrs.values() if v}
        if self.id:
            out.add(self.id.lower())
        out.update(c.lower() for c in self.classes)
        return out


@dataclass
class ElementDescriptor:
    """A live DOM element, as extracted by the browser integration below."""

    tag: str
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)
    text: str | None = None

    def tokens(self) -> set[str]:
        out = {v.lower() for v in self.attrs.values() if v}
        if self.id:
            out.add(self.id.lower())
        out.update(c.lower() for c in self.classes)
        if self.text:
            out.add(self.text.strip().lower())
        return out


@dataclass
class HealingProposal:
    old_selector: str
    new_selector: str
    confidence: float
    #: Always true: CLAUDE.md §11 — never silently modify a test.
    requires_approval: bool = True


def parse_selector(selector: str) -> ParsedSelector:
    """Parse the narrow slice of CSS this module needs: one tag, one id,
    zero-or-more classes, zero-or-more ``[attr]``/``[attr="value"]`` clauses.
    Combinators and pseudo-selectors are out of scope — a selector using them
    is returned with whatever prefix could be read, which is enough to fail
    the confidence threshold rather than crash.
    """
    parsed = ParsedSelector()
    tag_match = _TAG_RE.match(selector)
    if tag_match:
        parsed.tag = tag_match.group(0).lower()

    id_match = _ID_RE.search(selector)
    if id_match:
        parsed.id = id_match.group(1)

    parsed.classes = [m.lower() for m in _CLASS_RE.findall(selector)]

    for name, _, dq, sq, bare in _ATTR_RE.findall(selector):
        value = dq or sq or bare or ""
        parsed.attrs[name.lower()] = value

    return parsed


def score_candidate(old: ParsedSelector, candidate: ElementDescriptor) -> float:
    """0..1 confidence that ``candidate`` is what ``old`` used to point at."""
    score = 0.0

    if old.tag and old.tag == candidate.tag:
        score += 0.15

    old_tokens = old.tokens()
    candidate_tokens = candidate.tokens()
    if old_tokens:
        overlap = old_tokens & candidate_tokens
        # Any shared semantically-meaningful token (an id, a testid value, a
        # label, a class) is strong evidence — this is the "moved but still
        # means the same thing" signal the docstring calls out.
        score += 0.65 * (len(overlap) / len(old_tokens))

    if old.id and candidate.id and old.id.lower() == candidate.id.lower():
        score += 0.20

    return round(min(score, 1.0), 3)


def build_selector(element: ElementDescriptor) -> str:
    """The most stable selector for an element: id, then a semantic attribute,
    then tag+class as a last resort."""
    if element.id:
        return f"#{element.id}"
    for attr in _SEMANTIC_ATTRS:
        if value := element.attrs.get(attr):
            return f'{element.tag}[{attr}="{value}"]'
    if element.classes:
        return f"{element.tag}." + ".".join(element.classes)
    return element.tag


def propose_replacement(
    old_selector: str, elements: list[ElementDescriptor], *, threshold: float = 0.7
) -> HealingProposal | None:
    """Find the best-scoring live element for a selector that no longer matches.

    Returns ``None`` below ``threshold`` — an unconfident guess is worse than
    an honest "couldn't find a replacement," since a human still has to look
    either way, and a wrong high-confidence guess erodes trust in every one
    after it.
    """
    old = parse_selector(old_selector)
    best: ElementDescriptor | None = None
    best_score = 0.0

    for element in elements:
        candidate_score = score_candidate(old, element)
        if candidate_score > best_score:
            best, best_score = element, candidate_score

    if best is None or best_score < threshold:
        return None

    return HealingProposal(
        old_selector=old_selector,
        new_selector=build_selector(best),
        confidence=best_score,
    )


def extract_elements(page: object) -> list[ElementDescriptor]:
    """Pull every interactive/identifiable element out of the current page.

    ``page`` is a Playwright ``Page``; typed loosely so this stays importable
    (and this function stays callable with a fake) without the ``e2e`` extra.
    """
    raw = page.eval_on_selector_all(  # type: ignore[attr-defined]
        "a, button, input, select, textarea, [role], [data-testid]",
        """els => els.map(e => ({
            tag: e.tagName.toLowerCase(),
            id: e.id || null,
            classes: e.className && typeof e.className === 'string'
                ? e.className.split(/\\s+/).filter(Boolean) : [],
            attrs: Object.fromEntries(
                ['data-testid', 'aria-label', 'name', 'role', 'placeholder', 'title', 'type']
                    .map(a => [a, e.getAttribute(a)])
                    .filter(([, v]) => v)
            ),
            text: (e.innerText || e.value || '').trim().slice(0, 80) || null,
        }))""",
    )
    return [
        ElementDescriptor(
            tag=item["tag"],
            id=item.get("id"),
            classes=item.get("classes") or [],
            attrs=item.get("attrs") or {},
            text=item.get("text"),
        )
        for item in raw
    ]


def find_replacements(
    *,
    base_url: str,
    old_selectors: list[str],
    threshold: float = 0.7,
    timeout_seconds: float = 15.0,
    headless: bool = True,
) -> dict[str, HealingProposal | None]:
    """Navigate to ``base_url`` and propose a replacement for each of ``old_selectors``.

    Requires the optional ``e2e`` extra (Playwright) and its browser binaries.
    """
    from playwright.sync_api import sync_playwright

    timeout_ms = int(timeout_seconds * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_context().new_page()
            page.goto(base_url, timeout=timeout_ms, wait_until="load")
            elements = extract_elements(page)
        finally:
            browser.close()

    return {
        selector: propose_replacement(selector, elements, threshold=threshold)
        for selector in old_selectors
    }
