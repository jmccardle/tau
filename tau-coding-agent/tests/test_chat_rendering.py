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

    The live loop is network-paced: every agent event is separated by an await
    (a stream chunk or a tool execution), so Textual settles pending mounts
    between events. Pausing here mirrors that cadence — without it the test
    would fire a synchronous burst the real backend never produces.
    """
    await display.handle_stream_event(event)
    await pilot.pause()


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

    await display.finalize_exchange(tokens=12, seconds=6)


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
        assert "1 tool" in exchange.title and "tok" in exchange.title

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
        await display.finalize_exchange(tokens=3, seconds=1)
        await pilot.pause()

        assert list(display.query(ExchangeBox)) == []
        assert _box_roles(display) == ["user", "assistant"]
        answer = list(display.query(MessageBox))[-1]
        assert answer.content_text == "hello"
        # Real token + duration are surfaced on the answer (no summary line here).
        assert answer._subtitle == "3 tok · 0:01"


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

        await display.finalize_exchange(tokens=5, seconds=1)
        await pilot.pause()

        # No tools -> unwrapped to a single answer box that carries the reasoning.
        assert list(display.query(ExchangeBox)) == []
        answer = display.query_one(MessageBox)
        assert answer.content_text == "The answer is 4."
        assert answer.reasoning is not None
        assert answer.reasoning.text == "Let me think. 2+2=4."
        assert answer.reasoning.collapsed is True


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

        await display.finalize_exchange(tokens=5, seconds=1)
        await pilot.pause()


async def test_empty_terminal_turn_leaves_nothing():
    """A turn that streams nothing renderable then ends leaves no placeholder."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.begin_exchange()
        await _send(display, pilot, {"kind": "turn_start", "turn_index": 0})
        await display.finalize_exchange(tokens=0, seconds=0)
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
        await display.finalize_exchange(tokens=4, seconds=1)
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
        assert "1 tool" in exchange.title and "tok" in exchange.title
        # No fabricated duration on reload — the title has no 'M:SS' segment.
        assert ":" not in exchange.title

        step_boxes = list(exchange.query(MessageBox))
        assert len(step_boxes) == 1
        assert step_boxes[0].content_text == "Sure, let me look."
        tools = list(step_boxes[0].tool_boxes.values())
        assert len(tools) == 1 and tools[0].has_result is True


async def test_reload_aggregates_tokens_across_span():
    """The exchange summary sums each completion's persisted usage.total_tokens."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "usage": {"total_tokens": 30},
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
            "usage": {"total_tokens": 12},
            "content": [{"type": "text", "text": "done"}],
        },
    ]
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        await display.reload_messages(messages)
        await pilot.pause()
        exchange = display.query_one(ExchangeBox)
        assert "42 tok" in exchange.title  # 30 + 12, summed across the span


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
