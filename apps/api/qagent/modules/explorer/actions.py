"""Choosing what to click, fill, or submit next (CLAUDE.md sections 8-9).

Rules-first, exactly like ``modules/triage/classifier.py`` + ``triage/agent.py``:
a deterministic heuristic ranks every candidate action on the page, and a model
is consulted only when the ranking is genuinely ambiguous. The model is never
allowed to invent an action: it can only choose an index into the ranked list
this module already built from already-extracted elements, per ADR-0004's rule
that untrusted content (the DOM) may never expand the set of available actions.

Conservative by default: anything that looks destructive (delete, pay, cancel,
...) is dropped from the candidate list unless explicitly opted into, the same
way ``modules/browser/healing.py`` always requires human approval before a
selector change is applied.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any

from qagent.modules.browser.healing import build_selector
from qagent.modules.explorer.elements import ActionableElement
from qagent.modules.llm.client import LlmClient
from qagent.modules.llm.safety import fence
from qagent.modules.triage.classifier import FailureClass, Verdict

logger = logging.getLogger(__name__)

#: Below this top-ranked score (or within a hair of the runner-up), the choice
#: is ambiguous enough to be worth a model's opinion. Separate constant from
#: triage's ARBITRATION_THRESHOLD (0.70): heuristic action scores are noisier
#: than failure-classification signals, so a lower bar is appropriate.
ARBITRATION_THRESHOLD = 0.55
_NEAR_TIE_MARGIN = 0.05

_CTA_TEXT = {
    "submit", "continue", "next", "save", "confirm", "login", "sign in", "add", "search",
}
_GENERIC_TEXT = {"", "click here", "here", "learn more"}
_TEXT_INPUT_TYPES = {"text", "email", "search", "tel", "url", "number", "password", ""}
_NON_FILLABLE_INPUT_TYPES = {
    "submit", "button", "checkbox", "radio", "file", "hidden", "image", "reset", "range", "color",
}


class ActionType(enum.StrEnum):
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    SUBMIT = "submit"


@dataclass
class ElementRef:
    """Points back at an already-extracted ``ActionableElement`` — never a
    selector built ad hoc from untrusted content at arbitration time."""

    selector: str
    tag: str
    role: str | None
    input_type: str | None
    text: str | None
    testid: str | None
    #: The owning form's index (see ActionableElement.form_index), or None.
    #: Lets a caller scope "are this form's required fields filled" checks to
    #: the right form when a page has more than one.
    form_index: int | None = None


@dataclass
class Action:
    type: ActionType
    target: ElementRef
    value: str | None = None  # FILL/SELECT only
    reason: str = ""  # display-only rationale, never influences execution
    source: str = "heuristic"  # "heuristic" | "llm_arbitration"
    score: float = 0.0
    #: Mirrors the source element's ``required`` attribute. Lets a caller (see
    #: modules/explorer/interact.py) defer SUBMIT until every required FILL/
    #: SELECT candidate on the page has been attempted at least once.
    required: bool = False


@dataclass
class ActionOutcome:
    action: Action
    ok: bool
    resulting_url: str | None
    resulting_fingerprint: str | None = None
    error: str | None = None
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)


@dataclass
class InteractionPolicy:
    #: Hard cap on total actions *executed* on one page (across every branch
    #: attempt — see modules/explorer/interact.py), enforced by the caller.
    #: Not a ranking-visibility limit: enumerate_actions always returns every
    #: safe candidate so the required-field gate can see all of them.
    max_actions_per_page: int = 5
    allow_destructive: bool = False
    destructive_keywords: tuple[str, ...] = (
        "delete", "remove", "cancel", "unsubscribe", "deactivate", "terminate",
        "pay", "purchase", "charge", "checkout", "confirm order", "buy now",
    )
    #: Synthetic values only, never real PII. False skips FILL actions entirely.
    fill_fake_data: bool = True


# --------------------------------------------------------------------------- values


_SYNTHETIC_BY_TOKEN: tuple[tuple[str, str], ...] = (
    ("email", "qagent-probe@example.com"),
    ("phone", "555-0100"),
    ("tel", "555-0100"),
    ("url", "https://example.com"),
    ("website", "https://example.com"),
    ("zip", "94105"),
    ("postal", "94105"),
    ("first", "QAgent"),
    ("last", "Probe"),
    ("user", "qagent_probe"),
    ("search", "test"),
    ("name", "QAgent Probe"),
)

_SYNTHETIC_BY_TYPE: dict[str, str] = {
    "email": "qagent-probe@example.com",
    "tel": "555-0100",
    "url": "https://example.com",
    "number": "1",
    "password": "Qagent-Probe-1!",
    "search": "test",
}


def _synthesize_value(element: ActionableElement) -> str:
    """A deterministic, synthetic value for a fillable field — never real data."""
    input_type = (element.attrs.get("type") or "").lower()
    if input_type in _SYNTHETIC_BY_TYPE:
        return _SYNTHETIC_BY_TYPE[input_type]

    haystack = " ".join(
        filter(None, [element.attrs.get("name"), element.attrs.get("placeholder"), element.label])
    ).lower()
    for token, value in _SYNTHETIC_BY_TOKEN:
        if token in haystack:
            return value
    return "QAgent test value"


# --------------------------------------------------------------------------- ranking


def _looks_destructive(element: ActionableElement, keywords: tuple[str, ...]) -> bool:
    haystack = " ".join(
        filter(
            None,
            [element.text, element.attrs.get("aria-label"), element.attrs.get("name"), element.id],
        )
    ).lower()
    return any(keyword in haystack for keyword in keywords)


def _build_ref(element: ActionableElement) -> ElementRef:
    return ElementRef(
        selector=build_selector(element),
        tag=element.tag,
        role=element.attrs.get("role"),
        input_type=element.attrs.get("type"),
        text=element.text,
        testid=element.attrs.get("data-testid"),
        form_index=element.form_index,
    )


def _action_for_element(element: ActionableElement, policy: InteractionPolicy) -> Action | None:
    tag = element.tag
    input_type = (element.attrs.get("type") or "").lower()
    ref = _build_ref(element)

    if tag == "select":
        # Skip index 0: it's routinely an unselectable "Choose one" placeholder.
        value = element.options[1] if len(element.options) > 1 else None
        if value is None:
            return None
        return Action(
            type=ActionType.SELECT,
            target=ref,
            value=value,
            reason="choose an option",
            required=element.required,
        )

    if not policy.fill_fake_data:
        pass
    elif tag == "textarea" or (tag == "input" and input_type in _TEXT_INPUT_TYPES):
        return Action(
            type=ActionType.FILL,
            target=ref,
            value=_synthesize_value(element),
            reason="fill field",
            required=element.required,
        )

    # A plain <button> with no explicit type defaults to type="submit" per the
    # HTML spec *unconditionally* - including one with no owning form at all,
    # where "submitting" does nothing. Require in_form too, or a standalone
    # button (a JS-driven "Add to cart") gets wrongly treated as this page's
    # form submission and gated by required-field rules that don't apply to it.
    if element.in_form and (
        input_type == "submit" or (tag == "button" and "submit" in (element.text or "").lower())
    ):
        return Action(type=ActionType.SUBMIT, target=ref, reason="submit the form")

    if tag in {"button", "a"} or element.attrs.get("role") == "button" or input_type == "button":
        return Action(type=ActionType.CLICK, target=ref, reason="click")

    return None


def _score(element: ActionableElement, index: int) -> float:
    score = 0.0
    if element.attrs.get("data-testid"):
        score += 0.3
    text = (element.text or "").strip().lower()
    if any(cta in text for cta in _CTA_TEXT):
        score += 0.25
    if element.in_form:
        score += 0.15
    if element.required:
        score += 0.15
    score += 0.1 / (1 + index)
    if not text or text in _GENERIC_TEXT:
        score -= 0.2
    return round(score, 4)


def enumerate_actions(
    elements: list[ActionableElement], policy: InteractionPolicy | None = None
) -> list[Action]:
    """Rank every safe, actionable element on the page, highest first.

    Destructive-looking elements are dropped before scoring, never merely
    ranked low — a footgun that occasionally wins arbitration is not safe.

    Deliberately returns the *full* ranked list, uncapped: the required-fill-
    before-submit gate in modules/explorer/interact.py needs to see every
    still-required field to decide whether a SUBMIT is safe, and truncating
    here first would silently hide a required field the gate never gets a
    chance to defer against — exactly the bug that let a second form's submit
    fire before its own field was ever attempted. ``policy.max_actions_per_page``
    is enforced once, as a total-actions-spent-on-this-page budget, by the
    caller that actually executes actions — not as a visibility limit here.
    """
    policy = policy or InteractionPolicy()
    scored: list[Action] = []

    for index, element in enumerate(elements):
        if not element.visible or element.disabled:
            continue
        if not policy.allow_destructive and _looks_destructive(
            element, policy.destructive_keywords
        ):
            continue

        action = _action_for_element(element, policy)
        if action is None:
            continue

        action.score = _score(element, index)
        scored.append(action)

    scored.sort(key=lambda a: a.score, reverse=True)
    return scored


# --------------------------------------------------------------------------- arbitration


ACTION_CHOICE_SYSTEM = (
    "You are exploring a web application to find pages and states nobody has listed "
    "yet. Given a ranked list of possible next actions on the current page, choose "
    "the one most likely to reveal new application behavior (a new page, a new "
    "state, a validation message) rather than repeat something already seen. Prefer "
    "an action that completes a clear workflow (finishing a form, following a "
    "primary call-to-action) over an incidental or decorative control."
)

ACTION_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "chosen_index": {
            "type": "integer",
            "description": "Index into the numbered candidate list. Nothing outside it is valid.",
        },
        "reason": {"type": "string"},
    },
    "required": ["chosen_index", "reason"],
}


def _describe_candidates(ranked: list[Action]) -> str:
    lines = []
    for i, action in enumerate(ranked):
        descriptor = " ".join(
            part
            for part in [
                f"testid={action.target.testid!r}" if action.target.testid else None,
                f"text={action.target.text!r}" if action.target.text else None,
            ]
            if part
        )
        lines.append(f"[{i}] {action.type.value} <{action.target.tag}> {descriptor}".rstrip())
    return "\n".join(lines)


def arbitrate_action(
    ranked: list[Action], *, page_context: dict[str, Any], llm: LlmClient
) -> Action | None:
    """Ask the model to pick among ``ranked``, or return ``None`` to keep the top pick.

    The model receives only an already-extracted, already-ranked candidate list and
    can return nothing but an integer index into it (ADR-0004 point 3): it ranks a
    closed set, it never invents an action, a selector, or a value. Any response
    outside that closed set — an out-of-range index, malformed JSON, no provider
    configured, budget exhausted — falls back to the heuristic's own top pick.
    """
    if not ranked or not llm.available:
        return None

    candidates_block = fence(_describe_candidates(ranked), label="candidate_actions")
    page_block = fence(
        f"URL: {page_context.get('url', '')}\nTitle: {page_context.get('title', '')}",
        label="page_context",
    )

    data = llm.try_complete_json(
        purpose="explorer_action_arbitration",
        system=ACTION_CHOICE_SYSTEM,
        user=f"{page_block}\n\nCandidate actions:\n{candidates_block}",
        schema=ACTION_CHOICE_SCHEMA,
        max_tokens=256,
    )

    if not data or "chosen_index" not in data:
        return None

    try:
        index = int(data["chosen_index"])
    except (TypeError, ValueError):
        return None

    if not (0 <= index < len(ranked)):
        return None

    chosen = ranked[index]
    return Action(
        type=chosen.type,
        target=chosen.target,
        value=chosen.value,
        reason=str(data.get("reason") or chosen.reason),
        source="llm_arbitration",
        score=chosen.score,
    )


def choose_action(
    ranked: list[Action], *, page_context: dict[str, Any], llm: LlmClient
) -> Action | None:
    """The next action to take: the top heuristic pick, unless it is ambiguous
    (low score, or a near-tie with the runner-up) and a model is available to
    arbitrate among the same closed candidate set.
    """
    if not ranked:
        return None

    top = ranked[0]
    ambiguous = top.score < ARBITRATION_THRESHOLD or (
        len(ranked) > 1 and (top.score - ranked[1].score) < _NEAR_TIE_MARGIN
    )

    if ambiguous and llm.available:
        arbitrated = arbitrate_action(ranked, page_context=page_context, llm=llm)
        if arbitrated is not None:
            return arbitrated

    return top


# --------------------------------------------------------------------------- triage


def classify_action_outcome(outcome: ActionOutcome) -> Verdict | None:
    """Same evidence class as ``modules/browser/triage.classify_page_check``, but
    triggered by an interaction instead of a page load.

    A failed *action* (the element couldn't be found or clicked) is deliberately
    excluded here: that's evidence about the heuristic's selector, not about the
    application, so it's never treated as a defect (ADR-0002's reasoning against
    selector-based flakiness applies just as much to a click as to an assertion).
    An uncaught exception or console error while the action ran is unambiguous
    regardless of which element triggered it, so that alone is reported.
    """
    if not outcome.ok:
        return None
    if not outcome.page_errors and not outcome.console_errors:
        return None

    return Verdict(
        FailureClass.REAL_BUG,
        0.8,
        "An interactive action (click/fill/submit) triggered an uncaught exception or "
        "console error, which is unambiguous evidence of a defect regardless of which "
        "element was interacted with.",
        evidence=(outcome.page_errors[:3] + outcome.console_errors[:3]) or ["no detail captured"],
    )
