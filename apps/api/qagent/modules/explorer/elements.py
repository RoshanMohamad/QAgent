"""Actionable-element extraction for the interactive explorer.

Generalizes ``modules/browser/healing.py``'s ``extract_elements`` with the
extra DOM facts the heuristic action-ranker in ``modules/explorer/actions.py``
needs: visibility, ``required``, ``disabled``, whether the element sits inside
a ``<form>``, its associated ``<label>`` text, and ``<select>`` options.
``healing.py`` itself stays untouched — self-healing only ever needs identity
attributes (id, testid, class, text) to match a vanished selector, never form
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qagent.modules.browser.healing import ElementDescriptor

_ACTIONABLE_SELECTOR = "a, button, input, select, textarea, [role], [data-testid]"

#: Same base attribute/text extraction as healing.extract_elements, plus the
#: form-semantic facts action-ranking needs. Free text stays capped at 80
#: chars for the same reason healing.py caps it: it's a display/matching aid,
#: never something matched exactly.
_ACTIONABLE_JS = """els => els.map(e => {
    const labelText = (() => {
        if (e.labels && e.labels.length) return e.labels[0].innerText || '';
        if (e.id) {
            const byFor = document.querySelector(`label[for="${e.id}"]`);
            if (byFor) return byFor.innerText || '';
        }
        const parentLabel = e.closest('label');
        return parentLabel ? (parentLabel.innerText || '') : '';
    })();
    return {
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
        required: !!e.required,
        disabled: !!e.disabled,
        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
        in_form: !!e.closest('form'),
        label: labelText.trim().slice(0, 80) || null,
        options: e.tagName.toLowerCase() === 'select'
            ? Array.from(e.options).map(o => o.value).filter(v => v)
            : [],
    };
})"""


@dataclass
class ActionableElement(ElementDescriptor):
    """An ``ElementDescriptor`` plus the form/visibility facts action-ranking needs."""

    required: bool = False
    disabled: bool = False
    visible: bool = True
    in_form: bool = False
    label: str | None = None
    options: list[str] = field(default_factory=list)


def extract_actionable_elements(page: object) -> list[ActionableElement]:
    """Pull every actionable element out of the current page, with form semantics.

    ``page`` is a Playwright ``Page``; typed loosely (same as
    ``healing.extract_elements``) so this stays importable, and callable with
    a fake, without the ``e2e`` extra installed.
    """
    raw = page.eval_on_selector_all(  # type: ignore[attr-defined]
        _ACTIONABLE_SELECTOR, _ACTIONABLE_JS
    )
    return [
        ActionableElement(
            tag=item["tag"],
            id=item.get("id"),
            classes=item.get("classes") or [],
            attrs=item.get("attrs") or {},
            text=item.get("text"),
            required=bool(item.get("required")),
            disabled=bool(item.get("disabled")),
            visible=item.get("visible", True),
            in_form=bool(item.get("in_form")),
            label=item.get("label"),
            options=item.get("options") or [],
        )
        for item in raw
    ]
