"""Regression tests for the three chat-rendering bugs.

Covers:

1. Widget unification — user / assistant / tool-call / tool-result all render
   via the single ``MessageBox`` widget.
2. Arrival-order streaming — the pending placeholder opened at stream start
   resolves in place, and the assistant's FINAL text ends up LAST (after the
   tool calls it follows), not pinned at the top.
3. No whole-message text duplication — streamed assistant text updates one box
   in place; each assistant text block is one widget populated exactly once,
   even across a multi-turn [text -> tool call -> result -> final text] turn.

These drive the real ``ChatDisplay`` state machine (the bug surface) headlessly
via ``App.run_test()`` (see docs/textual-headless-testing.md), plus a focused
unit test of ``TauBackend``'s agent-event -> structured-event mapping.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from tau_coding_agent.app import ChatDisplay, MessageBox


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


# A realistic one-turn-with-tools event sequence as produced by
# TauBackend.stream_chat's on_event sink:
#   user already on screen, then the assistant loop runs:
#     turn 0: preamble text -> tool call -> (loop continues)
#     tool result arrives
#     turn 1: final answer text  (must land LAST)
def _replay_full_turn(display: ChatDisplay) -> None:
    display.add_message("user", "list the files")

    # turn 0 begins -> pending placeholder opens
    display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
    # preamble assistant text streams in (fragments)
    display.handle_stream_event({"kind": "text_delta", "delta": "Sure, "})
    display.handle_stream_event({"kind": "text_delta", "delta": "let me look."})
    # then a tool call
    display.handle_stream_event(
        {"kind": "tool_call", "id": "c1", "name": "ls", "arguments": {"path": "."}}
    )
    # tool result
    display.handle_stream_event(
        {"kind": "tool_result", "id": "c1", "name": "ls", "result": "a.py\nb.py", "is_error": False}
    )
    # turn 1 begins -> a NEW pending placeholder, resolves to the final answer
    display.handle_stream_event({"kind": "turn_start", "turn_index": 1})
    display.handle_stream_event({"kind": "text_delta", "delta": "There are "})
    display.handle_stream_event({"kind": "text_delta", "delta": "two files."})
    # whole loop done
    display.finalize_turn("12 tokens")


async def test_arrival_order_final_text_last():
    """Bug 2: widgets appear in arrival order; final assistant text is LAST."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        _replay_full_turn(display)
        await pilot.pause()

        roles = _box_roles(display)
        assert roles == [
            "user",
            "assistant",   # preamble text (resolved from the turn-0 pending box)
            "toolCall",
            "toolResult",
            "assistant",   # FINAL answer — last
        ], roles
        # The very last widget is the assistant's final text, not a tool block.
        assert roles[-1] == "assistant"
        assert _box_texts(display)[-1] == "There are two files."


async def test_no_text_duplication():
    """Bug 3: no assistant box contains duplicated/concatenated message text."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        _replay_full_turn(display)
        await pilot.pause()

        texts = _box_texts(display)
        # The two assistant boxes hold exactly their own turn's text — the
        # final-turn text is NOT appended onto the preamble box, and neither
        # text repeats inside a single box.
        assistant_texts = [
            t for b, t in zip(display.query(MessageBox), texts) if b.role == "assistant"
        ]
        assert assistant_texts == ["Sure, let me look.", "There are two files."], assistant_texts
        for t in assistant_texts:
            # No box should contain the same sentence twice.
            assert t.count("There are two files.") <= 1
            assert t.count("Sure, let me look.") <= 1


async def test_all_kinds_share_one_widget_class():
    """Bug 1: every chat entry is the same widget class (MessageBox)."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        _replay_full_turn(display)
        await pilot.pause()

        boxes = list(display.query(MessageBox))
        # user, assistant, toolCall, toolResult, assistant -> 5 boxes
        assert len(boxes) == 5, len(boxes)
        assert all(type(b) is MessageBox for b in boxes)
        # All four distinct kinds are present and rendered by the SAME class.
        assert {"user", "assistant", "toolCall", "toolResult"} <= set(_box_roles(display))


async def test_pending_placeholder_resolves_in_place_for_toolcall():
    """A tool-only turn (no preamble) resolves the pending box in place."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        # No text — straight to a tool call. The pending box becomes the call.
        display.handle_stream_event(
            {"kind": "tool_call", "id": "c1", "name": "read", "arguments": {"file": "x"}}
        )
        await pilot.pause()
        roles = _box_roles(display)
        # Exactly one box, resolved from pending -> toolCall (NOT a leftover
        # empty pending box sitting above it).
        assert roles == ["toolCall"], roles


async def test_no_leftover_pending_box_after_finalize():
    """A turn that opens a pending slot but yields no content leaves nothing."""
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        # ...stream produced nothing renderable, then the loop ended.
        display.finalize_turn("")
        await pilot.pause()
        assert _box_roles(display) == []


async def test_error_tool_result_gets_error_class():
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        display.handle_stream_event(
            {"kind": "tool_result", "id": "c1", "name": "bash", "result": "boom", "is_error": True}
        )
        await pilot.pause()
        box = display.query_one(MessageBox)
        assert box.role == "toolResult"
        assert box.has_class("box-error")


# ---------------------------------------------------------------------------
# Saved-chat reload: persisted block-content must render, not freeze.
#
# Regression for the busy-loop/freeze on clicking a sidebar session: a saved
# assistant/toolResult message stores content as a *list of block dicts*, and
# handing that straight to the str-only MessageBox raised
# `'list' object has no attribute 'replace'` inside compose() — which, fired
# for every message during the mount/layout cycle, manifested as a freeze.
# ---------------------------------------------------------------------------


# The persisted shape of a [text -> tool call -> result -> final text] turn, as
# written to ~/.tau/chats/*.json (assistant content is a block list; toolResult
# is its own role with tool_name/is_error at the message level).
_PERSISTED_TURN = [
    {"role": "user", "content": "list the files"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Sure, let me look."},
        {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"path": "."}},
    ]},
    {"role": "toolResult", "tool_name": "ls", "is_error": False,
     "content": [{"type": "text", "text": "a.py\nb.py"}]},
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
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": "x", "name": "bash", "arguments": {"command": "date"}},
            ]}
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
            {"role": "toolResult", "tool_name": "bash", "is_error": True,
             "content": [{"type": "text", "text": "boom"}]}
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
    """End-to-end contract: a session written by `tau -p` (_save_session) reloads
    cleanly through the renderer — the write format and read renderer agree, which
    is what makes a headless run *resumable* from the TUI."""
    import tau_coding_agent.session_store as store
    from tau_coding_agent.headless import _save_session

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    context = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "run date"},
    ]
    new_messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "date"}},
        ]},
        {"role": "toolResult", "tool_name": "bash", "is_error": False,
         "content": [{"type": "text", "text": "Thu"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "It's Thursday."}]},
    ]
    path = _save_session("local-llm", {"backend": "openai"}, context, new_messages)

    loaded = store.Chat.load(path)
    assert loaded.model == "local-llm"  # resolvable config key -> resumable
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        display = pilot.app.query_one(ChatDisplay)
        for msg in loaded.messages:
            if msg.get("role") != "system":
                display.add_persisted_message(msg)
        await pilot.pause()
        assert _box_roles(display) == [
            "user", "assistant", "toolCall", "toolResult", "assistant",
        ]


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

    def subscribe(self, handler):
        self._handler = handler
        return lambda: None

    async def prompt(self, text, context=None):
        for ev in self._events:
            self._handler(ev)
        return []  # new_messages — irrelevant to this test


async def test_backend_event_to_structured_mapping():
    """tool widgets come from tool_execution_*; duplicate message_end is deduped."""
    from tau_coding_agent.backends import TauBackend

    # Build a backend, then swap in a fake session (no network).
    backend = TauBackend(
        {"model": "m", "backend": "openai", "base_url": "http://x", "api_key": "not-needed",
         "tools": []}
    )

    # Scripted sequence mirroring agent_loop.py for [text -> tool call -> result -> final text]:
    events = [
        _FakeEvent(type="agent_start", timestamp=0),
        _FakeEvent(type="turn_start", timestamp=0, turn_index=0),
        # streaming preamble text: _stream_response re-sends the full accumulated text
        _FakeEvent(type="message_start", timestamp=0,
                   message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}),
        _FakeEvent(type="message_update", timestamp=0,
                   message={"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}),
        _FakeEvent(type="message_update", timestamp=0,
                   message={"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]}),
        # DoneEvent message_end (in _stream_response)
        _FakeEvent(type="message_end", timestamp=0,
                   message={"role": "assistant", "content": [
                       {"type": "text", "text": "Hi there"},
                       {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}},
                   ]}),
        # DUPLICATE message_end (emitted again in run() because tool calls exist)
        _FakeEvent(type="message_end", timestamp=0,
                   message={"role": "assistant", "content": [
                       {"type": "text", "text": "Hi there"},
                       {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}},
                   ]}),
        _FakeEvent(type="tool_execution_start", timestamp=0,
                   tool_call_id="c1", tool_name="ls", args={"p": "."}),
        _FakeEvent(type="tool_execution_end", timestamp=0,
                   tool_call_id="c1", tool_name="ls",
                   result=[{"type": "text", "text": "a.py"}], is_error=False),
        _FakeEvent(type="turn_end", timestamp=0, turn_index=0, tool_results=[]),
        # turn 1: final answer
        _FakeEvent(type="turn_start", timestamp=0, turn_index=1),
        _FakeEvent(type="message_update", timestamp=0,
                   message={"role": "assistant", "content": [{"type": "text", "text": "Done"}]}),
        _FakeEvent(type="message_end", timestamp=0,
                   message={"role": "assistant", "content": [{"type": "text", "text": "Done"}]}),
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
