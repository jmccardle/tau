"""The head-agnostic reading of a completion the output cap cut off.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3. The TUI's box, print mode's stderr
lines and an RPC host's ``message_end`` fields all describe the same fact; this
is where that fact is decided, so all three agree by construction.
"""

from __future__ import annotations

from tau_agent_core.truncation import (
    Truncation,
    completion_truncation,
    dropped_tool_calls,
    truncation_from_messages,
    truncation_notice,
    unrun_tool_result_text,
    with_unrun_tool_calls_counted,
)


def _assistant(stop_reason: str | None, dropped: int | None = None) -> dict:
    usage: dict = {"total_tokens": 100, "output_tokens": 90}
    if dropped is not None:
        usage["extra"] = {"dropped_partial_tool_calls": dropped}
    message: dict = {"role": "assistant", "content": [], "usage": usage}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return message


class TestDroppedToolCalls:
    def test_the_count_is_read_off_usage_extra(self):
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": 2}}) == 2

    def test_a_completion_that_dropped_none_wrote_no_key(self):
        assert dropped_tool_calls({"extra": {}}) == 0
        assert dropped_tool_calls({}) == 0

    def test_a_non_integer_count_reads_as_none_dropped(self):
        """A count τ did not write is not evidence of a loss."""
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": "two"}}) == 0
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": True}}) == 0
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": -1}}) == 0


class TestOneCompletion:
    def test_a_length_stop_is_one_truncated_completion(self):
        assert completion_truncation(_assistant("length")) == Truncation(1, 0)

    def test_it_carries_the_calls_that_completion_lost(self):
        assert completion_truncation(_assistant("length", 3)) == Truncation(1, 3)

    def test_every_other_stop_reason_is_nothing(self):
        for reason in ("stop", "toolUse", "error", None):
            assert completion_truncation(_assistant(reason)) == Truncation(0, 0)

    def test_an_aborted_completions_drops_are_not_the_caps_doing(self):
        """An abort drops calls too. This function says what the CAP did, and an
        Esc the user pressed is not a cap to raise."""
        assert completion_truncation(_assistant("aborted", 2)) == Truncation(0, 0)

    def test_a_message_with_no_usage_still_reports_the_stop(self):
        assert completion_truncation({"role": "assistant", "stop_reason": "length"}) == Truncation(
            1, 0
        )


class TestAWholeTurn:
    def test_two_truncated_completions_are_two_losses(self):
        """Unlike a prompt size, these SUM: each cut is its own missing answer."""
        messages = [_assistant("length", 1), _assistant("toolUse"), _assistant("length", 2)]
        assert truncation_from_messages(messages) == Truncation(2, 3)

    def test_a_clean_turn_is_nothing(self):
        assert truncation_from_messages([_assistant("stop")]) == Truncation(0, 0)
        assert truncation_from_messages([]) == Truncation(0, 0)

    def test_a_user_message_is_not_read(self):
        messages = [{"role": "user", "content": "hi", "stop_reason": "length"}]
        assert truncation_from_messages(messages) == Truncation(0, 0)


class TestTheNotice:
    def test_nothing_truncated_says_nothing(self):
        assert truncation_notice(Truncation(0, 0), max_tokens=4096) is None

    def test_it_quotes_the_cap_it_was_given(self):
        notice = truncation_notice(Truncation(1, 0), max_tokens=900)
        assert notice is not None
        assert "max_tokens = 900" in notice
        assert "stop_reason: length" in notice

    def test_an_unknown_cap_says_unknown_rather_than_a_default(self):
        """Fail Early: quoting 4096 at an operator whose real cap is something
        else sends them to change a number that was already right."""
        notice = truncation_notice(Truncation(1, 0), max_tokens=None)
        assert notice is not None
        assert "max_tokens = unknown" in notice

    def test_a_dropped_call_is_named_and_counted(self):
        notice = truncation_notice(Truncation(1, 1), max_tokens=4096)
        assert notice is not None
        assert "1 tool call was not run" in notice

    def test_several_dropped_calls_read_as_plural(self):
        notice = truncation_notice(Truncation(2, 3), max_tokens=4096)
        assert notice is not None
        assert "2 of this turn's completions" in notice
        assert "3 tool calls were not run" in notice

    def test_no_dropped_call_adds_no_second_sentence(self):
        notice = truncation_notice(Truncation(1, 0), max_tokens=4096)
        assert notice is not None
        assert "not run" not in notice


def _message(stop_reason: str, *blocks, extra: dict | None = None):
    from tau_llm.types import AssistantMessage, Usage

    return AssistantMessage(
        content=list(blocks),
        api="anthropic-messages",
        provider="anthropic",
        model="m",
        stop_reason=stop_reason,
        usage=Usage(extra=extra or {}),
    )


class TestWithUnrunToolCallsCounted:
    """docs/TRUNCATED-TOOL-CALLS.md §3.2: a cut-off completion keeps its calls, counted."""

    def test_the_calls_stay_and_are_counted(self):
        from tau_llm.types import TextContent, ToolCall

        text = TextContent(text="Writing the file now.")
        read = ToolCall(id="a", name="read", arguments={"path": "x"})
        write = ToolCall(id="b", name="write", arguments={"path": "y", "content": "# T"})
        msg = _message("length", text, read, write)
        out = with_unrun_tool_calls_counted(msg)
        assert out.content == [text, read, write]
        assert out.usage.extra["dropped_partial_tool_calls"] == 2
        assert "dropped_partial_tool_calls" not in msg.usage.extra, "input not mutated"

    def test_a_provider_count_is_added_to(self):
        from tau_llm.types import ToolCall

        msg = _message(
            "length",
            ToolCall(id="a", name="write", arguments={"path": "y"}),
            extra={"dropped_partial_tool_calls": 1},
        )
        assert with_unrun_tool_calls_counted(msg).usage.extra["dropped_partial_tool_calls"] == 2

    def test_other_stop_reasons_and_call_free_messages_are_unchanged(self):
        from tau_llm.types import TextContent, ToolCall

        complete = _message("toolUse", ToolCall(id="a", name="ls", arguments={}))
        assert with_unrun_tool_calls_counted(complete) is complete
        prose = _message("length", TextContent(text="cut"))
        assert with_unrun_tool_calls_counted(prose) is prose


class TestUnrunToolResultText:
    """What the model reads in place of a result, so it can resume in smaller pieces."""

    def test_the_cut_call_names_the_cap_and_its_last_string_argument(self):
        from tau_llm.types import ToolCall

        write = ToolCall(id="w", name="write", arguments={"path": "a.md", "content": "x" * 14203})
        text = unrun_tool_result_text(_message("length", write), write, max_tokens=4096)
        assert text == (
            "Not run: your output reached max_tokens = 4096 while writing this call's "
            "arguments, so they are cut off (content holds 14,203 characters so far) and "
            "nothing was executed. Write it in smaller pieces."
        )

    def test_a_nested_last_string_is_named_by_its_path(self):
        from tau_llm.types import ToolCall

        edit = ToolCall(
            id="e", name="edit", arguments={"path": "a", "edits": [{"old": "x", "new": "yy"}]}
        )
        text = unrun_tool_result_text(_message("length", edit), edit, max_tokens=10)
        assert "(edits[0].new holds 2 characters so far)" in text

    def test_a_last_argument_that_is_not_a_string_is_not_named(self):
        from tau_llm.types import ToolCall

        read = ToolCall(id="r", name="read", arguments={"path": "a", "limit": 12})
        text = unrun_tool_result_text(_message("length", read), read, max_tokens=10)
        assert "so far" not in text
        assert "cut off and nothing was executed" in text

    def test_an_earlier_call_is_told_it_was_complete(self):
        from tau_llm.types import ToolCall

        read = ToolCall(id="r", name="read", arguments={"path": "a"})
        write = ToolCall(id="w", name="write", arguments={"path": "b", "content": "c"})
        text = unrun_tool_result_text(_message("length", read, write), read, max_tokens=None)
        assert text.startswith("Not run: your output reached max_tokens = unknown later")
        assert "arguments are complete" in text


class TestTheLoopEndsTheTurn:
    """A cut-off call never executes: it is recorded, answered, and the turn ends."""

    async def test_the_call_is_kept_answered_and_no_second_request_is_made(self):
        from unittest.mock import patch

        from tau_llm.streaming import DoneEvent
        from tau_llm.types import Model, TextContent, ToolCall
        from tau_agent_core.agent_session import AgentSession
        from tau_agent_core.session_log import InMemorySessionLog
        from tau_agent_core.tools.base import AgentTool, ToolDefinition

        ran: list[dict] = []
        requests: list[int] = []
        tool = AgentTool(
            definition=ToolDefinition(
                name="write",
                label="Write",
                description="Write a file",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=lambda ctx: ran.append(ctx) or "ok",
            )
        )
        draft = "# Fruit\n1. Apples"

        class _Stream:
            def __aiter__(self):
                async def _gen():
                    final = _message(
                        "length",
                        TextContent(text="Here is the file."),
                        ToolCall(
                            id="w", name="write", arguments={"path": "docs/x.md", "content": draft}
                        ),
                    )
                    yield DoneEvent(final=final, usage=final.usage)

                return _gen()

            async def result(self):
                return None

        async def _stream_simple(model, context, options=None):
            requests.append(1)
            return _Stream()

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=Model(
                id="m",
                name="m",
                api="anthropic-messages",
                provider="anthropic",
                base_url="https://example.invalid",
                context_window=128000,
                max_tokens=64,
            ),
            tools=[tool],
        )
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            new = await session.prompt("write docs/x.md")

        assert ran == []
        assert len(requests) == 1
        assert [_role(m) for m in new] == ["user", "assistant", "toolResult"]
        assistant = _dump(new[1])
        assert [b["type"] for b in assistant["content"]] == ["text", "toolCall"]
        assert assistant["content"][1]["arguments"] == {"path": "docs/x.md", "content": draft}
        assert assistant["usage"]["extra"]["dropped_partial_tool_calls"] == 1
        result = _dump(new[2])
        assert result["tool_call_id"] == "w"
        assert result["is_error"] is True
        assert result["content"][0]["text"] == (
            "Not run: your output reached max_tokens = 64 while writing this call's "
            "arguments, so they are cut off (content holds 17 characters so far) and "
            "nothing was executed. Write it in smaller pieces."
        )
        persisted = [_role(m) for m in session.messages]
        assert persisted[-2:] == ["assistant", "toolResult"]

    async def test_run_continue_refuses_every_call_and_ends_too(self):
        from unittest.mock import patch

        from tau_llm.streaming import DoneEvent
        from tau_llm.types import ToolCall, UserMessage
        from tau_agent_core.agent_loop import AgentLoop
        from tau_agent_core.agent_loop_types import AgentLoopConfig
        from tau_agent_core.tools.base import AgentTool, ToolDefinition

        ran: list[dict] = []
        requests: list[int] = []
        tools = [
            AgentTool(
                definition=ToolDefinition(
                    name=name,
                    label=name,
                    description=name,
                    parameters={"type": "object", "properties": {}, "required": []},
                    execute=lambda ctx: ran.append(ctx) or "ok",
                )
            )
            for name in ("read", "write")
        ]
        final = _message(
            "length",
            ToolCall(id="r", name="read", arguments={"path": "a"}),
            ToolCall(id="w", name="write", arguments={"path": "b", "content": "abc"}),
        )

        async def _stream_simple(model, context, options=None):
            requests.append(1)

            async def _gen():
                yield DoneEvent(final=final, usage=final.usage)

            return _gen()

        loop = AgentLoop(config=AgentLoopConfig(model="gpt-4o"), tools=tools)
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            out = await loop.run_continue(context=[UserMessage(content="go", timestamp=0)])

        assert ran == []
        assert len(requests) == 1
        results = [_dump(m) for m in out if _role(m) == "toolResult"]
        assert [r["tool_call_id"] for r in results] == ["r", "w"]
        assert all(r["is_error"] for r in results)
        assert "arguments are complete" in results[0]["content"][0]["text"]
        assert "content holds 3 characters so far" in results[1]["content"][0]["text"]


def _dump(message) -> dict:
    return message if isinstance(message, dict) else message.model_dump()


def _role(message) -> str | None:
    return _dump(message).get("role")


class TestReasoningShare:
    """docs/REASONING-WIRE.md §7: how much of the cap went to reasoning (#23)."""

    def _thought(self, stop_reason: str, reasoning: int | None) -> dict:
        message = _assistant(stop_reason)
        if reasoning is not None:
            message["usage"]["reasoning_tokens"] = reasoning
        return message

    def test_a_reported_split_rides_the_reading(self):
        assert completion_truncation(self._thought("length", 70)) == Truncation(1, 0, 70)

    def test_no_reported_split_reads_none_not_zero(self):
        assert completion_truncation(self._thought("length", None)).reasoning_tokens is None

    def test_a_turn_sums_only_what_was_reported(self):
        turn = [self._thought("length", 70), self._thought("length", None)]
        assert truncation_from_messages(turn) == Truncation(2, 0, 70)

    def test_the_notice_quotes_it(self):
        notice = truncation_notice(Truncation(1, 0, 1500), max_tokens=2000) or ""
        assert "1,500 of those output tokens were reasoning" in notice

    def test_the_notice_says_nothing_of_a_split_it_was_not_given(self):
        notice = truncation_notice(Truncation(1, 0), max_tokens=2000) or ""
        assert "reasoning" not in notice
