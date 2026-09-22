"""Token counting: provenance, the class estimator, and the online fit.

The claim under test throughout is that a number knows where it came from. A
count that says ``exact=True`` must be one, and a count that is a guess must say
so — that is the whole reason :class:`TokenCount` exists instead of ``int``.
"""

from __future__ import annotations

import pytest

from tau_llm.tokens import (
    CALIBRATION_MIN_OBSERVATIONS,
    CLASS_COEFFICIENTS,
    CLASS_FEATURES,
    ZERO_TOKENS,
    CalibrationState,
    CharClassCounter,
    ContextCalibrator,
    TextCounter,
    TokenCount,
    TokenizerCounter,
    TokenizerUnavailable,
    character_classes,
    clear_counter_cache,
    counter_for,
)
from tau_llm.types import Model


def _model(**kwargs) -> Model:
    base = dict(
        id="m",
        name="M",
        api="openai-completions",
        provider="openai",
        base_url="http://example.invalid/v1",
        context_window=128000,
        max_tokens=4096,
    )
    base.update(kwargs)
    return Model(**base)  # type: ignore[arg-type]


class _StubTokenizer:
    """One token per whitespace-separated word, so counts are predictable."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text: str, add_special_tokens: bool = True):
        self.calls += 1
        return type("Enc", (), {"ids": list(range(len(text.split())))})()


# ── TokenCount ───────────────────────────────────────────────────────────────


def test_negative_token_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        TokenCount(tokens=-1, source="usage", exact=True, includes_template=True)


def test_int_of_a_count_is_its_tokens() -> None:
    assert int(TokenCount(tokens=42, source="usage", exact=True, includes_template=True)) == 42


def test_adding_an_estimate_to_an_exact_count_gives_an_estimate() -> None:
    exact = TokenCount(tokens=100, source="tokenizer", exact=True, includes_template=False)
    guess = TokenCount(tokens=50, source="classes", exact=False, includes_template=False)
    total = exact + guess
    assert total.tokens == 150
    assert total.exact is False


def test_adding_a_bare_count_to_a_framed_one_drops_the_framing_claim() -> None:
    framed = TokenCount(tokens=100, source="usage", exact=True, includes_template=True)
    bare = TokenCount(tokens=10, source="usage", exact=True, includes_template=False)
    assert (framed + bare).includes_template is False


def test_zero_is_the_identity_and_keeps_the_other_provenance() -> None:
    guess = TokenCount(tokens=7, source="classes", exact=False, includes_template=False)
    assert ZERO_TOKENS + guess == guess
    assert guess + ZERO_TOKENS == guess


# ── character classes ────────────────────────────────────────────────────────


def test_character_classes_partition_the_string() -> None:
    text = "Ab3 !é"
    lo, up, di, sp, pu, na = character_classes(text)
    assert (lo, up, di, sp, pu, na) == (1, 1, 1, 1, 1, 1)
    assert lo + up + di + sp + pu + na == len(text)


@pytest.mark.parametrize(
    "text", ["", "plain prose here", "{\"k\": 12345}", "def f(x):\n\treturn x", "你好"]
)
def test_character_classes_always_sum_to_the_length(text: str) -> None:
    assert sum(character_classes(text)) == len(text)


# ── CharClassCounter ─────────────────────────────────────────────────────────


def test_class_counter_is_never_exact() -> None:
    counter = CharClassCounter()
    assert counter.exact is False
    assert counter.count("some prose").exact is False
    assert counter.source == "classes"


def test_empty_text_costs_nothing_and_says_so_exactly() -> None:
    assert CharClassCounter().count("") == ZERO_TOKENS


def test_digits_cost_far_more_than_whitespace() -> None:
    """The reason one chars-per-token ratio cannot serve both prose and code."""
    counter = CharClassCounter()
    digits = counter.count("1234567890" * 20, role="toolResult")
    spaces = counter.count(" " * 200, role="toolResult")
    assert digits.tokens > spaces.tokens * 5


def test_counting_is_monotone_in_added_text() -> None:
    counter = CharClassCounter()
    short = counter.count("the quick brown fox", role="assistant")
    longer = counter.count("the quick brown fox jumps over the lazy dog", role="assistant")
    assert longer.tokens > short.tokens


def test_an_unknown_role_falls_to_the_default_row() -> None:
    counter = CharClassCounter()
    assert counter.count("hello there", role="banana") == counter.count("hello there")


def test_negative_coefficients_are_rejected() -> None:
    bad = dict(CLASS_COEFFICIENTS)
    bad["default"] = (-0.1,) + tuple(CLASS_COEFFICIENTS["default"][1:])
    with pytest.raises(ValueError, match="negative weight"):
        CharClassCounter(bad)


def test_wrong_width_coefficients_are_rejected() -> None:
    with pytest.raises(ValueError, match="expected 7"):
        CharClassCounter({"default": (1.0, 2.0)})


def test_coefficients_without_a_default_row_are_rejected() -> None:
    with pytest.raises(ValueError, match="'default' row"):
        CharClassCounter({"assistant": CLASS_COEFFICIENTS["assistant"]})


def test_shipped_coefficients_are_all_non_negative_and_the_right_width() -> None:
    for role, row in CLASS_COEFFICIENTS.items():
        assert len(row) == len(CLASS_FEATURES), role
        assert all(w >= 0 for w in row), role


# ── TokenizerCounter ─────────────────────────────────────────────────────────


def test_tokenizer_counter_is_exact_about_text_and_not_about_framing() -> None:
    counter = TokenizerCounter(_StubTokenizer(), name="stub")
    count = counter.count("one two three")
    assert count.tokens == 3
    assert count.exact is True
    assert count.includes_template is False
    assert count.source == "tokenizer"


def test_tokenizer_counter_rejects_an_object_that_cannot_encode() -> None:
    with pytest.raises(TypeError, match="encode"):
        TokenizerCounter(object())


def test_tokenizer_counter_does_not_call_the_tokenizer_for_empty_text() -> None:
    stub = _StubTokenizer()
    assert TokenizerCounter(stub).count("") == ZERO_TOKENS
    assert stub.calls == 0


def test_both_counters_satisfy_the_protocol() -> None:
    assert isinstance(CharClassCounter(), TextCounter)
    assert isinstance(TokenizerCounter(_StubTokenizer()), TextCounter)


# ── counter_for ──────────────────────────────────────────────────────────────


def test_a_model_with_no_tokenizer_gets_the_class_counter() -> None:
    counter = counter_for(_model())
    assert counter.source == "classes"


def test_a_named_tokenizer_that_does_not_load_raises_by_default() -> None:
    clear_counter_cache()
    model = _model(tokenizer="/nonexistent/path/tokenizer.json")
    with pytest.raises((OSError, TokenizerUnavailable, Exception)):
        counter_for(model)


def test_a_named_tokenizer_that_does_not_load_can_be_asked_to_degrade() -> None:
    clear_counter_cache()
    model = _model(tokenizer="/nonexistent/path/tokenizer.json")
    assert counter_for(model, fallback=True).source == "classes"


def test_a_failed_load_is_not_cached_as_a_success() -> None:
    clear_counter_cache()
    model = _model(tokenizer="/nonexistent/path/tokenizer.json")
    counter_for(model, fallback=True)
    with pytest.raises(Exception):
        counter_for(model)


# ── ContextCalibrator ────────────────────────────────────────────────────────


def _feed(cal: ContextCalibrator, a: float, r: float, turns: int = 4) -> None:
    for i in range(turns):
        n = 4 + 2 * i
        payload = 1000 + 900 * i
        cal.observe(messages=n, payload_tokens=payload, billed_tokens=round(a * n + r * payload))


def test_a_cold_calibrator_will_not_solve() -> None:
    cal = ContextCalibrator()
    assert cal.solve() is None
    assert cal.ready is False


def test_it_stays_cold_until_the_minimum_observations() -> None:
    cal = ContextCalibrator()
    for i in range(CALIBRATION_MIN_OBSERVATIONS - 1):
        cal.observe(messages=2 + i, payload_tokens=100 * (i + 1), billed_tokens=300 * (i + 1))
        assert cal.solve() is None


def test_it_recovers_planted_parameters() -> None:
    cal = ContextCalibrator()
    _feed(cal, a=200.0, r=1.05)
    a, r = cal.solve()
    assert a == pytest.approx(200.0, abs=0.5)
    assert r == pytest.approx(1.05, abs=0.001)


def test_an_uncalibrated_predict_returns_the_payload_unchanged() -> None:
    cal = ContextCalibrator()
    payload = TokenCount(tokens=500, source="tokenizer", exact=True, includes_template=False)
    assert cal.predict(messages=3, payload=payload) is payload


def test_a_calibrated_predict_adds_framing_and_says_it_did() -> None:
    cal = ContextCalibrator()
    _feed(cal, a=200.0, r=1.05)
    payload = TokenCount(tokens=1000, source="tokenizer", exact=True, includes_template=False)
    out = cal.predict(messages=10, payload=payload)
    assert out.tokens == pytest.approx(200 * 10 + 1050, abs=2)
    assert out.includes_template is True
    assert out.exact is False
    assert out.source == "calibrated"


def test_an_absurd_multiplier_is_refused_rather_than_returned() -> None:
    """A payload that needs tripling is measuring something else; the fit says None."""
    cal = ContextCalibrator()
    for i in range(6):
        n = 4 + i
        cal.observe(messages=n, payload_tokens=100 * (i + 1), billed_tokens=300 * (i + 1) + n)
    assert cal.solve() is None


def test_a_negative_framing_cost_is_clamped_not_returned() -> None:
    """Framing cannot refund tokens, so ``a`` is floored at 0 and ``r`` re-solved."""
    cal = ContextCalibrator()
    for i in range(6):
        n = 4 + 3 * i
        payload = 1000 + 800 * i
        cal.observe(messages=n, payload_tokens=payload, billed_tokens=round(1.02 * payload - 5 * n))
    solved = cal.solve()
    assert solved is not None
    a, _ = solved
    assert a >= 0.0


def test_observing_an_impossible_turn_raises() -> None:
    cal = ContextCalibrator()
    with pytest.raises(ValueError, match="messages must be > 0"):
        cal.observe(messages=0, payload_tokens=10, billed_tokens=10)
    with pytest.raises(ValueError, match="must be >= 0"):
        cal.observe(messages=1, payload_tokens=-1, billed_tokens=10)


def test_state_round_trips_and_carries_the_fit() -> None:
    cal = ContextCalibrator()
    _feed(cal, a=150.0, r=1.1)
    revived = ContextCalibrator(CalibrationState.from_dict(cal.state.to_dict()))
    assert revived.observations == cal.observations
    assert revived.solve() == pytest.approx(cal.solve())


def test_a_state_missing_a_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing"):
        CalibrationState.from_dict({"nn": 1.0, "count": 1})
