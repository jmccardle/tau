"""Tests for the exchange-grouped chat-rendering state machine.

One user→answer span renders as a single collapsible ``ExchangeBox``; each turn
is one assistant ``MessageBox`` *step* (reasoning + text + ``ToolBox`` children)
mounted inside it, and the final text-only answer snaps OUT below the collapsed
summary. Covers:

1. Widget uniformity — every message is a ``MessageBox``; every tool call+result
   is one ``ToolBox`` (no bespoke per-kind widget classes).
2. Exchange grouping + promotion — intermediate steps live inside the collapsed
   summarized exchange; the final answer stays visible below it. A no-tool span
   is unwrapped to a plain answer.
3. No whole-message text duplication — each step keeps only its own turn's text;
   reasoning streams into its region and folds when the answer begins.

These drive the real ``ChatDisplay`` state machine headlessly via
``App.run_test()`` (see docs/textual-headless-testing.md), pacing events with a
render tick between them to mirror the network-paced live loop, plus a focused
unit test of ``TauBackend``'s agent-event -> structured-event mapping.

The saved-chat *reload* path reconstructs the SAME exchange grouping from the
persisted flat message list (``reload_messages``), so a reloaded chat looks like
a freshly-streamed one; ``add_persisted_message`` remains the per-message
normalizer it builds on (covered in its own section below).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownBlock

from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.app import ChatDisplay, MessageBox
from tau_coding_agent.backends import DEFAULT_LANE
from tau_coding_agent.chat_widgets import ExchangeBox, ToolBox

# ---------------------------------------------------------------------------
# Test harness app: embeds the real ChatDisplay, nothing else.
# ---------------------------------------------------------------------------


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ChatDisplay()


def _box_roles(display: ChatDisplay) -> list[str]:
    """Roles of the MessageBox widgets in document (== arrival) order."""
    return [b.role for b in display.query(MessageBox)]


def _box_texts(display: ChatDisplay) -> list[str]:
    return [b.content_text for b in display.query(MessageBox)]


def _top_level(display: ChatDisplay) -> list:
    """Immediate children of the display (top-level boxes + exchanges)."""
    return list(display.children)


async def _send(display: ChatDisplay, pilot, event: dict) -> None:
    """Deliver one lifecycle event, then yield a render tick.

    This is the PACED cadence: a tick between every event. It is one of the two
    cadences the live loop really produces, and it is the easy one.

    The other is a synchronous burst, and this helper used to carry a comment
    claiming the real backend never produces one. That was wrong, and it is why
    a deterministic bug (§2a) shipped with no failing test: ``asyncio.Queue.get``
    on a non-empty queue returns without yielding, so the agent loop hands
    several events to the display back to back with no event-loop tick between
    them. Burst coverage lives in ``_send_burst`` below; use it for anything that
    touches mounting.
    """
    await display.handle_stream_event(event)
    await pilot.pause()


async def _send_burst(display: ChatDisplay, events: list[dict]) -> None:
    """Deliver several lifecycle events with NO render tick between them.

    Mirrors the agent loop draining a queue that already has events in it. The
    caller pauses afterwards, which is when Textual settles the mounts these
    events queued.
    """
    for event in events:
        await display.handle_stream_event(event)


async def _fresh_parse_blocks(pilot, text: str) -> list[str]:
    """The block texts a ONE-SHOT ``Markdown.update(text)`` produces.

    The independent oracle for the streamed-rendering test above: mounts a
    brand new ``Markdown`` widget, does a normal whole-text parse of the same
    final string, and returns its blocks' plain text -- what incremental
    ``append()``-based streaming must match, block for block.
    """
    md = Markdown("")
    await pilot.app.query_one(ChatDisplay).mount(md)
    try:
        await md.update(text)
        await pilot.pause()
        return [b._content.plain for b in md.query(MarkdownBlock)]
    finally:
        await md.remove()


# A realistic one-turn-with-tools span as produced by TauBackend.stream_chat's
# on_event sink, now grouped into an exchange:
#   user already on screen, then the assistant loop runs inside one exchange:
#     turn 0: preamble text -> tool call -> result   (a step inside the exchange)
#     turn 1: final answer text                       (snaps OUT below the summary)
async def _replay_tool_turn(display: ChatDisplay, pilot) -> None:
    display.add_message("user", "list the files", source="verbatim")
    await display.begin_exchange()

    await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
    await _send(display, pilot, {"kind": "text_delta", "delta": "Sure, "})
    await _send(display, pilot, {"kind": "text_delta", "delta": "let me look."})
    await _send(
        display, pilot, {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {"path": "."}}
    )
    await _send(
        display,
        pilot,
        {
            "kind": "tool_result",
            "id": "c1",
            "name": "ls",
            "result": "a.py\nb.py",
            "is_error": False,
        },
    )
    await _send(display, pilot, {"kind": "turn_start", "turn_index": 1})
    await _send(display, pilot, {"kind": "text_delta", "delta": "There are "})
    await _send(display, pilot, {"kind": "text_delta", "delta": "two files."})

    await display.finalize_exchange(context=900, output=12, seconds=6)


async def test_exchange_groups_tools_and_promotes_final_answer():
    """The span collapses to ONE summarized exchange; the final answer snaps
    out below it, staying visible. Intermediate steps live inside the exchange."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await _replay_tool_turn(display, pilot)
        await pilot.pause()

        # Top level: user box, the collapsed exchange, the promoted final answer.
        top = _top_level(display)
        assert isinstance(top[0], MessageBox) and top[0].role == "user"
        assert isinstance(top[1], ExchangeBox)
        assert isinstance(top[2], MessageBox) and top[2].role == "assistant"
        assert top[2].content_text == "There are two files."

        exchange = top[1]
        assert exchange.collapsed is True
        assert "1 tool" in exchange.title and "out" in exchange.title

        # The preamble step lives INSIDE the exchange with its tool folded in.
        step_boxes = list(exchange.query(MessageBox))
        assert len(step_boxes) == 1
        assert step_boxes[0].content_text == "Sure, let me look."
        tools = list(step_boxes[0].tool_boxes.values())
        assert len(tools) == 1 and tools[0].has_result is True


async def test_no_text_duplication():
    """No box concatenates another turn's text: the preamble step keeps only its
    own text, the promoted answer keeps only the final text."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await _replay_tool_turn(display, pilot)
        await pilot.pause()

        assistant_texts = [
            b.content_text for b in display.query(MessageBox) if b.role == "assistant"
        ]
        assert assistant_texts == ["Sure, let me look.", "There are two files."], assistant_texts
        for t in assistant_texts:
            assert t.count("There are two files.") <= 1
            assert t.count("Sure, let me look.") <= 1


async def test_messages_and_tools_use_uniform_widgets():
    """Every message is a MessageBox; every tool call+result is one ToolBox —
    no bespoke per-kind widget classes."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await _replay_tool_turn(display, pilot)
        await pilot.pause()

        # user + preamble step + promoted answer = 3 MessageBoxes (the tool
        # call/result do NOT become their own MessageBoxes anymore).
        boxes = list(display.query(MessageBox))
        assert all(type(b) is MessageBox for b in boxes)
        assert _box_roles(display) == ["user", "assistant", "assistant"]
        tool_boxes = list(display.query(ToolBox))
        assert len(tool_boxes) == 1 and all(type(t) is ToolBox for t in tool_boxes)


async def test_trivial_exchange_unwrapped_to_plain_answer():
    """A no-tool span has nothing to group — the wrapper is dropped and only the
    plain answer remains (no empty '0 tools' summary line)."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.add_message("user", "hi", source="verbatim")
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await _send(display, pilot, {"kind": "text_delta", "delta": "hello"})
        await display.finalize_exchange(context=400, output=3, seconds=1)
        await pilot.pause()

        assert list(display.query(ExchangeBox)) == []
        assert _box_roles(display) == ["user", "assistant"]
        answer = list(display.query(MessageBox))[-1]
        assert answer.content_text == "hello"
        # Real token + duration are surfaced on the answer (no summary line here).
        assert answer._subtitle == "400 ctx · 3 out · 0:01"


async def test_reasoning_streams_into_step_and_collapses_on_text():
    """Reasoning streams into the step's region (expanded), then folds away the
    instant answer text begins; the promoted answer keeps the reasoning."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await _send(display, pilot, {"kind": "reasoning_delta", "delta": "Let me think. "})
        await _send(display, pilot, {"kind": "reasoning_delta", "delta": "2+2=4."})

        step = display.active_step()
        assert step is not None and step.reasoning is not None
        assert step.reasoning.collapsed is False  # expanded while thinking
        assert step.reasoning.text == "Let me think. 2+2=4."

        await _send(display, pilot, {"kind": "text_delta", "delta": "The answer is 4."})
        assert step.reasoning.collapsed is True  # answer began -> reasoning folds

        await display.finalize_exchange(context=400, output=5, seconds=1)
        await pilot.pause()

        # No tools -> unwrapped to a single answer box that carries the reasoning.
        assert list(display.query(ExchangeBox)) == []
        answer = display.query_one(MessageBox)
        assert answer.content_text == "The answer is 4."
        assert answer.reasoning is not None
        assert answer.reasoning.text == "Let me think. 2+2=4."
        assert answer.reasoning.collapsed is True


def _rendered_text(widget) -> str:
    """The text actually parsed into ``widget``'s Markdown blocks.

    Deliberately not ``region.text``/``box.content_text`` — those read the
    buffer, which was full the whole time §2a was losing everything. Only the
    block tree says a token reached the screen.
    """
    return "".join(b._content.plain for b in widget.query(MarkdownBlock))


# ---------------------------------------------------------------------------
# §2a: mounting under a synchronous burst.
#
# Every test above paces its events with a render tick. These do not, because
# the live loop does not: `_start_step` mounts the step fire-and-forget and the
# agent loop drains its queue without yielding, so the first reasoning delta or
# tool call of a turn arrives before the step box has composed. Before the fix
# `MessageBox.ensure_reasoning` mounted into a slot `compose()` had not created
# yet, raised `AttributeError` — swallowed by `EventBus.emit` and misreported as
# an extension error — and left `self._reasoning` pointing at a region that was
# never mounted, so every later delta accumulated into a widget nobody could see.
# ---------------------------------------------------------------------------


async def test_reasoning_burst_before_the_step_composes_still_renders():
    """The reported §2a bug: 0 of 28 reasoning tokens reached the screen."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()

        # turn_start and the first deltas in one burst — no tick anywhere.
        await _send_burst(
            display,
            [{"kind": "turn_start", "turn_index": 0}]
            + [{"kind": "reasoning_delta", "delta": f"t{i} "} for i in range(8)],
        )
        await pilot.pause()

        step = display.active_step()
        assert step is not None and step.reasoning is not None
        assert step.reasoning.is_mounted, "the region was created but never mounted"
        assert _rendered_text(step.reasoning) == "t0 t1 t2 t3 t4 t5 t6 t7"

        # Paced deltas after the burst keep landing in the same region.
        for i in range(8, 12):
            await _send(display, pilot, {"kind": "reasoning_delta", "delta": f"t{i} "})
        assert _rendered_text(step.reasoning) == "t0 t1 t2 t3 t4 t5 t6 t7 t8 t9 t10 t11"
        assert step.reasoning.text == "".join(f"t{i} " for i in range(12))


async def test_a_reasoning_burst_reports_no_error():
    """The AttributeError was swallowed and blamed on an extension, so assert on
    the raise itself: handle_stream_event must not throw under a burst."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        await _send_burst(
            display,
            [
                {"kind": "turn_start", "turn_index": 0},
                {"kind": "reasoning_delta", "delta": "think"},
            ],
        )
        await pilot.pause()


async def test_text_burst_before_the_step_composes_still_renders():
    """The text body already buffered pre-compose; this pins that it still does,
    since the reasoning fix routes through the same on_mount catch-up."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        await _send_burst(
            display,
            [
                {"kind": "turn_start", "turn_index": 0},
                {"kind": "text_delta", "delta": "The answer "},
                {"kind": "text_delta", "delta": "is 4."},
            ],
        )
        await pilot.pause()
        step = display.active_step()
        assert step is not None
        assert _rendered_text(step._md_widget) == "The answer is 4."


async def test_reasoning_then_text_in_one_burst_both_render():
    """Both slots used before compose(), in the order the live path produces.

    The whole turn arriving at once means the answer folds the reasoning away
    before the region has mounted, so the region mounts already-collapsed and
    D1's deferred parse applies: nothing is in its block tree while it is shut,
    and expanding it renders the buffer. That is the same end state the paced
    path reaches by a different route, and it is what the reader sees.
    """
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        await _send_burst(
            display,
            [
                {"kind": "turn_start", "turn_index": 0},
                {"kind": "reasoning_delta", "delta": "2+2 is 4."},
                {"kind": "text_delta", "delta": "The answer is 4."},
            ],
        )
        await pilot.pause()
        step = display.active_step()
        assert step is not None and step.reasoning is not None
        assert step.reasoning.is_mounted
        assert _rendered_text(step._md_widget) == "The answer is 4."
        assert step.reasoning.collapsed is True  # answer began -> reasoning folds
        assert step.reasoning.text == "2+2 is 4."

        step.reasoning.collapsed = False  # the reader opens it
        await pilot.pause()
        assert _rendered_text(step.reasoning) == "2+2 is 4."


async def test_tool_call_and_result_in_one_burst_both_render():
    """``add_tool_call`` had the identical slot bug one method down, and the
    result body is lost if ``ToolBox.set_result`` runs before the box mounts —
    so buffering the box without buffering its result would trade a crash for
    silent data loss."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        await _send_burst(
            display,
            [
                {"kind": "turn_start", "turn_index": 0},
                {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {"path": "."}},
                {"kind": "tool_result", "id": "c1", "name": "ls", "result": "a.py\nb.py"},
            ],
        )
        await pilot.pause()
        boxes = list(display.query(ToolBox))
        assert len(boxes) == 1
        box = boxes[0]
        assert box.is_mounted
        assert box.has_result
        assert box.title == "✓ ls(path=.)"
        assert "a.py\nb.py" in _rendered_text(box._result_md)


async def test_a_deferred_tool_result_survives_an_error_and_a_block():
    """The error and veto branches write the body through the same buffer."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        await _send_burst(
            display,
            [
                {"kind": "turn_start", "turn_index": 0},
                {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {}},
                {"kind": "tool_result", "id": "c1", "result": "boom", "is_error": True},
                {"kind": "tool_call", "id": "c2", "name": "rm", "arguments": {}},
                {"kind": "tool_result", "id": "c2", "result": "nope", "blocked": True},
            ],
        )
        await pilot.pause()
        errored, blocked = list(display.query(ToolBox))
        assert "boom" in _rendered_text(errored._result_md)
        assert errored.has_class("box-error")
        assert "nope" in _rendered_text(blocked._result_md)
        assert blocked.has_class("box-blocked")


async def test_streamed_rendering_matches_full_parse_across_awkward_deltas():
    """Fix C: reasoning/answer stream through MarkdownStream's incremental
    ``append()`` instead of a full ``update()`` rebuild on every tick. A timing
    win that renders "fast and blank" (or corrupted) would still pass a
    benchmark, so this asserts on the actual rendered ``MarkdownBlock`` text —
    not just the accumulator strings ``content_text``/``reasoning.text``,
    which ``update_content``/``set_text`` would keep correct even if the
    widget itself were never touched.

    Deltas are split on deliberately awkward boundaries — mid multi-newline
    run, mid-word — because a delta that splits a run of newlines is exactly
    where an incremental append could diverge from a whole-text parse.
    """
    reasoning_full = "Step one.\nStep two.\n\nStep three, a longer line.\nFinal step."
    reasoning_deltas = [
        "Step one.\nStep tw",  # mid-word
        "o.\n",  # splits the "\n\n" run: first newline lands here...
        "\nStep three, a long",  # ...second newline lands here, then mid-word
        "er line.\nFinal step.",
    ]
    answer_full = "Answer line one.\nAnswer line two.\n\nAnswer line three."
    answer_deltas = [
        "Answer line one",
        ".\nAnswer li",
        "ne two.\n",
        "\nAnswer line three.",
    ]
    assert "".join(reasoning_deltas) == reasoning_full
    assert "".join(answer_deltas) == answer_full

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        for delta in reasoning_deltas:
            await _send(display, pilot, {"kind": "reasoning_delta", "delta": delta})
        for delta in answer_deltas:
            await _send(display, pilot, {"kind": "text_delta", "delta": delta})

        step = display.active_step()
        assert step is not None and step.reasoning is not None
        # The accumulators are correct either way (kept in sync on every
        # delta) -- the real question is what the WIDGET actually rendered.
        assert step.reasoning.text == reasoning_full
        assert step.content_text == answer_full

        streamed_reasoning_blocks = [b._content.plain for b in step.reasoning.query(MarkdownBlock)]
        # Scoped to the answer's OWN Markdown widget: step.query(MarkdownBlock)
        # would also pick up the reasoning region's blocks (a child of the same
        # box), which live under a *different* Markdown widget entirely.
        streamed_answer_blocks = [b._content.plain for b in step._md_widget.query(MarkdownBlock)]
        # An assistant answer's source is "markdown", so _format passes it
        # through and the widget must match a plain whole-text parse.
        assert streamed_reasoning_blocks == await _fresh_parse_blocks(pilot, reasoning_full)
        assert streamed_answer_blocks == await _fresh_parse_blocks(pilot, answer_full)
        # Non-trivial: an awkward split that silently dropped/duplicated a
        # paragraph break would still leave the accumulators right but collapse
        # or duplicate a block here. Both bodies are read as markdown, so their
        # single "\n"s are SOFT breaks (same paragraph) and only the "\n\n" run
        # starts a new one: 2 blocks each.
        assert len(streamed_reasoning_blocks) == 2
        assert len(streamed_answer_blocks) == 2

        await display.finalize_exchange(context=400, output=5, seconds=1)
        await pilot.pause()


async def test_empty_terminal_turn_leaves_nothing():
    """A turn that streams nothing renderable then ends leaves no placeholder."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await display.finalize_exchange(context=0, output=0, seconds=0)
        await pilot.pause()
        assert _box_roles(display) == []
        assert list(display.query(ExchangeBox)) == []


async def test_tool_only_final_turn_keeps_collapsed_exchange():
    """If the terminal step still has a tool (no clean answer), leave it grouped
    and collapsed rather than snapping a tool box out as a fake 'answer'."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await _send(
            display, pilot, {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {}}
        )
        await _send(
            display,
            pilot,
            {"kind": "tool_result", "id": "c1", "name": "ls", "result": "x", "is_error": False},
        )
        await display.finalize_exchange(context=400, output=4, seconds=1)
        await pilot.pause()

        exchanges = list(display.query(ExchangeBox))
        assert len(exchanges) == 1 and exchanges[0].collapsed is True
        assert "1 tool" in exchanges[0].title
        # No promoted answer at top level — only the exchange.
        assert [c for c in display.children if isinstance(c, MessageBox)] == []


async def test_tool_result_error_marks_toolbox():
    """An errored tool result marks ITS ToolBox (folded in by id), not a
    separate error box."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await _send(
            display,
            pilot,
            {"kind": "tool_call", "id": "c1", "name": "bash", "arguments": {"command": "false"}},
        )
        await _send(
            display,
            pilot,
            {"kind": "tool_result", "id": "c1", "name": "bash", "result": "boom", "is_error": True},
        )

        step = display.active_step()
        assert step is not None
        tb = step.tool_boxes["c1"]
        assert tb.has_result is True
        assert tb.has_class("box-error")


# ---------------------------------------------------------------------------
# add_persisted_message: the per-message normalizer (flat boxes).
#
# Regression for the busy-loop/freeze on clicking a sidebar session: a saved
# assistant/toolResult message stores content as a *list of block dicts*, and
# handing that straight to the str-only MessageBox raised
# `'list' object has no attribute 'replace'` inside compose() — which, fired
# for every message during the mount/layout cycle, manifested as a freeze. This
# normalizer is the building block reload_messages composes into exchanges.
# ---------------------------------------------------------------------------


# The persisted shape of a [text -> tool call -> result -> final text] turn, as
# written to ~/.tau/chats/*.json (assistant content is a block list; toolResult
# is its own role with tool_name/is_error at the message level).
_PERSISTED_TURN = [
    {"role": "user", "content": "list the files"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Sure, let me look."},
            {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"path": "."}},
        ],
    },
    {
        "role": "toolResult",
        "tool_call_id": "c1",
        "tool_name": "ls",
        "is_error": False,
        "content": [{"type": "text", "text": "a.py\nb.py"}],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "There are two files."}]},
]


async def test_reload_list_content_renders_in_arrival_order():
    """Reloading a saved chat renders the SAME boxes/order as live streaming."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        for msg in _PERSISTED_TURN:
            display.add_persisted_message(msg)
        await pilot.pause()

        roles = _box_roles(display)
        assert roles == ["user", "assistant", "toolCall", "toolResult", "assistant"], roles
        # Final assistant text lands last, identical to the live path.
        assert roles[-1] == "assistant"
        assert _box_texts(display)[-1] == "There are two files."


async def test_reload_does_not_raise_on_list_content():
    """The exact regression: list content must not raise (the old freeze)."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        # Tool-only assistant message (no preamble text) — pure block list.
        display.add_persisted_message(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "x",
                        "name": "bash",
                        "arguments": {"command": "date"},
                    },
                ],
            }
        )
        await pilot.pause()
        assert _box_roles(display) == ["toolCall"]


async def test_reload_plain_string_content():
    """Older chats store assistant content as a plain string; still renders."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.add_persisted_message({"role": "user", "content": "hi"})
        display.add_persisted_message({"role": "assistant", "content": "hello there"})
        await pilot.pause()
        assert _box_roles(display) == ["user", "assistant"]
        assert _box_texts(display)[-1] == "hello there"


async def test_reload_toolresult_error_gets_error_class():
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.add_persisted_message(
            {
                "role": "toolResult",
                "tool_name": "bash",
                "is_error": True,
                "content": [{"type": "text", "text": "boom"}],
            }
        )
        await pilot.pause()
        box = display.query_one(MessageBox)
        assert box.role == "toolResult"
        assert box.has_class("box-error")


async def test_reload_unrenderable_content_raises():
    """Fail-Early: an unexpected content shape raises rather than dropping it."""
    import pytest

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        with pytest.raises(TypeError):
            display.add_persisted_message({"role": "assistant", "content": {"unexpected": "dict"}})


async def test_headless_saved_session_round_trips(tmp_path, monkeypatch):
    """End-to-end contract: a session written by `tau -p` (append-on-message)
    reloads cleanly through the renderer — the write format and read renderer
    agree, which is what makes a headless run *resumable* from the TUI."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    session = store.Session.create("/proj", "local-llm", "openai", system_prompt="sys")
    session.append_message({"role": "user", "content": "run date"})
    for msg in [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "date"}},
            ],
        },
        {
            "role": "toolResult",
            "tool_call_id": "c1",
            "tool_name": "bash",
            "is_error": False,
            "content": [{"type": "text", "text": "Thu"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "It's Thursday."}]},
    ]:
        session.append_message(msg)

    loaded = store.Session.load(session.path)
    assert loaded.model == "local-llm"  # resolvable config key -> resumable
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(loaded.messages)
        await pilot.pause()
        # Reconstructs the exchange: user box, collapsed exchange, final answer.
        top = _top_level(display)
        assert isinstance(top[0], MessageBox) and top[0].role == "user"
        assert isinstance(top[1], ExchangeBox)
        assert isinstance(top[2], MessageBox) and top[2].role == "assistant"
        assert top[2].content_text == "It's Thursday."


# ---------------------------------------------------------------------------
# reload_messages: reconstruct exchanges from the persisted flat list (#5).
#
# Walks the flat transcript back into the SAME widget tree the live state
# machine leaves behind — collapsed ExchangeBox per span, folded tool boxes, the
# terminal answer promoted out. The only difference is the summary omits
# wall-clock duration (not persisted; not fabricated — Fail-Early).
# ---------------------------------------------------------------------------


async def test_reload_reconstructs_exchange_like_live():
    """A persisted [text -> tool -> result -> answer] turn reloads into the same
    shape the live path produces: user box, collapsed exchange, promoted answer."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages([{"role": "system", "content": "s"}] + _PERSISTED_TURN)
        await pilot.pause()

        top = _top_level(display)
        assert isinstance(top[0], MessageBox) and top[0].role == "user"
        assert isinstance(top[1], ExchangeBox)
        assert isinstance(top[2], MessageBox) and top[2].role == "assistant"
        assert top[2].content_text == "There are two files."

        exchange = top[1]
        assert exchange.collapsed is True
        assert "1 tool" in exchange.title and "out" in exchange.title
        # No fabricated duration on reload — the title has no 'M:SS' segment.
        assert ":" not in exchange.title

        step_boxes = list(exchange.query(MessageBox))
        assert len(step_boxes) == 1
        assert step_boxes[0].content_text == "Sure, let me look."
        tools = list(step_boxes[0].tool_boxes.values())
        assert len(tools) == 1 and tools[0].has_result is True


async def test_reload_sums_output_but_takes_context_from_the_last_completion():
    """Reload mirrors the live path: ``output`` sums across the span, ``context``
    is the LAST completion's prompt. Summing prompts would report 100 + 500 = 600
    for a conversation that only ever reached 500 tokens, which is the
    running-total overcount this replaced."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
            "content": [{"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}}],
        },
        {
            "role": "toolResult",
            "tool_call_id": "c1",
            "tool_name": "ls",
            "is_error": False,
            "content": [{"type": "text", "text": "x"}],
        },
        {
            "role": "assistant",
            "usage": {
                "input_tokens": 400,
                "cache_read_tokens": 100,
                "output_tokens": 12,
                "total_tokens": 512,
            },
            "content": [{"type": "text", "text": "done"}],
        },
    ]
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(messages)
        await pilot.pause()
        exchange = display.query_one(ExchangeBox)
        # 30 + 12 generated; context is 400 + 100 cached, NOT 100 + 500 summed.
        assert "500 ctx" in exchange.title
        assert "42 out" in exchange.title


async def test_reload_consolidates_legacy_bloated_blocks():
    """A legacy assistant message with many one-fragment thinking/text blocks
    (written before the provider consolidated them) reloads as ONE reasoning
    region + ONE answer body, not hundreds of boxes."""
    bloated = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": t} for t in ("Let ", "me ", "think.")]
        + [{"type": "text", "text": t} for t in ("The ", "answer.")],
    }
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages([{"role": "user", "content": "q"}, bloated])
        await pilot.pause()
        # No tools -> unwrapped to a single answer carrying the joined reasoning.
        assert list(display.query(ExchangeBox)) == []
        answer = [b for b in display.query(MessageBox) if b.role == "assistant"][-1]
        assert answer.content_text == "The answer."
        assert answer.reasoning is not None
        assert answer.reasoning.text == "Let me think."


async def test_reload_tool_only_final_keeps_collapsed_exchange():
    """A span whose last completion is still a tool call (cut off) stays grouped
    and collapsed — no tool box promoted as a fake answer."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}}],
        },
        {
            "role": "toolResult",
            "tool_call_id": "c1",
            "tool_name": "ls",
            "is_error": False,
            "content": [{"type": "text", "text": "x"}],
        },
    ]
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(messages)
        await pilot.pause()
        exchanges = list(display.query(ExchangeBox))
        assert len(exchanges) == 1 and exchanges[0].collapsed is True
        top_boxes = [c for c in display.children if isinstance(c, MessageBox)]
        assert len(top_boxes) == 1 and top_boxes[0].role == "user"  # only the user box


async def test_reload_multiple_user_turns_make_separate_exchanges():
    """Each user turn starts a fresh span; a trivial second turn is unwrapped."""
    messages = _PERSISTED_TURN + [
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(messages)
        await pilot.pause()
        users = [b for b in display.query(MessageBox) if b.role == "user"]
        assert len(users) == 2
        # First span has a tool -> one collapsed exchange; second is trivial.
        assert len(list(display.query(ExchangeBox))) == 1


# ---------------------------------------------------------------------------
# Focused unit test: TauBackend agent-event -> structured-event mapping.
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Minimal stand-in for tau_agent_core AgentEvent (attribute access)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSession:
    """Replays a scripted AgentEvent sequence through the subscribed handler."""

    def __init__(self, events):
        self._events = events
        self._handler = None
        # A real AgentSession keeps a ledger of tokens spent OUTSIDE the loop
        # (compaction, ctx.complete()); stream_chat folds the exchange's delta into
        # usage_totals. This fake makes no such calls, so a constant zero is the
        # honest reading, not a stub. See tau_agent_core.usage.
        self._side = dict.fromkeys(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
            ),
            0,
        )

    @property
    def side_usage(self):
        return dict(self._side)

    def subscribe(self, handler):
        self._handler = handler
        return lambda: None

    async def submit(self, submission, context=None):
        # `submit`, not `prompt`: since B2-a the backend admits the caller's own
        # Submission through the one door instead of routing via the prompt()
        # compatibility wrapper (docs/SUBMISSION-LIFECYCLE.md phase 3).
        for ev in self._events:
            self._handler(ev)
        return SubmissionResult(
            accepted=True,
            submission_id=submission.submission_id,
            messages=[],  # new_messages — irrelevant to this test
        )


async def test_backend_event_to_structured_mapping():
    """tool widgets come from tool_execution_*; duplicate message_end is deduped."""
    from tau_coding_agent.backends import TauBackend

    # Build a backend, then swap in a fake session (no network).
    backend = TauBackend(
        {
            "model": "m",
            "backend": "openai",
            "base_url": "http://x",
            "api_key": "not-needed",
            "tools": [],
        }
    )

    # Scripted sequence mirroring agent_loop.py for [text -> tool call -> result -> final text]:
    events = [
        _FakeEvent(type="agent_start", timestamp=0),
        _FakeEvent(type="turn_start", timestamp=0, turn_index=0),
        # streaming preamble text: _stream_response re-sends the full accumulated text
        _FakeEvent(
            type="message_start",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
        ),
        _FakeEvent(
            type="message_update",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
        ),
        _FakeEvent(
            type="message_update",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
        ),
        # DoneEvent message_end (in _stream_response)
        _FakeEvent(
            type="message_end",
            timestamp=0,
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hi there"},
                    {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}},
                ],
            },
        ),
        # DUPLICATE message_end (emitted again in run() because tool calls exist)
        _FakeEvent(
            type="message_end",
            timestamp=0,
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hi there"},
                    {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}},
                ],
            },
        ),
        _FakeEvent(
            type="tool_execution_start",
            timestamp=0,
            tool_call_id="c1",
            tool_name="ls",
            args={"p": "."},
        ),
        _FakeEvent(
            type="tool_execution_end",
            timestamp=0,
            tool_call_id="c1",
            tool_name="ls",
            result=[{"type": "text", "text": "a.py"}],
            is_error=False,
        ),
        _FakeEvent(type="turn_end", timestamp=0, turn_index=0, tool_results=[]),
        # turn 1: final answer
        _FakeEvent(type="turn_start", timestamp=0, turn_index=1),
        _FakeEvent(
            type="message_update",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        ),
        _FakeEvent(
            type="message_end",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        ),
        _FakeEvent(type="turn_end", timestamp=0, turn_index=1, tool_results=[]),
        _FakeEvent(type="agent_end", timestamp=0),
    ]
    backend.agent_session = _FakeSession(events)  # type: ignore[assignment]

    structured: list[dict] = []
    text_deltas: list[str] = []

    full, usage, new_messages, tool_calls_info = await backend.stream_chat(
        [{"role": "user", "content": "hi"}],
        callback=lambda d: text_deltas.append(d),
        on_event=lambda e: structured.append(e),
    )

    kinds = [e["kind"] for e in structured]
    # Exactly one tool_call + one tool_result (NOT two from the duplicate
    # message_end), and they come from tool_execution_*.
    assert kinds.count("tool_call") == 1, kinds
    assert kinds.count("tool_result") == 1, kinds
    assert kinds.count("turn_start") == 2, kinds

    # Text deltas are real fragments, not the full re-sent string each time.
    # "Hi" then "Hi there" -> deltas "Hi", " there"; then turn 1 "Done".
    assert text_deltas == ["Hi", " there", "Done"], text_deltas
    assert full == "Hi there" + "Done"

    # tool_result content carries the joined text and error flag.
    tr = next(e for e in structured if e["kind"] == "tool_result")
    assert tr["result"] == "a.py"
    assert tr["is_error"] is False

    # tool_calls_info (for persistence) deduped to a single entry by id.
    assert len(tool_calls_info) == 1
    assert tool_calls_info[0]["id"] == "c1"
    assert tool_calls_info[0]["result"] == "a.py"


class _CompactingFakeSession(_FakeSession):
    """A session that AUTO-COMPACTS during the exchange.

    Compaction summarizes the whole conversation, so its input is roughly a full
    context window — routinely the priciest single call in a session, and it fires
    without the user asking. It goes through `complete_simple`, which emits no
    events, so it lands on the session's side ledger rather than on a `message_end`.
    """

    async def submit(self, submission, context=None):
        out = await super().submit(submission, context)
        # What AgentSession._perform_compaction does at the end-of-prompt drain.
        self._side["input_tokens"] += 6000
        self._side["output_tokens"] += 200
        self._side["total_tokens"] += 6200
        return out


async def test_stream_chat_usage_includes_tokens_spent_off_the_agent_loop():
    """The bug, at the seam where the user actually saw it.

    usage_totals was summed purely from `message_end` events, which only the agent
    LOOP emits. An auto-compaction inside the same exchange spent thousands of tokens
    that reached no event and therefore no meter — so the cost τ displayed was
    confidently UNDERSTATED, in the direction that makes a session look cheaper than
    it was. `stream_chat` now folds in the session's side-ledger delta.
    """
    from tau_coding_agent.backends import TauBackend

    backend = TauBackend(
        {
            "model": "m",
            "backend": "openai",
            "base_url": "http://x",
            "api_key": "not-needed",
            "tools": [],
            # Priced, so the assertion lands on the number the user is actually shown.
            "cost": {"input": 1_000_000.0, "output": 1_000_000.0},
        }
    )

    loop_usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    events = [
        _FakeEvent(type="agent_start", timestamp=0),
        _FakeEvent(type="turn_start", timestamp=0, turn_index=0),
        _FakeEvent(
            type="message_end",
            timestamp=0,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": loop_usage,
            },
        ),
        _FakeEvent(type="turn_end", timestamp=0, turn_index=0, tool_results=[]),
        _FakeEvent(type="agent_end", timestamp=0),
    ]
    backend.agent_session = _CompactingFakeSession(events)  # type: ignore[assignment]

    _full, usage, _new, _tc = await backend.stream_chat(
        [{"role": "user", "content": "hi"}], callback=lambda d: None
    )

    # The loop's own 15 tokens PLUS the compaction's 6200 — not 15.
    assert usage["prompt_tokens"] == 6010
    assert usage["completion_tokens"] == 205
    assert usage["total_tokens"] == 6215, "the compaction's tokens are no longer invisible"

    # And the price the user sees follows: $1/token here, so 6215 tokens != 15.
    assert usage["cost_usd"] == pytest.approx(6215.0)


# ---------------------------------------------------------------------------
# §1 + §2b: the render cap, and the batched reload.
#
# Both symptoms in docs/PLAN-0.9.4.md — "long conversations take a long time to
# load" and "text accumulates but doesn't display" — are one defect: Textual
# re-arranges the WHOLE widget tree on every layout pass, so cost is quadratic
# in the mounted widget count. An 800-message reload took over four minutes and
# throttled the next turn to a couple of tokens a second.
#
# Two levers, tested here structurally rather than by wall-clock: bound the
# mounted widgets (the cap), and stop re-arranging between every mount (the
# batch). A timing assertion would be flaky on a loaded machine; the widget
# count and the layout-pass count are the things that actually cause the time.
# ---------------------------------------------------------------------------


def _transcript(turns: int, *, system: bool = True) -> list[dict]:
    """``turns`` user→assistant pairs, optionally behind a system message."""
    msgs: list[dict] = [{"role": "system", "content": "s"}] if system else []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return msgs


def _fold_rows(display: ChatDisplay) -> list:
    return list(display.query(".chat-fold"))


def _count_layouts(app: App) -> "list[int]":
    """Start counting the screen's full layout passes. Returns a 1-item list."""
    count = [0]
    original = app.screen._refresh_layout

    def counting(*args, **kwargs):
        count[0] += 1
        return original(*args, **kwargs)

    app.screen._refresh_layout = counting  # type: ignore[method-assign]
    return count


def test_render_cap_stops_at_whichever_bound_comes_first():
    """Walking backwards, the bound that cuts MORE is the one reached first."""
    display = ChatDisplay()
    # 20 short turns: the 4-turn bound bites long before the 50-message one.
    msgs = _transcript(20)
    start = display.render_cap_start(msgs)
    assert [m["role"] for m in msgs[start:]].count("user") == ChatDisplay.RENDER_CAP_TURNS

    # One turn per 20 messages: now the MESSAGE bound bites first, and the start
    # is still a user message, so no user→answer span is cut in half.
    fat: list[dict] = []
    for i in range(10):
        fat.append({"role": "user", "content": f"q{i}"})
        fat += [{"role": "assistant", "content": []} for _ in range(19)]
    start = display.render_cap_start(fat)
    assert fat[start]["role"] == "user"
    assert len(fat) - start <= ChatDisplay.RENDER_CAP_MESSAGES


def test_a_short_transcript_is_not_capped_at_all():
    display = ChatDisplay()
    assert display.render_cap_start(_transcript(3)) == 0


def test_one_span_longer_than_the_bound_is_mounted_whole():
    """Rendering nothing is not a smaller version of rendering something.

    A single turn with 80 tool results exceeds the message bound on its own.
    Snapping forward to the next user message would leave the display empty, so
    the span mounts whole and the bound is the thing that gives.
    """
    display = ChatDisplay()
    msgs: list[dict] = [{"role": "user", "content": "q"}]
    msgs += [{"role": "assistant", "content": []} for _ in range(80)]
    assert display.render_cap_start(msgs) == 0


def test_a_transcript_with_no_user_message_is_mounted_whole():
    display = ChatDisplay()
    assert display.render_cap_start([{"role": "assistant", "content": []}] * 90) == 0


async def test_a_capped_reload_mounts_the_tail_and_says_what_it_left_out():
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        msgs = _transcript(30)
        await display.reload_messages(msgs)
        await pilot.pause()

        # 4 turns => 8 boxes, whatever the transcript length is.
        assert len(display.query(MessageBox)) == 2 * ChatDisplay.RENDER_CAP_TURNS
        assert _box_texts(display)[-1] == "a29", "the tail is the END of the transcript"

        # The system message never renders, so it is not counted as hidden.
        assert display.elided_count == 60 - 2 * ChatDisplay.RENDER_CAP_TURNS
        rows = _fold_rows(display)
        assert len(rows) == 1
        assert f"⋯ {display.elided_count} earlier" in str(rows[0].content)
        assert display.children[0] is rows[0], "the row stands where the messages would"


async def test_the_cap_bounds_widgets_but_not_the_message_list():
    """Fail Early: this is a RENDERING bound. Nothing may leave the data.

    The list handed in is not copied, not sliced and not mutated — the display
    keeps the caller's own object, which is what lets `show_all_messages` render
    the rest without a second source of truth to fall out of step.
    """
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        msgs = _transcript(30)
        before = list(msgs)
        await display.reload_messages(msgs)
        await pilot.pause()
        assert msgs == before
        assert display._reload_source is msgs


async def test_the_mounted_widget_count_does_not_grow_with_the_transcript():
    """The quadratic's input, held flat. This is why the cap exists."""
    counts = []
    for turns in (10, 40, 160):
        async with _Harness().run_test() as pilot:
            await pilot.pause()
            display = pilot.app.query_one(ChatDisplay)
            await display.reload_messages(_transcript(turns))
            await pilot.pause()
            counts.append(len(display.query("*")))
    assert counts[0] == counts[1] == counts[2]


async def test_showing_everything_mounts_the_rest_and_drops_the_row():
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(_transcript(30))
        await pilot.pause()
        assert display.elided_count

        await display.show_all_messages()
        await pilot.pause()
        assert len(display.query(MessageBox)) == 60
        assert _box_texts(display)[0] == "q0"
        assert display.elided_count == 0
        assert _fold_rows(display) == []


async def test_showing_everything_is_a_no_op_when_nothing_was_left_out():
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(_transcript(2))
        await pilot.pause()
        await display.show_all_messages()
        await pilot.pause()
        assert len(display.query(MessageBox)) == 4


async def test_a_second_capped_reload_does_not_leave_the_old_row_behind():
    """A stale row would keep claiming a count for a transcript that is gone."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(_transcript(30))
        await pilot.pause()
        await display.reload_messages(_transcript(3))
        await pilot.pause()
        assert _fold_rows(display) == []
        assert display.elided_count == 0


async def test_a_reload_ends_scrolled_to_the_newest_message():
    """The batch holds off layout, so the final scroll runs against sizes that
    only exist once the batch ends. Getting this wrong parks the reader at the
    top of a conversation they resumed to continue."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(_transcript(30), cap=False)
        await pilot.pause()
        assert display.max_scroll_y > 0, "the transcript is taller than the window"
        assert display.scroll_y >= display.max_scroll_y - 0.5


async def test_an_uncapped_reload_costs_a_handful_of_layout_passes_not_one_each():
    """The batch, measured by the thing that costs the time.

    Every awaited mount used to hand control back to the event loop, where the
    screen timer re-arranged the entire tree — 78 to 104 full passes on a
    200-message reload, each over a tree that was still growing. That is the
    quadratic. Counted rather than timed: the count is deterministic and the
    wall-clock is not.
    """
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        layouts = _count_layouts(pilot.app)
        await display.reload_messages(_transcript(50), cap=False)
        await pilot.pause()
        assert len(display.query(MessageBox)) == 100, "it really did mount them all"
        assert layouts[0] < 20, f"{layouts[0]} layout passes for 100 messages"


# ---------------------------------------------------------------------------
# §2c: the live counter on a running exchange.
#
# Before this the exchange title said "Working…" and then nothing on screen
# changed until answer text arrived. A turn spent reasoning, or waiting on a
# slow tool, looked exactly like a turn that had died.
#
# The counter's whole design question is what it may claim. There is NO measured
# token count during a completion: TextDeltaEvent.partial carries an
# AssistantMessage whose usage is all zeros, and a server sends its usage block
# on the final chunk. So the readout has one measured part (`N out`, the sum of
# the per-completion usage this lane has been TOLD, which steps at each tool
# boundary) and one labelled estimate (`~N chunks`, stream events for the
# completion in flight, called chunks because one chunk is not guaranteed to be
# one token). These tests assert that separation, not the wall-clock.
# ---------------------------------------------------------------------------

import time  # noqa: E402


def _exchange_title(display: ChatDisplay) -> str:
    return display.query_one(ExchangeBox).title


def _completion_end(output: int, *, lane: str = DEFAULT_LANE, context: int = 0) -> dict:
    return {"kind": "completion_end", "lane": lane, "output": output, "context": context}


async def test_a_running_exchange_claims_no_tokens_it_has_not_measured():
    """The first completion is in flight: chunks, no `out` part at all.

    Zero is not shown as `0 out`. Nothing has reported, so nothing is claimed —
    the same rule that omits an unknown duration rather than printing 0:00.
    """
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        for _ in range(3):
            await _send(display, pilot, {"kind": "text_delta", "delta": "x"})
        display._tick_live_counters()
        title = _exchange_title(display)
        assert "out" not in title, title
        assert "~3 chunks" in title, title


async def test_a_completion_boundary_turns_the_estimate_into_a_measurement():
    """`out` appears and the chunk count clears: the completion is over."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        for _ in range(5):
            await _send(display, pilot, {"kind": "text_delta", "delta": "x"})
        await _send(display, pilot, _completion_end(1200))
        display._tick_live_counters()
        title = _exchange_title(display)
        assert "1.2k out" in title, title
        assert "chunk" not in title, title


async def test_the_estimate_restarts_for_the_next_completion():
    """A tool-bearing turn: the measured half accumulates, the estimate does not.

    This is the case the counter exists for — the one design that shows a REAL
    number during a long turn, because message_end fires once per completion.
    """
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        for _ in range(5):
            await _send(display, pilot, {"kind": "text_delta", "delta": "x"})
        await _send(display, pilot, _completion_end(400))
        for _ in range(2):
            await _send(display, pilot, {"kind": "text_delta", "delta": "x"})
        display._tick_live_counters()
        title = _exchange_title(display)
        assert "400 out" in title, title
        assert "~2 chunks" in title, title


async def test_a_provider_that_reports_no_usage_makes_no_token_claim():
    """Fail-Early: the boundary is real, the measurement is zero, so `out` is
    omitted rather than printed as a 0 that reads like a working counter."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "text_delta", "delta": "x"})
        await _send(display, pilot, _completion_end(0))
        display._tick_live_counters()
        assert "out" not in _exchange_title(display)


async def test_reasoning_deltas_count_as_chunks_too():
    """A reasoning model that thinks for a minute before answering is exactly
    the silence this counter has to fill."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        for _ in range(4):
            await _send(display, pilot, {"kind": "reasoning_delta", "delta": "t"})
        display._tick_live_counters()
        assert "~4 chunks" in _exchange_title(display)


async def test_the_clock_moves_with_no_events_at_all():
    """The tool-call case: thirty seconds of silence, and the title still moves.

    The elapsed figure is driven off a timer rather than off deltas, which is
    the whole reason a counter fed only by stream events would still go dead
    during the wait that matters most.
    """
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        state = display._lanes[DEFAULT_LANE]
        state.started = time.monotonic() - 12
        display._tick_live_counters()
        assert "0:12" in _exchange_title(display)


async def test_the_counter_runs_only_while_a_lane_is_open():
    """An idle chat pays nothing: the timer is paused outside a turn.

    Reads ``Timer._active``, which is private, because Textual exposes
    ``pause()``/``resume()`` and no way to ask which one last ran. The
    alternative — waiting real seconds and watching for a repaint — would be a
    slow test that is flaky in exactly the case it is meant to catch.
    """
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        assert display._live_timer is not None
        assert display._live_timer._active.is_set() is False, "paused before any turn"
        await display.begin_exchange()
        assert display._live_timer._active.is_set() is True, "running during a turn"
        await display.finalize_exchange(context=10, output=20, seconds=1.0)
        assert display._live_timer._active.is_set() is False, "paused again after it"


async def test_clearing_the_chat_mid_turn_stops_the_counter():
    """The exchanges it was drawing have been removed; leaving it running would
    tick over a lane dict pointing at detached widgets."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await display.clear_messages()
        assert display._live_timer is not None
        assert display._live_timer._active.is_set() is False


async def test_two_lanes_count_separately():
    """One readout per lane, which is why this is on the exchange and not on the
    header subtitle: two concurrent turns have two different answers and the
    subtitle's one line could only report one of them."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange("a")
        await display.begin_exchange("b", label="agent · fork:explore")
        for _ in range(3):
            await _send(display, pilot, {"kind": "text_delta", "delta": "x", "lane": "a"})
        await _send(display, pilot, {"kind": "text_delta", "delta": "y", "lane": "b"})
        display._tick_live_counters()
        titles = [e.title for e in display.query(ExchangeBox)]
        assert any("~3 chunks" in t for t in titles), titles
        assert any("~1 chunk" in t and "chunks" not in t for t in titles), titles


async def test_a_foreign_lane_keeps_its_badge_while_it_runs():
    """B3-b: whose turn this is has to be legible at every moment of it, not
    only in the finished summary."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange("b", label="bus · nats_bus")
        await _send(display, pilot, {"kind": "text_delta", "delta": "x", "lane": "b"})
        display._tick_live_counters()
        assert _exchange_title(display).startswith("bus · nats_bus · Working…")


async def test_the_summary_survives_the_counter():
    """finalize_exchange pops the lane BEFORE it stamps the summary, so a tick
    landing afterwards cannot repaint the finished title back to `Working…`."""
    async with _Harness().run_test() as pilot:
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await _send(
            display, pilot, {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {}}
        )
        await _send(
            display, pilot, {"kind": "tool_result", "id": "c1", "name": "ls", "result": "a.py"}
        )
        await display.finalize_exchange(context=100, output=50, seconds=2.0)
        await pilot.pause()
        display._tick_live_counters()
        await pilot.pause()
        title = _exchange_title(display)
        assert title.startswith("✓ "), title
        assert "Working" not in title, title


# ---------------------------------------------------------------------------
# §2c: the completion boundary on the wire (TurnStream -> render event).
# ---------------------------------------------------------------------------


def _message_end(usage: dict | None) -> _FakeEvent:
    message: dict = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    if usage is not None:
        message["usage"] = usage
    return _FakeEvent(type="message_end", timestamp=0, message=message)


def test_a_completion_boundary_publishes_the_measured_total():
    """The number is the lane's real running sum, not this completion's alone —
    the same figure lane_end reports, published early."""
    from tau_coding_agent.backends import TurnStream

    stream = TurnStream("lane-1")
    first = stream.feed(_message_end({"output_tokens": 30, "input_tokens": 100}))
    assert [e["kind"] for e in first] == ["completion_end"]
    assert first[0] == {
        "kind": "completion_end",
        "lane": "lane-1",
        "output": 30,
        "context": 100,
    }
    second = stream.feed(_message_end({"output_tokens": 12, "input_tokens": 140}))
    assert second[0]["output"] == 42, "summed across completions, like lane_end"
    assert second[0]["context"] == 140, "replaced, not summed — a prompt contains the last one"


def test_the_duplicate_message_end_restates_the_same_total():
    """The agent loop emits message_end twice per tool-bearing turn and only the
    first carries usage. Both mark the same real boundary, so both publish — and
    the second must not double-count."""
    from tau_coding_agent.backends import TurnStream

    stream = TurnStream()
    stream.feed(_message_end({"output_tokens": 30}))
    again = stream.feed(_message_end(None))
    assert again[0]["output"] == 30


def test_a_message_end_with_no_message_publishes_nothing():
    """No message, no boundary — there is nothing to have ended."""
    from tau_coding_agent.backends import TurnStream

    assert TurnStream().feed(_FakeEvent(type="message_end", timestamp=0, message=None)) == []
