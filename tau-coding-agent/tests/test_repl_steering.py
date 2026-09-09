"""Typing while the model generates — docs/TUI-STEERING.md §2, docs/REPL-HEAD.md §5.

A line typed at a REPL prompt during a turn is steering, not a second turn, and
the head holds it: the delivery point is the configured strategy's, the whole
buffer goes as ONE message, and a command is refused rather than queued.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tau_agent_core.submission import SubmissionResult
from tau_coding_agent import app
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.repl import run_repl
from tau_coding_agent.repl_input import MemoryReader
from tau_coding_agent.config import ConfigError
from tau_coding_agent.steering import (
    DEFAULT_STEERING_STRATEGY,
    STEERING_CONFIG_KEY,
    STEERING_STRATEGIES,
    SteeringBuffer,
    configured_steering_strategy,
    steering_note,
)

from repl_fakes import FakeBackend, ReplEnv, env  # noqa: F401

#: A turn that says something, calls a tool and ends — one delivery point in it.
TURN_WITH_A_TOOL: list[dict[str, Any]] = [
    {"kind": "lane_start", "source": "interactive", "submitter": "human", "text": "do it"},
    {"kind": "text_delta", "delta": "Looking.\n"},
    {"kind": "tool_call", "id": "t1", "name": "read", "arguments": {"path": "main.py"}},
    {"kind": "tool_result", "id": "t1", "name": "read", "result": "ok", "is_error": False},
    {"kind": "completion_end", "output": 12, "context": 30, "stop_reason": "stop"},
    {"kind": "lane_end", "context": 30, "output": 12, "seconds": 0.4},
]


#: The same turn with no tool call in it — no mid-turn delivery point at all.
TOOLLESS_TURN: list[dict[str, Any]] = [
    event for event in TURN_WITH_A_TOOL if not event["kind"].startswith("tool_")
]


def _arm(script: list[dict[str, Any]]) -> Any:
    """A ``create_backend`` preparer that gives the double a render script."""

    def prepare(backend: FakeBackend) -> None:
        backend.script = list(script)

    return prepare


def test_the_strategy_tables_have_not_drifted_from_the_tui() -> None:
    """``steering.py`` holds the values app.py holds; app.py imports Textual, so
    a head that must not cannot share the constant — only assert it is the same."""
    assert STEERING_CONFIG_KEY == app.STEERING_CONFIG_KEY
    assert STEERING_STRATEGIES == app.STEERING_STRATEGIES
    assert DEFAULT_STEERING_STRATEGY == app.DEFAULT_STEERING_STRATEGY


def test_an_unknown_strategy_raises_with_the_tui_wording() -> None:
    """Fail-Early: a typo that silently selected the other strategy would be
    invisible until a steering message did not land where it was aimed."""
    with pytest.raises(ConfigError) as excinfo:
        configured_steering_strategy({STEERING_CONFIG_KEY: "steal"})
    assert "'steering_strategy' = 'steal' is not a steering strategy" in str(excinfo.value)
    assert "Use one of enqueue, steer" in str(excinfo.value)


async def test_an_unknown_strategy_is_refused_before_anything_is_built(env: ReplEnv) -> None:
    """At startup, not at the first mid-turn line: no backend, no session, no turn."""
    env.config[STEERING_CONFIG_KEY] = "steal"
    with pytest.raises(ConfigError):
        await env.run(["hello"])
    assert env.backend is None


def test_the_buffer_joins_everything_it_holds_into_one_message() -> None:
    """"All at once, and only all at once": a second line joins the first."""
    buffer = SteeringBuffer()
    buffer.hold("first")
    buffer.hold("second")
    assert buffer.take() == "first\n\nsecond"
    assert buffer.take() is None


def test_reclaim_hands_back_delivered_text_before_pending_text() -> None:
    """Oldest first: a delivery the core has not woven in was typed before
    anything still pending, and reclaimed text goes in front of a draft."""
    buffer = SteeringBuffer()
    buffer.await_weave("aimed at the running turn")
    buffer.hold("typed after it")
    assert buffer.reclaim() == "aimed at the running turn\n\ntyped after it"
    assert buffer.reclaim() is None


def test_a_confirmed_delivery_is_no_longer_reclaimable() -> None:
    """``steer_message`` is the core's proof that the weave happened."""
    buffer = SteeringBuffer()
    buffer.await_weave("one")
    buffer.confirm()
    assert buffer.reclaim() is None


def test_the_note_names_the_strategys_own_delivery_point() -> None:
    """Both strategies can deliver at the turn edge, so the promise is the
    strategy's own point and the delivery reports what happened."""
    assert steering_note("steer") == "steering, at this turn's next tool call"
    assert steering_note("enqueue") == "waiting for this turn to end"


async def test_a_mid_turn_line_is_delivered_at_the_tool_call(env: ReplEnv) -> None:
    """The default strategy: the running turn takes the message before its next
    call to the model, as a ``multitask_strategy="steer"`` submission."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await env.run(["do it", "use ripgrep instead"])
    assert [sub.multitask_strategy for sub in env.submissions] == ["enqueue", "steer"]
    steer = env.submissions[1]
    assert steer.text == "use ripgrep instead"
    assert steer.expand_commands is False
    assert steer.allow_user_input is True
    assert "⏳ use ripgrep instead" in env.text
    assert "steering, at this turn's next tool call" in env.text


async def test_two_mid_turn_lines_arrive_as_one_message(env: ReplEnv) -> None:
    """One buffer, joined with a blank line: the model is asked once, not twice.

    A turn with no tool call in it, so both lines are certainly still held when
    the delivery point arrives — the turn edge, which is where ``"steer"`` also
    delivers when the turn made no further call to the model.
    """
    env.install(_arm(TOOLLESS_TURN))
    await env.run(["do it", "wait", "use ripgrep"])
    assert len(env.submissions) == 2
    assert env.submissions[1].text == "wait\n\nuse ripgrep"
    assert env.submissions[1].multitask_strategy == "enqueue"


async def test_enqueue_delivers_at_the_turn_edge_as_its_own_turn(env: ReplEnv) -> None:
    """The other strategy: the tool call is not a delivery point, the turn's end is."""
    env.config[STEERING_CONFIG_KEY] = "enqueue"
    env.install(_arm(TURN_WITH_A_TOOL))
    await env.run(["do it", "and then deploy"])
    assert [sub.multitask_strategy for sub in env.submissions] == ["enqueue", "enqueue"]
    assert env.submissions[1].text == "and then deploy"
    assert env.submissions[1].expand_commands is False
    assert "waiting for this turn to end" in env.text


async def test_a_command_typed_mid_turn_is_refused_and_left_at_the_prompt(env: ReplEnv) -> None:
    """``/compact`` and ``/fork`` rewrite the context the running turn is being
    answered from, so a command is refused — never held, never dispatched."""
    env.install(_arm(TURN_WITH_A_TOOL))
    await env.run(["do it", "/compact"])
    assert env.backend is not None
    assert env.backend.commands == []
    assert len(env.submissions) == 1
    assert "/compact runs between turns. Press Enter again when this one finishes." in env.text
    assert env.reader.drafts == ["/compact"]


async def test_a_refused_steer_comes_back_to_the_prompt(env: ReplEnv) -> None:
    """A refusal is reported and the text is handed back: the user typed it, and
    re-delivering it at the next tool call would refuse it again forever."""

    def prepare(backend: FakeBackend) -> None:
        backend.script = list(TURN_WITH_A_TOOL)
        backend.steer_result = SubmissionResult(
            accepted=False, submission_id="s", rejection_reason="an extension holds the session"
        )

    env.install(prepare)
    await env.run(["do it", "steer this"])
    assert "an extension holds the session" in env.text
    assert env.reader.drafts == ["steer this"]


class _GatedReader(MemoryReader):
    """A reader whose reads after the first park until the test releases them.

    The suite's reader answers instantly, which cannot express the case that
    matters here: a read OUTSTANDING while a turn runs, cancelled by a question
    the turn raised and re-issued when it is answered.
    """

    def __init__(self, lines: list[str], answers: list[str] | None = None) -> None:
        super().__init__(lines, answers)
        self.gate = asyncio.Event()
        self._reads = 0

    async def read(self, *, default: str = "") -> str | None:
        self._reads += 1
        if self._reads > 1:
            await self.gate.wait()
        return await super().read(default=default)

    async def ask(self, question: str, **kwargs: Any) -> str | None:
        # A real question takes a person some time; the loop re-enters its wait meanwhile.
        for _ in range(3):
            await asyncio.sleep(0)
        return await super().ask(question, **kwargs)


async def test_a_line_typed_after_a_mid_turn_form_still_steers(env: ReplEnv) -> None:
    """``_ask_alone`` re-issues the read from INSIDE the turn task, so the loop
    must be told: a loop waiting on the turn alone sees nothing typed after a
    form until the turn ends, and the line becomes a whole second turn — no echo,
    no note, and the delivery point it was aimed at already passed."""
    reader = _GatedReader(["do it", "use ripgrep instead"], answers=["yes"])

    def prepare(backend: FakeBackend) -> None:
        backend.script = list(TURN_WITH_A_TOOL)
        original = backend.submit_turn

        async def submit_turn(submission: Any, context: Any) -> SubmissionResult:
            if submission.multitask_strategy != "steer" and backend.delegate is not None:
                delegate, backend.delegate = backend.delegate, None
                await delegate.form({"fields": [{"name": "sure", "kind": "text"}]})
                reader.gate.set()
                for _ in range(6):
                    await asyncio.sleep(0)
            return await original(submission, context)

        backend.submit_turn = submit_turn  # type: ignore[method-assign]

    env.install(prepare)
    env.reader = reader
    await run_repl(CLIArgs(mode="repl"), env.config, reader=reader, console=env.console)

    assert [sub.multitask_strategy for sub in env.submissions] == ["enqueue", "steer"]
    assert env.submissions[1].text == "use ripgrep instead"
    assert "⏳ use ripgrep instead" in env.text
