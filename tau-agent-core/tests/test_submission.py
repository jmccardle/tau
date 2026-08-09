"""Tests for `tau_agent_core.submission` — the phase-1 dataclasses only.

Reference: docs/SUBMISSION-LIFECYCLE.md, "The dataclasses" section (phase 1, part 1),
and "Parity enforcement".

Nothing here is wired into `AgentSession` yet (that is the next work item) — these tests
exercise the module in isolation: construction, the `__post_init__` normalization
(`silent ⇒ store_history=False`) and validation (`correlation` must be JSON-safe,
recursively), the declared defaults, and the round-trip parity property that is what makes
a free-form `correlation: dict[str, Any]` safe to ship at all — if a `Submission` cannot
survive asdict() -> reconstruct, it is carrying a live object that will break the JSON and
webserver renderers three hops downstream (decision 4).
"""

from __future__ import annotations

import dataclasses
from typing import Any, get_args

import pytest

from tau_agent_core.submission import (
    MAX_SUBMISSION_DEPTH,
    MultitaskStrategy,
    Submission,
    SubmissionResult,
    SubmissionSource,
)


def _sub(**overrides: Any) -> Submission:
    """A minimal valid Submission, with `overrides` layered on top."""
    fields = {
        "text": "hello",
        "source": "interactive",
        "submitter": "human",
        "submission_id": "11111111-1111-1111-1111-111111111111",
    }
    fields.update(overrides)
    return Submission(**fields)


# ── defaults (spec: the Fail-Early / smuggling-resistant defaults) ─────────


def test_defaults_are_the_fail_early_ones() -> None:
    sub = _sub()
    # reject, not enqueue: pi throws when streaming and no behaviour is named; "reject" is
    # that failure as a value. A default of "enqueue" would silently paper over the missing
    # concurrency guard prompt() has today.
    assert sub.multitask_strategy == "reject"
    # False, not True: injected text (e.g. off a bus) must never command-dispatch by default.
    assert sub.expand_commands is False
    assert sub.allow_user_input is False
    assert sub.store_history is True
    assert sub.silent is False
    assert sub.correlation == {}
    assert sub.depth == 0
    assert sub.images is None


def test_max_submission_depth_is_ten() -> None:
    # decision 3. Enforcement is submit()'s job (next work item) — this module only
    # declares the bound and stores whatever depth it is given; see
    # test_depth_beyond_max_does_not_raise_at_construction below.
    assert MAX_SUBMISSION_DEPTH == 10


def test_depth_beyond_max_does_not_raise_at_construction() -> None:
    # The cap is enforced in AgentSession.submit(), not here — a Submission is just a
    # record. Pinning this now documents the boundary so a future submit() patch cannot
    # accidentally move the check here without a test noticing the behaviour changed.
    sub = _sub(depth=MAX_SUBMISSION_DEPTH + 5)
    assert sub.depth == MAX_SUBMISSION_DEPTH + 5


# ── silent ⇒ store_history=False, resolved once in __post_init__ ──────────


def test_silent_forces_store_history_false_from_default() -> None:
    sub = _sub(silent=True)
    assert sub.store_history is False


def test_silent_forces_store_history_false_overriding_explicit_true() -> None:
    sub = _sub(silent=True, store_history=True)
    assert sub.store_history is False


def test_silent_false_leaves_store_history_untouched() -> None:
    assert _sub(silent=False, store_history=True).store_history is True
    assert _sub(silent=False, store_history=False).store_history is False


def test_silent_true_store_history_false_is_stable() -> None:
    # Already-consistent input is left alone (idempotent normalization).
    sub = _sub(silent=True, store_history=False)
    assert sub.store_history is False


# ── correlation validation (decision 4) ────────────────────────────────────


def test_correlation_accepts_json_scalars_lists_and_dicts() -> None:
    sub = _sub(
        correlation={
            "subject": "agent.turn.request",
            "binding_id": 7,
            "retry": 1.5,
            "urgent": True,
            "parent": None,
            "tags": ["a", "b", 3],
            "meta": {"nested": {"deeper": [1, 2, {"k": "v"}]}},
        }
    )
    assert sub.correlation["meta"]["nested"]["deeper"][2]["k"] == "v"


def test_correlation_rejects_a_live_object_at_top_level() -> None:
    class FakeNatsMessage:
        pass

    with pytest.raises(ValueError, match="not a JSON scalar/list/dict"):
        _sub(correlation={"msg": FakeNatsMessage()})


def test_correlation_rejects_a_live_object_nested_two_levels_deep() -> None:
    # This is exactly the failure decision 4 names: a live object riding inside a nested
    # dict/list, which would otherwise only fail three hops downstream in a JSON renderer.
    class FakeNatsMessage:
        pass

    with pytest.raises(ValueError, match=r"correlation\['bus'\]\['msg'\]"):
        _sub(correlation={"bus": {"subject": "x", "msg": FakeNatsMessage()}})

    with pytest.raises(ValueError, match="not a JSON scalar/list/dict"):
        _sub(correlation={"items": [1, 2, {"bad": object()}]})


def test_correlation_rejects_a_non_string_key_at_top_level() -> None:
    with pytest.raises(ValueError, match="non-string key"):
        _sub(correlation={1: "value"})  # type: ignore[dict-item]


def test_correlation_rejects_a_non_string_key_nested() -> None:
    with pytest.raises(ValueError, match="non-string key"):
        _sub(correlation={"outer": {2: "value"}})


def test_correlation_rejects_tuple_and_set_and_bytes() -> None:
    # Not JSON scalars/list/dict, and they round-trip through asdict()/json differently
    # (or not at all) — exactly the shapes decision 4 excludes on purpose.
    with pytest.raises(ValueError, match="not a JSON scalar/list/dict"):
        _sub(correlation={"t": (1, 2)})
    with pytest.raises(ValueError, match="not a JSON scalar/list/dict"):
        _sub(correlation={"s": {1, 2}})
    with pytest.raises(ValueError, match="not a JSON scalar/list/dict"):
        _sub(correlation={"b": b"raw"})


# ── round-trip parity (Parity enforcement section) ─────────────────────────


def _round_trip(sub: Submission) -> Submission:
    dumped = dataclasses.asdict(sub)
    return Submission(**dumped)


@pytest.mark.parametrize("source", get_args(SubmissionSource))
def test_round_trip_every_source(source: SubmissionSource) -> None:
    sub = _sub(source=source, submitter=f"probe-{source}")
    assert _round_trip(sub) == sub


@pytest.mark.parametrize("strategy", get_args(MultitaskStrategy))
def test_round_trip_every_multitask_strategy(strategy: MultitaskStrategy) -> None:
    sub = _sub(multitask_strategy=strategy)
    assert _round_trip(sub) == sub


def test_round_trip_with_images_and_rich_correlation() -> None:
    sub = _sub(
        images=[{"type": "image", "data": "base64==", "mime_type": "image/png"}],
        expand_commands=True,
        allow_user_input=True,
        store_history=False,
        depth=3,
        correlation={
            "bus_subject": "agent.turn.request",
            "binding_id": "b-42",
            "retries": [1, 2, 3],
            "meta": {"ok": True, "score": 0.5, "note": None},
        },
    )
    rebuilt = _round_trip(sub)
    assert rebuilt == sub
    # The dump is a plain, independent structure — mutating it must not alias the original,
    # or a caller holding the dict could corrupt a Submission already in flight.
    dumped = dataclasses.asdict(sub)
    dumped["correlation"]["meta"]["ok"] = False
    assert sub.correlation["meta"]["ok"] is True


def test_round_trip_silent_submission() -> None:
    # silent=True normalizes store_history to False in __post_init__; the ROUND-TRIPPED
    # object must land on the same normalized state, not re-derive something different.
    sub = _sub(silent=True, store_history=True)
    assert sub.store_history is False
    rebuilt = _round_trip(sub)
    assert rebuilt == sub
    assert rebuilt.store_history is False


def test_round_trip_minimal_defaults() -> None:
    sub = _sub()
    assert _round_trip(sub) == sub


# ── SubmissionResult: the typed refusal, and its own round trip ───────────


def test_submission_result_accepted() -> None:
    result = SubmissionResult(accepted=True, submission_id="sid-1", messages=[{"role": "user"}])
    assert result.accepted is True
    assert result.rejection_reason is None
    assert result.messages == [{"role": "user"}]


def test_submission_result_rejection_is_a_value_not_an_exception() -> None:
    # decision-adjacent to "Five mechanisms" point 5 (LSP ApplyWorkspaceEditResult): a refusal
    # is constructed normally, never raised.
    result = SubmissionResult(
        accepted=False, submission_id="sid-2", rejection_reason="a turn is already in flight"
    )
    assert result.accepted is False
    assert result.rejection_reason == "a turn is already in flight"
    assert result.messages == []


def test_submission_result_round_trip() -> None:
    result = SubmissionResult(
        accepted=False,
        submission_id="sid-3",
        rejection_reason="depth exceeded",
        messages=[{"role": "assistant", "content": "no"}],
    )
    dumped = dataclasses.asdict(result)
    rebuilt = SubmissionResult(**dumped)
    assert rebuilt == result
