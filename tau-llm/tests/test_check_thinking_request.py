"""#74: the refusal for a thinking level a wire cannot honour is one pure function."""

from __future__ import annotations

import pytest

from tau_llm import Model, ThinkingRefused, check_thinking_request


def _model(**kw) -> Model:
    return Model(
        id="m",
        name="n",
        api="openai-completions",
        provider="openai",
        base_url="http://h/v1",
        context_window=1000,
        max_tokens=100,
        **kw,
    )


def test_off_without_a_declared_off_form_is_refused_before_any_request():
    with pytest.raises(
        ThinkingRefused, match=r"models\.n\.thinking_level_map\.off.*--thinking LEVEL"
    ):
        check_thinking_request(_model(), "off")


def test_off_is_accepted_when_the_model_reasons_or_maps_it():
    assert check_thinking_request(_model(reasoning=True), "off") == "off"
    assert check_thinking_request(_model(thinking_level_map={"off": {}}), "off") == "off"
    assert check_thinking_request(_model(), None) is None
