"""Action enumeration/ranking is pure; arbitration is tested against
NullProvider (never calls out) and a fake provider (schema-violation safety).
"""

from __future__ import annotations

from qagent.config import get_settings
from qagent.modules.explorer.actions import (
    ARBITRATION_THRESHOLD,
    Action,
    ActionOutcome,
    ActionType,
    ElementRef,
    InteractionPolicy,
    arbitrate_action,
    choose_action,
    classify_action_outcome,
    enumerate_actions,
)
from qagent.modules.explorer.elements import ActionableElement
from qagent.modules.llm.budget import Budget
from qagent.modules.llm.client import LlmClient
from qagent.modules.llm.providers import LlmResult, NullProvider


def _ref(**kwargs) -> ElementRef:
    defaults = dict(selector="x", tag="button", role=None, input_type=None, text=None, testid=None)
    defaults.update(kwargs)
    return ElementRef(**defaults)


def _null_llm() -> LlmClient:
    return LlmClient(settings=get_settings(), provider=NullProvider(), budget=Budget(10, 1000, 1.0))


# --------------------------------------------------------------------------- enumerate_actions


def test_destructive_elements_are_excluded_by_default() -> None:
    elements = [
        ActionableElement(tag="button", text="Delete account", attrs={}),
        ActionableElement(tag="button", text="Save changes", attrs={}),
    ]

    actions = enumerate_actions(elements)

    assert all("delete" not in (a.target.text or "").lower() for a in actions)
    assert any("save" in (a.target.text or "").lower() for a in actions)


def test_destructive_elements_allowed_when_policy_opts_in() -> None:
    elements = [ActionableElement(tag="button", text="Delete account", attrs={})]
    policy = InteractionPolicy(allow_destructive=True)

    actions = enumerate_actions(elements, policy)

    assert len(actions) == 1
    assert actions[0].type is ActionType.CLICK


def test_disabled_and_invisible_elements_are_dropped() -> None:
    elements = [
        ActionableElement(tag="button", text="Go", attrs={}, disabled=True),
        ActionableElement(tag="button", text="Also go", attrs={}, visible=False),
    ]

    assert enumerate_actions(elements) == []


def test_submit_button_outranks_generic_click() -> None:
    elements = [
        ActionableElement(tag="button", text="something vague", attrs={}),
        ActionableElement(tag="button", text="Submit", attrs={"data-testid": "submit-btn"}),
    ]

    actions = enumerate_actions(elements)

    assert actions[0].target.testid == "submit-btn"


def test_required_field_outranks_optional_one() -> None:
    elements = [
        ActionableElement(tag="input", attrs={"type": "text", "name": "optional"}, required=False),
        ActionableElement(tag="input", attrs={"type": "text", "name": "required"}, required=True),
    ]

    actions = enumerate_actions(elements)

    assert actions[0].target.selector != actions[1].target.selector
    assert actions[0].score >= actions[1].score


def test_select_picks_second_option_skipping_placeholder() -> None:
    elements = [ActionableElement(tag="select", attrs={"name": "country"}, options=["", "us", "ca"])]

    actions = enumerate_actions(elements)

    assert actions[0].type is ActionType.SELECT
    assert actions[0].value == "us"


def test_text_input_becomes_a_fill_action_with_synthetic_value() -> None:
    elements = [ActionableElement(tag="input", attrs={"type": "email", "name": "email"})]

    actions = enumerate_actions(elements)

    assert actions[0].type is ActionType.FILL
    assert "@" in actions[0].value


def test_fill_fake_data_false_skips_fill_actions() -> None:
    elements = [ActionableElement(tag="input", attrs={"type": "email", "name": "email"})]
    policy = InteractionPolicy(fill_fake_data=False)

    assert enumerate_actions(elements, policy) == []


def test_max_actions_per_page_caps_result() -> None:
    elements = [ActionableElement(tag="button", text=f"btn {i}", attrs={}) for i in range(10)]
    policy = InteractionPolicy(max_actions_per_page=3)

    assert len(enumerate_actions(elements, policy)) == 3


# --------------------------------------------------------------------------- arbitration


def test_arbitrate_action_never_calls_model_when_unavailable() -> None:
    ranked = [Action(type=ActionType.CLICK, target=_ref(), score=0.9)]

    result = arbitrate_action(ranked, page_context={"url": "http://x", "title": ""}, llm=_null_llm())

    assert result is None


def test_choose_action_returns_top_pick_when_llm_unavailable() -> None:
    ranked = [
        Action(type=ActionType.CLICK, target=_ref(text="a"), score=0.1),
        Action(type=ActionType.CLICK, target=_ref(text="b"), score=0.05),
    ]

    chosen = choose_action(ranked, page_context={"url": "http://x", "title": ""}, llm=_null_llm())

    assert chosen is ranked[0]


def test_choose_action_empty_candidates_returns_none() -> None:
    assert choose_action([], page_context={}, llm=_null_llm()) is None


class _FakeProvider:
    name = "fake"
    is_real = True

    def __init__(self, data: dict):
        self._data = data

    def complete_json(self, *, system, user, schema, model, max_tokens=2048) -> LlmResult:
        return LlmResult(
            data=self._data, input_tokens=1, output_tokens=1, latency_ms=1, model=model, provider="fake"
        )


def test_arbitrate_action_falls_back_on_out_of_range_index() -> None:
    ranked = [Action(type=ActionType.CLICK, target=_ref(), score=0.1)]
    llm = LlmClient(
        settings=get_settings(),
        provider=_FakeProvider({"chosen_index": 99, "reason": "nope"}),
        budget=Budget(10, 1000, 1.0),
    )

    result = arbitrate_action(ranked, page_context={"url": "http://x", "title": ""}, llm=llm)

    assert result is None


def test_arbitrate_action_accepts_a_valid_index() -> None:
    ranked = [
        Action(type=ActionType.CLICK, target=_ref(text="a"), score=0.1),
        Action(type=ActionType.CLICK, target=_ref(text="b"), score=0.1),
    ]
    llm = LlmClient(
        settings=get_settings(),
        provider=_FakeProvider({"chosen_index": 1, "reason": "more likely to reveal new state"}),
        budget=Budget(10, 1000, 1.0),
    )

    result = arbitrate_action(ranked, page_context={"url": "http://x", "title": ""}, llm=llm)

    assert result is not None
    assert result.target.text == "b"
    assert result.source == "llm_arbitration"


def test_choose_action_uses_arbitration_when_ambiguous() -> None:
    ranked = [
        Action(type=ActionType.CLICK, target=_ref(text="a"), score=ARBITRATION_THRESHOLD - 0.1),
        Action(type=ActionType.CLICK, target=_ref(text="b"), score=ARBITRATION_THRESHOLD - 0.2),
    ]
    llm = LlmClient(
        settings=get_settings(),
        provider=_FakeProvider({"chosen_index": 1, "reason": "b is better"}),
        budget=Budget(10, 1000, 1.0),
    )

    chosen = choose_action(ranked, page_context={"url": "http://x", "title": ""}, llm=llm)

    assert chosen.target.text == "b"
    assert chosen.source == "llm_arbitration"


# --------------------------------------------------------------------------- classify_action_outcome


def test_classify_action_outcome_ignores_failed_actions() -> None:
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()), ok=False, resulting_url=None, error="boom"
    )

    assert classify_action_outcome(outcome) is None


def test_classify_action_outcome_ignores_clean_success() -> None:
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()), ok=True, resulting_url="http://x/next"
    )

    assert classify_action_outcome(outcome) is None


def test_classify_action_outcome_flags_page_errors_as_real_bug() -> None:
    outcome = ActionOutcome(
        action=Action(type=ActionType.CLICK, target=_ref()),
        ok=True,
        resulting_url="http://x/next",
        page_errors=["TypeError: cannot read property 'price' of undefined"],
    )

    verdict = classify_action_outcome(outcome)

    assert verdict is not None
    assert verdict.failure_class.value == "real_bug"
