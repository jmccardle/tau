"""What the REPL prints for one lane — docs/REPL-HEAD.md §4.

Driven through a REAL :class:`~tau_coding_agent.backends.RenderRouter`, the way
``test_render_router.py`` drives one, so the vocabulary under test is the router's
own and not a hand-copied guess at it. Two properties matter beyond the text: no
raw delta ever reaches the scrollback (each block is written once, as Markdown),
and a foreign lane is rendered DISTINGUISHABLY rather than dropped.
"""

from __future__ import annotations

import re

import pytest
from rich.console import Console

from tau_agent_core.events import AgentEvent
from tau_agent_core.submission import Submission
from tau_coding_agent.backends import RenderRouter, replay_render_events
from tau_coding_agent.repl import (
    BlockSplitter,
    ReplRenderer,
    _clip,
    _format_arguments,
    fold_lines,
    reasoning_tokens,
    split_markdown_blocks,
)
from tau_coding_agent.repl_input import MemoryReader

_TS = 1_700_000_000_000


def _renderer() -> tuple[ReplRenderer, Console, MemoryReader]:
    console = Console(record=True, width=100, force_terminal=False)
    reader = MemoryReader()
    return ReplRenderer(console, reader, model_name="local-llm"), console, reader


def _text_event(lane: str, text: str) -> AgentEvent:
    return AgentEvent(
        type="message_update",
        message={"role": "assistant", "content": [{"type": "text", "text": text}]},
        submission_id=lane,
        timestamp=_TS,
    )


def _tool_start(lane: str, name: str, args: dict) -> AgentEvent:
    return AgentEvent(
        type="tool_execution_start",
        tool_call_id="t1",
        tool_name=name,
        args=args,
        submission_id=lane,
        timestamp=_TS,
    )


def _tool_end(lane: str, name: str, result: str, **kwargs) -> AgentEvent:
    return AgentEvent(
        type="tool_execution_end",
        tool_call_id="t1",
        tool_name=name,
        result=result,
        submission_id=lane,
        timestamp=_TS + 1000,
        **kwargs,
    )


class TestSplitter:
    """The one streaming rule: a block reaches the scrollback once, whole."""

    def test_a_paragraph_flushes_once_the_next_line_is_complete(self) -> None:
        blocks, rest = split_markdown_blocks("one\n\ntwo\n")
        assert blocks == ["one"]
        assert rest == "two\n"

    def test_a_boundary_with_nothing_after_it_waits(self) -> None:
        assert split_markdown_blocks("one\n\n") == ([], "one\n\n")

    def test_a_fence_is_never_split(self) -> None:
        text = "```python\nx = 1\n\ny = 2\n```\n\nafter\n"
        blocks, rest = split_markdown_blocks(text)
        assert blocks == ["```python\nx = 1\n\ny = 2\n```"]
        assert rest == "after\n"

    def test_a_list_across_a_blank_line_stays_one_block(self) -> None:
        text = "- one\n\n- two\n\ndone\n"
        blocks, rest = split_markdown_blocks(text)
        assert blocks == ["- one\n\n- two"]
        assert rest == "done\n"

    def test_an_ordered_list_is_recognised_by_shape(self) -> None:
        blocks, rest = split_markdown_blocks("1. one\n\n2. two\n\nend\n")
        assert blocks == ["1. one\n\n2. two"]
        assert rest == "end\n"

    @pytest.mark.parametrize("size", [1, 3, 7, 40])
    def test_feeding_it_in_pieces_says_what_the_whole_string_says(self, size: int) -> None:
        """The streamed answer and the same text handed over at once are one rule:
        a splitter that disagreed with itself would render by chunk size."""
        text = "intro\n\n- one\n\n- two\n\n```py\nx = 1\n\ny = 2\n```\n\nafter\n\ntail"
        splitter = BlockSplitter()
        streamed: list[str] = []
        for start in range(0, len(text), size):
            streamed.extend(splitter.feed(text[start : start + size]))
        assert (streamed, splitter.remainder) == split_markdown_blocks(text)

    def test_the_scan_resumes_inside_an_open_fence_rather_than_restarting(self) -> None:
        """An open fence never flushes, so the unflushed block grows to the size of
        the whole code block: re-scanning it per delta is quadratic in the answer."""
        splitter = BlockSplitter()
        splitter.feed("```python\n")
        marks = []
        for index in range(50):
            splitter.feed(f"line {index}\n")
            marks.append(splitter.scanned)

        assert marks == sorted(marks)
        assert marks[-1] == len(splitter.remainder)


class TestLane:
    async def test_a_whole_turn_renders_in_order(self) -> None:
        """One lane, end to end: text as Markdown, a tool call and its result,
        then the footer that says what the lane read, wrote and took."""
        renderer, console, reader = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(
            text="hi", source="interactive", submitter="human", submission_id="a"
        )

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(_text_event("a", "Here is the plan.\n\nNext.\n"))
        await router.on_agent_event(_tool_start("a", "read", {"path": "main.py"}))
        await router.on_agent_event(_tool_end("a", "read", "line one\nline two"))
        await router.on_submission_end(submission=sub, side_usage={})

        out = console.export_text(clear=False)
        assert "Here is the plan." in out
        assert "⚙ read(path=main.py)" in out
        assert "✓ read — line one (2 lines)" in out
        assert "ctx 0 · out 0" in out

    async def test_no_raw_delta_reaches_the_scrollback_twice(self) -> None:
        """The growing block is not printed as it grows: the word appears once."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        for chunk in ("Alpha", " beta", " gamma"):
            await router.on_agent_event(_text_event("a", chunk))
        await router.on_submission_end(submission=sub, side_usage={})

        assert console.export_text(clear=False).count("Alpha") == 1

    async def test_a_foreign_lane_is_marked_and_not_dropped(self) -> None:
        """Jupyter's rule: a head decides HOW to render another source's lane,
        never WHETHER to."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(
            text="run the nightly",
            source="timer",
            submitter="cron:nightly",
            submission_id="t1",
        )

        await router.on_submission_start(submission=sub, text="run the nightly")
        await router.on_agent_event(_text_event("t1", "working\n\non it\n"))
        await router.on_submission_end(submission=sub, side_usage={})

        out = console.export_text(clear=False)
        assert "Timer · cron:nightly" in out
        assert "run the nightly" in out
        assert "working" in out
        assert "end Timer · cron:nightly" in out

    async def test_the_head_says_a_turn_was_aborted(self) -> None:
        """Nothing on the render stream carries an end reason (§4), so the head's
        own state machine is what prints the marker."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        renderer.aborting.add("a")
        await router.on_submission_end(submission=sub, side_usage={})

        assert "⏹ aborted" in console.export_text(clear=False)

    async def test_an_orphan_is_reported(self) -> None:
        """An event naming no open lane is real, and a head that swallowed it
        would look exactly like one that had stopped working."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer, on_orphan=renderer.on_orphan)
        await router.on_agent_event(_text_event("nope", "x"))

        assert len(renderer.orphans) == 1
        assert "orphan render event" in console.export_text(clear=False)

    async def test_a_blocked_tool_is_the_vetos_one_surface(self) -> None:
        """A veto's record reaches the sink only, so this event is where a head
        learns the call was refused."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(
            _tool_end("a", "bash", "refused", blocked=True, blocked_by="guard")
        )
        await router.on_submission_end(submission=sub, side_usage={})

        assert "⛔ bash blocked by guard" in console.export_text(clear=False)

    async def test_an_unknown_kind_raises_rather_than_passing(self) -> None:
        """A render kind with no case is a rendering decision nobody made."""
        renderer, _, _ = _renderer()
        with pytest.raises(ValueError, match="no case for render event"):
            renderer({"kind": "something_new", "lane": "a"})


class TestCompactness:
    """docs/REPL-HEAD.md §4's compact forms: one line per call, a folded result,
    a reasoning block collapsed to its size, and a footer that stays out of the
    reader's way."""

    def test_a_call_shows_its_first_argument_and_counts_the_rest(self) -> None:
        assert _format_arguments({"path": "main.py"}) == "path=main.py"
        assert _format_arguments({"path": "main.py", "limit": 20}) == "path=main.py, +1"

    def test_a_line_is_cut_at_the_width_rather_than_wrapped(self) -> None:
        assert _clip("abcdef", 4) == "abc⋯"
        assert _clip("abc", 10) == "abc"

    def test_a_result_folds_and_says_what_it_left_out(self) -> None:
        shown, hidden = fold_lines("\n".join(f"line {n}" for n in range(10)), 4)
        assert shown == ["line 0", "line 1", "line 2", "line 3"]
        assert hidden == 6

    async def test_a_long_result_prints_the_fold_and_the_count(self) -> None:
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")
        body = "\n".join(f"line {n}" for n in range(10))

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(_tool_end("a", "read", body))
        await router.on_submission_end(submission=sub, side_usage={})

        out = console.export_text(clear=False)
        assert "✓ read — line 0 (10 lines)" in out
        assert "line 3" in out
        assert "line 4" not in out
        assert "⋯ 6 more lines" in out

    async def test_reasoning_collapses_to_one_line_with_its_size(self) -> None:
        """No provider reports reasoning tokens, so the size is the core's own
        estimate and is printed as one."""
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(
            AgentEvent(
                type="message_update",
                message={"role": "assistant", "content": [{"type": "thinking", "thinking": "x" * 40}]},
                submission_id="a",
                timestamp=_TS,
            )
        )
        await router.on_agent_event(_text_event("a", "done"))
        await router.on_submission_end(submission=sub, side_usage={})

        out = console.export_text(clear=False)
        assert f"▸ reasoning (~{reasoning_tokens('x' * 40)} tokens)" in out
        assert "x" * 40 not in out

    async def test_the_footer_sits_at_the_right_margin(self) -> None:
        renderer, console, _ = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_submission_end(submission=sub, side_usage={})

        footer = [
            line for line in console.export_text(clear=False).splitlines() if "ctx 0" in line
        ]
        assert footer and footer[0].rstrip().endswith("ctx 0 · out 0")
        assert footer[0].startswith(" ")


class TestSpinner:
    async def test_the_unflushed_text_rides_the_toolbar_and_lane_end_clears_it(self) -> None:
        """"Waiting" is a claim about the stream, so the stream is what ends it —
        and what replaces it is the tail of the block that has not flushed yet
        (§4), since a paragraph with no blank line in it reaches nothing else."""
        renderer, console, reader = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        renderer.spin("waiting for the model…")
        await router.on_submission_start(submission=sub, text="hi")
        for delta in ("one\n", "two\n", "three\n", "four"):
            await router.on_agent_event(_text_event("a", delta))

        assert reader.spinners[0] == "waiting for the model…"
        assert reader.spinners[-1] == "two\nthree\nfour"
        assert "four" not in console.export_text(clear=False)

        await router.on_submission_end(submission=sub, side_usage={})
        assert reader.spinners[-1] is None

    async def test_a_delta_with_nothing_to_show_says_waiting(self) -> None:
        """A blank toolbar beside a live turn is a dead prompt to look at."""
        renderer, _, reader = _renderer()
        router = RenderRouter(renderer)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(_text_event("a", "\n"))
        assert reader.spinners[-1] == "waiting for the model…"


_USAGE = {"input_tokens": 120, "output_tokens": 30, "prompt_tokens": 120}


def _message_end(lane: str, message: dict) -> AgentEvent:
    return AgentEvent(type="message_end", message=message, submission_id=lane, timestamp=_TS + 1000)


def _same_seconds(text: str) -> str:
    """The footer's wall-clock, normalized: live and replay read two clocks."""
    return re.sub(r"\d+\.\d+s", "Ns", text)


class TestReplay:
    """The anti-drift check a second rendering path is only acceptable under:
    a resumed transcript goes through the SAME handler a live turn does."""

    async def test_a_replayed_turn_reads_as_the_live_one_did(self) -> None:
        answer = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Here is the plan.\n\nNext.\n"},
                {"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "main.py"}},
            ],
            "usage": _USAGE,
            "stop_reason": "toolUse",
        }
        live, live_console, _ = _renderer()
        router = RenderRouter(live)
        sub = Submission(text="hi", source="interactive", submitter="human", submission_id="a")

        await router.on_submission_start(submission=sub, text="hi")
        await router.on_agent_event(_text_event("a", "Here is the plan.\n\nNext.\n"))
        await router.on_agent_event(_tool_start("a", "read", {"path": "main.py"}))
        await router.on_agent_event(_tool_end("a", "read", "line one\nline two"))
        await router.on_agent_event(_message_end("a", answer))
        await router.on_submission_end(submission=sub, side_usage={})

        replayed, replay_console, _ = _renderer()
        for event in replay_render_events(
            [
                {"role": "user", "content": "hi", "timestamp": _TS},
                {**answer, "timestamp": _TS + 1000},
                {
                    "role": "toolResult",
                    "tool_name": "read",
                    "toolCallId": "t1",
                    "content": [{"type": "text", "text": "line one\nline two"}],
                    "timestamp": _TS + 3000,
                },
            ]
        ):
            replayed(event)

        echo, _, rest = replay_console.export_text(clear=False).partition("\n")
        assert echo.strip() == "› hi"
        assert _same_seconds(rest) == _same_seconds(live_console.export_text(clear=False))

    def test_the_system_prompt_is_its_size_and_reasoning_is_its_estimate(self) -> None:
        """Neither is the conversation: one is a header, the other is collapsed."""
        renderer, console, _ = _renderer()
        for event in replay_render_events(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi", "timestamp": _TS},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "one two three four"},
                        {"type": "text", "text": "Done."},
                    ],
                    "timestamp": _TS + 1,
                },
            ]
        ):
            renderer(event)

        out = console.export_text(clear=False)
        assert "system prompt (16 chars)" in out
        assert "reasoning (~" in out
        assert "one two three four" not in out
        assert "Done." in out
