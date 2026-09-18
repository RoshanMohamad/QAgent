"""Actionable-element extraction: pure mapping logic, no browser needed."""

from __future__ import annotations

from qagent.modules.explorer.elements import ActionableElement, extract_actionable_elements


class _FakePage:
    def __init__(self, raw_elements: list[dict]):
        self._raw = raw_elements

    def eval_on_selector_all(self, selector: str, script: str) -> list[dict]:
        return self._raw


def test_extract_maps_raw_dicts_to_actionable_elements() -> None:
    page = _FakePage(
        [
            {
                "tag": "input",
                "id": "email",
                "classes": [],
                "attrs": {"type": "email", "name": "email"},
                "text": None,
                "required": True,
                "disabled": False,
                "visible": True,
                "in_form": True,
                "label": "Email address",
                "options": [],
            }
        ]
    )

    elements = extract_actionable_elements(page)

    assert len(elements) == 1
    el = elements[0]
    assert isinstance(el, ActionableElement)
    assert el.tag == "input"
    assert el.required is True
    assert el.in_form is True
    assert el.label == "Email address"


def test_extract_defaults_missing_optional_fields() -> None:
    page = _FakePage([{"tag": "a", "id": None, "classes": [], "attrs": {}, "text": "Home"}])

    elements = extract_actionable_elements(page)

    el = elements[0]
    assert el.required is False
    assert el.disabled is False
    assert el.visible is True
    assert el.in_form is False
    assert el.label is None
    assert el.options == []


def test_extract_carries_select_options() -> None:
    page = _FakePage(
        [
            {
                "tag": "select",
                "id": None,
                "classes": [],
                "attrs": {"name": "country"},
                "text": None,
                "options": ["", "us", "ca"],
            }
        ]
    )

    elements = extract_actionable_elements(page)

    assert elements[0].options == ["", "us", "ca"]


def test_extract_carries_form_index_for_scoping_required_fields_per_form() -> None:
    page = _FakePage(
        [
            {"tag": "input", "id": None, "classes": [], "attrs": {}, "text": None, "form_index": 0},
            {"tag": "input", "id": None, "classes": [], "attrs": {}, "text": None, "form_index": 1},
            {"tag": "a", "id": None, "classes": [], "attrs": {}, "text": "Home", "form_index": None},
        ]
    )

    elements = extract_actionable_elements(page)

    assert [el.form_index for el in elements] == [0, 1, None]


def test_extract_marks_disabled_and_invisible_elements() -> None:
    page = _FakePage(
        [
            {"tag": "button", "id": None, "classes": [], "attrs": {}, "text": "Go", "disabled": True},
            {"tag": "button", "id": None, "classes": [], "attrs": {}, "text": "Hidden", "visible": False},
        ]
    )

    elements = extract_actionable_elements(page)

    assert elements[0].disabled is True
    assert elements[1].visible is False
