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

from tau_coding_agent.app import Parley


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
