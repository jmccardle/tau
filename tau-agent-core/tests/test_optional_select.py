"""An optional select may be left unanswered (#101): spec, answer check, headless value."""

from __future__ import annotations

import pytest

from tau_agent_core.extension_types import (
    form_headless_value,
    validate_ask_spec,
    validate_form_spec,
    validate_form_values,
)

_VERDICT = {
    "name": "verdict",
    "kind": "select",
    "options": ["fine", "wrong"],
    "required": False,
}


def _fields(*extra: dict) -> list[dict]:
    return validate_form_spec({"fields": [_VERDICT, *extra]})[1]


def test_the_spec_keeps_required_false() -> None:
    assert _fields()[0]["required"] is False


def test_the_ask_spec_keeps_it_too() -> None:
    ask = validate_ask_spec({"fields": [_VERDICT], "actions": [{"label": "step", "command": "go"}]})
    assert ask["fields"][0]["required"] is False


def test_required_must_be_a_bool() -> None:
    with pytest.raises(ValueError, match="'required' must be a bool"):
        validate_form_spec({"fields": [{**_VERDICT, "required": "no"}]})


def test_an_optional_select_answers_none() -> None:
    validate_form_values(_fields(), {"verdict": None})


def test_a_required_select_refuses_none() -> None:
    plain = validate_form_spec(
        {"fields": [{k: v for k, v in _VERDICT.items() if k != "required"}]}
    )[1]
    with pytest.raises(ValueError, match="not one of"):
        validate_form_values(plain, {"verdict": None})


def test_an_optional_select_still_refuses_a_stray_option() -> None:
    with pytest.raises(ValueError, match="not one of"):
        validate_form_values(_fields(), {"verdict": "maybe"})


def test_the_key_is_still_declared_not_absent() -> None:
    with pytest.raises(ValueError, match="missing value"):
        validate_form_values(_fields(), {})


def test_headless_defaults_leave_an_optional_select_unanswered() -> None:
    assert form_headless_value(_fields()[0]) is None
