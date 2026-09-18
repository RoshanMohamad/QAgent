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
    const tag = e.tagName.toLowerCase();
    // e.getAttribute('type') only sees an *explicit* attribute. A plain
    // <button> with no type inside a <form> defaults to type="submit" per the
    // HTML spec, and a plain <input> defaults to "text" - the DOM .type
    // property resolves that default, getAttribute does not. Getting this
    // wrong misses the single most common submit control shape on real forms.
    const effectiveType = (tag === 'button' || tag === 'input') ? (e.type || null) : null;
    const explicitAttrs = ['data-testid', 'aria-label', 'name', 'role', 'placeholder', 'title']
        .map(a => [a, e.getAttribute(a)])
        .filter(([, v]) => v);
    const typeValue = effectiveType || e.getAttribute('type');
    if (typeValue) explicitAttrs.push(['type', typeValue]);
    return {
        tag: tag,
        id: e.id || null,
        classes: e.className && typeof e.className === 'string'
            ? e.className.split(/\\s+/).filter(Boolean) : [],
        attrs: Object.fromEntries(explicitAttrs),
        text: (e.innerText || e.value || '').trim().slice(0, 80) || null,
        required: !!e.required,
        disabled: !!e.disabled,
        visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
        in_form: !!e.closest('form'),
        form_index: e.form ? Array.from(document.forms).indexOf(e.form) : null,
        label: labelText.trim().slice(0, 80) || null,
        options: tag === 'select'
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
    #: Index into ``document.forms`` for the element's owning form, or
    #: ``None`` when the element has no owning form (a bare ``<a>``, or any
    #: form-associated element outside every ``<form>``). Distinct forms on
    #: one page get distinct indices, which is what lets required-field
    #: gating (modules/explorer/interact.py) scope itself to "this form",
    #: rather than blocking one form's submit because a wholly unrelated
    #: form elsewhere on the page still has an empty required field.
    form_index: int | None = None
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
            form_index=item.get("form_index"),
            label=item.get("label"),
            options=item.get("options") or [],
        )
        for item in raw
    ]
