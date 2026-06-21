"""Tests for Parley app-level action / widget wiring (distinct from chat
rendering).

Regression for the "+ New Chat" sidebar button doing nothing: its handler was a
*sync* ``on_button_pressed`` that called the *async* ``action_new_chat()``
without awaiting it, so the coroutine was created and silently discarded
(Python even warned ``coroutine 'Parley.action_new_chat' was never awaited``).

Driven through the real app via ``App.run_test()`` / Pilot.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.app import ChatDisplay, Parley
from tau_coding_agent.chat_widgets import ReasoningRegion, ToolBox


# A reloaded transcript with reasoning + a tool call/result + a final answer,
# used to exercise the global fold toggles and the conversation rollup.
_RELOAD = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "usage": {"total_tokens": 30}, "content": [
        {"type": "thinking", "thinking": "let me look"},
        {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}},
    ]},
    {"role": "toolResult", "tool_call_id": "c1", "tool_name": "ls",
     "is_error": False, "content": [{"type": "text", "text": "a.py"}]},
    {"role": "assistant", "usage": {"total_tokens": 12}, "content": [
        {"type": "thinking", "thinking": "done"},
        {"type": "text", "text": "one file"},
    ]},
]


@pytest.fixture
def app(monkeypatch, tmp_path):
    # Sandbox session persistence (Chat.save reads session_store.TAU_DIR) and
    # avoid building a real backend (no network).
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", lambda cfg: object())

    a = Parley()
    # Controlled config so the test doesn't depend on ~/.tau/config.json.
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_new_chat_button_creates_chat(app, tmp_path):
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_chat is None

        await pilot.click("#new-chat-button")
        await pilot.pause()

        # The async action actually ran: a chat is active, seeded with the
        # system prompt, and persisted to the (sandboxed) chats dir.
        assert app.current_chat is not None
        assert app.current_chat.model == "m"
        assert app.current_chat.messages[0] == {"role": "system", "content": "sys"}
        assert len(list((tmp_path / "chats").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# #6 — global thinking/tool-output toggles + conversation rollup.
# ---------------------------------------------------------------------------


async def _reload(app, pilot) -> ChatDisplay:
    """Reload a known transcript into the display and return it."""
    await app.action_new_chat()
    display = app.query_one(ChatDisplay)
    await display.reload_messages(_RELOAD)
    await pilot.pause()
    return display


async def test_toggle_reasoning_folds_all_regions(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        regions = list(app.query(ReasoningRegion))
        # One reasoning region in the tool step, one on the promoted answer.
        assert len(regions) == 2 and all(r.collapsed for r in regions)  # reload folds them

        # All folded -> first toggle expands all.
        app.action_toggle_reasoning()
        await pilot.pause()
        assert all(not r.collapsed for r in app.query(ReasoningRegion))
        assert app.reasoning_collapsed is False

        # Any expanded -> next toggle collapses all.
        app.action_toggle_reasoning()
        await pilot.pause()
        assert all(r.collapsed for r in app.query(ReasoningRegion))
        assert app.reasoning_collapsed is True


async def test_toggle_tools_folds_all_boxes(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        boxes = list(app.query(ToolBox))
        assert len(boxes) == 1 and all(b.collapsed for b in boxes)  # default collapsed

        app.action_toggle_tools()  # collapsed -> expand all
        await pilot.pause()
        assert all(not b.collapsed for b in app.query(ToolBox))

        app.action_toggle_tools()  # expanded -> collapse all
        await pilot.pause()
        assert all(b.collapsed for b in app.query(ToolBox))


async def test_toggle_with_no_widgets_is_noop(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()  # empty display, no regions/boxes
        app.action_toggle_reasoning()
        app.action_toggle_tools()
        await pilot.pause()
        assert app.reasoning_collapsed is False
        assert app.tools_collapsed is False


def test_aggregate_label_rolls_up_tools_and_tokens():
    # Pure function: 1 tool call total, 30 + 12 = 42 tokens.
    assert Parley._aggregate_label(_RELOAD) == "1 tool · 42 tok"
    # Plural tools and the k-formatting share the widget helpers.
    many = [
        {"role": "assistant", "usage": {"total_tokens": 2500}, "content": [
            {"type": "toolCall", "id": "a", "name": "ls", "arguments": {}},
            {"type": "toolCall", "id": "b", "name": "cat", "arguments": {}},
        ]},
    ]
    assert Parley._aggregate_label(many) == "2 tools · 2.5k tok"
    # Nothing to roll up yet -> empty (subtitle then shows just the model).
    assert Parley._aggregate_label([{"role": "user", "content": "hi"}]) == ""


async def test_subtitle_shows_rollup_after_reload(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        await _reload(app, pilot)
        # The real reload path sets current_chat to the loaded chat; mirror that
        # so the rollup (derived from current_chat.messages) has the transcript.
        app.current_chat.messages = _RELOAD
        app._refresh_subtitle()
        await pilot.pause()
        assert app.sub_title == "m · 1 tool · 42 tok"
