"""The two notices a turn can leave behind — docs/REPL-HEAD.md §4.

An answer cut at the output cap, and a prompt cache that read nothing. Both are
read by the pure readers print mode reads them with, and both are printed into
the scrollback here because a REPL's scrollback IS its transcript: ``tau -p``
writes stderr only because its stdout is one.

The advice sentence each notice ends with is now one string in ``headless.py``
(:data:`TRUNCATION_ADVICE`, :func:`cache_dialect_advice`), so print mode and this
head cannot drift into two wordings of the same fix.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.headless import (
    TRUNCATION_ADVICE,
    cache_dialect_advice,
    report_cache_miss,
    report_truncation,
)

from repl_fakes import FakeBackend, ReplEnv, env  # noqa: F401


def _assistant(stop_reason: str, dropped: int = 0) -> dict[str, Any]:
    """One completion as the loop persists it, with its stop reason and drops."""
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "half an ans"}],
        "stop_reason": stop_reason,
        "usage": {"extra": {"dropped_partial_tool_calls": dropped}} if dropped else {},
    }


def _turn(*messages: dict[str, Any]) -> Any:
    """A preparer whose ``submit_turn`` answers with *messages*."""

    def prepare(backend: FakeBackend) -> None:
        backend.turn_result = SubmissionResult(
            accepted=True, submission_id="s", messages=list(messages)
        )

    return prepare


def _unwrapped(text: str) -> str:
    """The console's text with its wrapping undone, so a sentence can be matched."""
    return " ".join(text.split())


async def test_a_completion_cut_at_the_cap_says_so_and_names_the_cap(env: ReplEnv) -> None:
    """The reader wants to know which number to raise, so the notice quotes the cap
    this run actually sent rather than the default it might not be using."""
    env.config["models"]["local-llm"]["max_tokens"] = 512
    env.install(_turn(_assistant("length", dropped=2)))
    await env.run(["write the migration"])
    text = _unwrapped(env.text)
    assert "stopped at the output cap (stop_reason: length, max_tokens = 512)" in text
    assert "2" in text
    assert _unwrapped(TRUNCATION_ADVICE) in text


async def test_the_notice_is_printed_once_for_the_turn_that_earned_it(env: ReplEnv) -> None:
    """One turn, one reading: the notice rides on ``submit_turn``'s messages, not on
    a render event that fires twice per completion (§4's ``completion_end`` row)."""
    env.install(_turn(_assistant("length")))
    await env.run(["write the migration"])
    assert _unwrapped(env.text).count("stopped at the output cap") == 1


async def test_an_aborted_turn_earns_no_truncation_notice(env: ReplEnv) -> None:
    """An Esc is not a cap: ``truncation.py`` reports ``Truncation(0, 0)`` for an
    abort, and this notice exists to tell an operator to raise a cap."""
    env.install(_turn(_assistant("aborted", dropped=3)))
    await env.run(["write the migration"])
    assert "output cap" not in env.text


async def test_a_finished_turn_says_nothing_at_all(env: ReplEnv) -> None:
    """The quiet path: nothing was cut, so nothing is said."""
    env.install(_turn(_assistant("stop")))
    await env.run(["hello"])
    assert "output cap" not in env.text


async def test_a_cache_notice_rides_the_lane_end_and_carries_the_dialect_advice(
    env: ReplEnv,
) -> None:
    """The router's observer is the one with the latch, so this head reads its
    verdict off ``lane_end`` rather than building a second observer per turn."""

    def prepare(backend: FakeBackend) -> None:
        backend.script = [
            {"kind": "lane_start", "source": "interactive", "submitter": "human", "text": "hi"},
            {"kind": "lane_end", "context": 9000, "output": 20, "cache_notice": "read 0 tokens"},
        ]

    env.install(prepare)
    await env.run(["hi"])
    text = _unwrapped(env.text)
    assert "read 0 tokens" in text
    assert _unwrapped(cache_dialect_advice("local-llm")) in text


async def test_the_same_notice_is_said_once_per_model_per_session(env: ReplEnv) -> None:
    """A display policy, and this head's alone: the reading is per turn, and a
    reader told twice about one misconfiguration learns nothing the second time."""

    def prepare(backend: FakeBackend) -> None:
        backend.script = [
            {"kind": "lane_end", "context": 9000, "output": 20, "cache_notice": "read 0 tokens"}
        ]

    env.install(prepare)
    await env.run(["hi", "again"])
    assert _unwrapped(env.text).count("read 0 tokens") == 1


def test_print_mode_gives_the_same_advice_as_the_prompt_line(capsys: Any) -> None:
    """The extraction's whole point: two heads, one sentence about one fix."""
    assert report_truncation([_assistant("length")], 512) is not None
    assert report_cache_miss([], "local-llm") is None
    err = capsys.readouterr().err
    assert TRUNCATION_ADVICE in _unwrapped(err)
