"""Tests for repeat-tool-call detection and ``agent_end.end_reason``.

Two things built together, because one needs the other to be observable.

**Repeat detection** (docs/PLAN-0.9.3.md §4.2 item 2) bounds a model that calls
the same failing tools forever. That bound used to exist by accident: ``max_turns``
defaulted to 50, so a runaway stopped when 50 arrived. 0.9.4 made ``max_turns``
default to ``None``, which removed the accident and left nothing in its place.

**``end_reason``** is how a caller learns which of those happened. Before it,
a loop stopped by a ceiling emitted the same ``agent_end`` as one where the model
had finished — so a truncated answer and a complete one were the same event.

Reference: docs/PLAN-0.9.3.md §4.2 item 2
Reference: docs/PLAN-0.9.4.md §8 ("No repeat-tool-call detection in agent_loop.py")
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.abort import AbortSignal
from tau_llm.streaming import DoneEvent, ErrorEvent
from tau_llm.types import (
    AssistantMessage,
    TextContent,
    ToolCall as TauToolCall,
    Usage,
    UserMessage,
)

from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.events import AgentEvent
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def async_emit(events: list, e: AgentEvent) -> None:
    events.append(e)


def _user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=int(time.time() * 1000))


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
        usage=Usage(),
    )


def _calls_assistant(calls: list[tuple[str, str, dict[str, Any]]]) -> AssistantMessage:
    """An assistant message asking for ``(id, name, arguments)`` tool calls."""
    return AssistantMessage(
        content=[
            TauToolCall(type="toolCall", id=cid, name=name, arguments=args)
            for cid, name, args in calls
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=int(time.time() * 1000),
        usage=Usage(),
    )


def _failing_tool(name: str) -> AgentTool:
    async def failing_execute(**kw):
        raise Exception(f"{name} failed")

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name.capitalize(),
            description=f"The {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=failing_execute,
            execution_mode="parallel",
        )
    )


def _ok_tool(name: str) -> AgentTool:
    async def ok_execute(**kw):
        return AgentToolResult(
            tool_name=name,
            content=[{"type": "text", "text": "ok"}],
        )

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name.capitalize(),
            description=f"The {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=ok_execute,
            execution_mode="parallel",
        )
    )


def _terminating_tool(name: str) -> AgentTool:
    async def terminate_execute(**kw):
        return AgentToolResult(
            tool_name=name,
            content=[{"type": "text", "text": "stopping"}],
            terminate=True,
        )

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name.capitalize(),
            description=f"The {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=terminate_execute,
            execution_mode="parallel",
        )
    )


class _MockStream:
    def __init__(self, events: list):
        self._events = events

    def __aiter__(self):
        return _Iter(self._events)

    async def result(self):
        for e in self._events:
            if isinstance(e, DoneEvent):
                return e.final
        return None


class _Iter:
    def __init__(self, events: list):
        self._events = events
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        e = self._events[self._i]
        self._i += 1
        return e


def _scripted(replies: list[AssistantMessage], call_count: list[int]):
    """A ``stream_simple`` stand-in that answers with *replies*, in order.

    Runs off the END of the script rather than repeating the last reply, so a
    loop that takes one turn more than the test expects fails loudly here instead
    of spinning.
    """

    async def mock_stream_func(model, context, options):
        i = call_count[0]
        call_count[0] += 1
        if i >= len(replies):
            raise AssertionError(
                f"the loop asked for LLM call {i + 1}; the script has {len(replies)}"
            )
        return _MockStream([DoneEvent(final=replies[i], usage=Usage())])

    return mock_stream_func


def _end_reason(events: list[AgentEvent]) -> str | None:
    ends = [e for e in events if e.type == "agent_end"]
    assert len(ends) == 1, f"expected exactly one agent_end, got {len(ends)}"
    return ends[0].end_reason


# ---------------------------------------------------------------------------
# Repeat detection
# ---------------------------------------------------------------------------


class TestRepeatToolCallDetection:
    async def test_three_identical_all_error_batches_stop_the_loop(self):
        """The documented case: the same failing call, and nothing else, at the
        shipped default of 3.

        This is the AskSage shape — turns 2..N being the identical failure. The
        script holds exactly three replies, so a loop that did not stop would
        fail on the fourth call rather than run away.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {"x": 1})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply] * 3, calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_two_repeats_still_leave_the_model_a_turn(self):
        """The reason the default is 3 and not 2.

        A ``tool_result`` hook that reacts to repeated failures can only fire ON
        the second failure, and exists to change what the model sees before the
        third attempt. Stopping at 2 would end the run in the same turn the
        guidance was appended, so the guidance would be written and never read.
        Here the model uses that third turn to answer.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {"x": 1})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply, reply, _text_assistant("I see the problem")], calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "done"

    async def test_a_single_success_in_the_batch_is_progress(self):
        """One non-error result means the turn did something, so no stop.

        The model repeats an identical batch three times; one of the two calls in
        it succeeds every time. The loop must keep going, and here it ends only
        because the script's last reply is plain text.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom"), _ok_tool("fine")],
        )

        reply = _calls_assistant([("c1", "boom", {}), ("c2", "fine", {})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply, reply, reply, _text_assistant("done")], calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 4
        assert _end_reason(events) == "done"

    async def test_different_arguments_are_not_a_repeat(self):
        """Same tool, different arguments — the model is trying something else."""
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        calls = [0]
        script = [
            _calls_assistant([("c1", "boom", {"path": "a"})]),
            _calls_assistant([("c2", "boom", {"path": "b"})]),
            _calls_assistant([("c3", "boom", {"path": "c"})]),
            _text_assistant("giving up"),
        ]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 4
        assert _end_reason(events) == "done"

    async def test_the_call_id_is_not_part_of_the_signature(self):
        """A repeat is the same WORK, not the same ids.

        Providers mint a fresh ``tool_call_id`` every turn, so comparing ids would
        make every batch unique and the check would never fire.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        calls = [0]
        script = [
            _calls_assistant([("call_aaa", "boom", {"x": 1})]),
            _calls_assistant([("call_bbb", "boom", {"x": 1})]),
            _calls_assistant([("call_ccc", "boom", {"x": 1})]),
        ]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_argument_key_order_does_not_matter(self):
        """``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` ask for the same work.

        Python preserves insertion order in a dict, so without ``sort_keys`` these
        two encode differently and a real repeat would be missed.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        calls = [0]
        script = [
            _calls_assistant([("c1", "boom", {"a": 1, "b": 2})]),
            _calls_assistant([("c2", "boom", {"b": 2, "a": 1})]),
            _calls_assistant([("c3", "boom", {"a": 1, "b": 2})]),
        ]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_call_order_within_a_batch_does_not_matter(self):
        """The same two calls in the other order is still the same batch."""
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom"), _failing_tool("bang")],
        )

        calls = [0]
        script = [
            _calls_assistant([("c1", "boom", {}), ("c2", "bang", {})]),
            _calls_assistant([("c3", "bang", {}), ("c4", "boom", {})]),
            _calls_assistant([("c5", "boom", {}), ("c6", "bang", {})]),
        ]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_a_higher_limit_tolerates_more_repeats(self):
        """``repeat_tool_call_limit=4`` stops on the fourth identical batch."""
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o", repeat_tool_call_limit=4),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply] * 4, calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 4
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_none_disables_the_check(self):
        """``repeat_tool_call_limit=None`` is pi's behaviour: nothing stops it here.

        ``max_turns`` is set so the test terminates, and the reason it reports is
        the ceiling — proof the repeat check did not fire.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o", repeat_tool_call_limit=None, max_turns=5),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply] * 5, calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 5
        assert _end_reason(events) == "max_turns"

    async def test_a_limit_below_two_is_rejected(self):
        """A limit of 1 would stop on a batch that has repeated nothing yet."""
        with pytest.raises(Exception):
            AgentLoopConfig(model="gpt-4o", repeat_tool_call_limit=1)

    async def test_the_repeated_turn_is_still_emitted_and_returned(self):
        """Stopping is a decision about the NEXT turn, not a reason to hide this one.

        The turn that trips the check has already run its tools; its ``turn_end``
        and its messages must reach the caller like any other turn's.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply] * 3, calls),
        ):
            final = await loop.run(prompts=[_user("go")])

        turn_ends = [e for e in events if e.type == "turn_end"]
        assert len(turn_ends) == 3
        # Every turn's tool results are present, and all of them say they failed.
        assert all(tr["is_error"] for e in turn_ends for tr in (e.tool_results or []))
        # Three assistant messages and three tool-result messages came back.
        assert len(final) == 6


# ---------------------------------------------------------------------------
# end_reason, for every way the loop can stop
# ---------------------------------------------------------------------------


class TestEndReason:
    async def test_done_when_the_model_has_nothing_more_to_say(self):
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
        )
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([_text_assistant("hello")], calls),
        ):
            await loop.run(prompts=[_user("hi")])

        assert _end_reason(events) == "done"

    async def test_terminate_when_a_tool_asks_to_stop(self):
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_terminating_tool("halt")],
        )
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([_calls_assistant([("c1", "halt", {})])], calls),
        ):
            await loop.run(prompts=[_user("go")])

        assert _end_reason(events) == "terminate"

    async def test_max_turns_is_no_longer_silent(self):
        """The sibling debt: a stated ceiling used to be reached with no signal.

        ``max_turns=2`` truncates a model that would have kept going, and the
        ``agent_end`` now says so instead of looking like a finished run.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o", max_turns=2),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )
        calls = [0]
        script = [
            _calls_assistant([("c1", "boom", {"x": 1})]),
            _calls_assistant([("c2", "boom", {"x": 2})]),
        ]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert calls[0] == 2
        assert _end_reason(events) == "max_turns"

    async def test_aborted_when_the_signal_is_raised_between_turns(self):
        events: list[AgentEvent] = []
        signal = AbortSignal()
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_ok_tool("fine")],
            abort_signal=signal,
        )

        calls = [0]

        async def mock_stream_func(model, context, options):
            calls[0] += 1
            # Abort as the first turn's response is produced, so the loop's
            # between-turns check is what sees it.
            signal.abort()
            return _MockStream(
                [DoneEvent(final=_calls_assistant([("c1", "fine", {})]), usage=Usage())]
            )

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func):
            await loop.run(prompts=[_user("go")])

        assert _end_reason(events) == "aborted"

    async def test_error_when_the_loop_raises(self):
        """``end_reason`` and ``is_error`` cannot disagree — both come from one check."""
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
        )

        async def mock_stream_func(model, context, options):
            return _MockStream([ErrorEvent(error="upstream exploded")])

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=mock_stream_func):
            with pytest.raises(BaseException):
                await loop.run(prompts=[_user("go")])

        ends = [e for e in events if e.type == "agent_end"]
        assert len(ends) == 1
        assert ends[0].end_reason == "error"
        assert ends[0].is_error is True
        assert ends[0].error is not None

    async def test_only_agent_end_carries_a_reason(self):
        """Every other event type leaves ``end_reason`` unset."""
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_ok_tool("fine")],
        )
        calls = [0]
        script = [_calls_assistant([("c1", "fine", {})]), _text_assistant("done")]
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_scripted(script, calls)):
            await loop.run(prompts=[_user("go")])

        assert [e.end_reason for e in events if e.type != "agent_end"] == [
            None for e in events if e.type != "agent_end"
        ]


# ---------------------------------------------------------------------------
# run_continue takes the same bound
# ---------------------------------------------------------------------------


class TestRunContinue:
    async def test_run_continue_stops_on_a_repeat_too(self):
        """A continuation is a turn like any other, so a runaway there is bounded.

        Without this, the check could be bypassed by whichever entry point the
        caller happened to use.
        """
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
            tools=[_failing_tool("boom")],
        )

        reply = _calls_assistant([("c1", "boom", {})])
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([reply] * 3, calls),
        ):
            await loop.run_continue(context=[_user("go")])

        assert calls[0] == 3
        assert _end_reason(events) == "repeat_tool_calls"

    async def test_run_continue_reports_done(self):
        events: list[AgentEvent] = []
        loop = AgentLoop(
            config=AgentLoopConfig(model="gpt-4o"),
            emit=lambda e: async_emit(events, e),
        )
        calls = [0]
        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_scripted([_text_assistant("ok")], calls),
        ):
            await loop.run_continue(context=[_user("go")])

        assert _end_reason(events) == "done"
