"""Part-2 tests: the TUI tree-browser + three-mode subtree compaction (§3.6).

Covers the SessionTreeModal overlay (Textual Pilot: returns the chosen id / None on
cancel) and TauBackend.navigate_tree in both modes — the no-summary ``navigate``
append (drops the abandoned branch from context but not from disk) and the
``summarize`` ``branch_summary`` append (inline splice + custom instructions reaching
the summarizer's SYSTEM prompt), plus the re-render seam.

Reference: SESSION-TREE-IMPLEMENTATION.md §3 (all), §5 Decision 5.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.app import App
from textual.widgets import Tree

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.app import SessionTreeModal
from tau_coding_agent.backends import TauBackend
from tau_coding_agent.session_store import Session


# --- synthetic tree helpers -------------------------------------------------


def _linear_session(tmp_path) -> Session:
    """A→B→C linear session (system + user + assistant), persisted to tmp_path."""
    session = Session.create(
        str(tmp_path), "gpt-4o", "openai", system_prompt="sys", base_dir=tmp_path
    )
    session.append_message({"role": "user", "content": "hello"})
    session.append_message({"role": "assistant", "content": "hi there"})
    return session


# --- SessionTreeModal (Textual Pilot) --------------------------------------


class _ModalHarness(App):
    """Minimal host that pushes one modal and records its dismissal value."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


async def test_tree_modal_enter_returns_current_leaf(tmp_path):
    session = _linear_session(tmp_path)
    roots = ConversationTree(session.entries(), session.cursor).tree()
    harness = _ModalHarness(SessionTreeModal(roots))
    async with harness.run_test() as pilot:
        await pilot.pause()
        # The current leaf is highlighted; Enter selects it.
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == session.cursor


async def test_tree_modal_escape_returns_none(tmp_path):
    session = _linear_session(tmp_path)
    roots = ConversationTree(session.entries(), session.cursor).tree()
    harness = _ModalHarness(SessionTreeModal(roots))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


async def test_tree_modal_navigates_and_selects_interior_node(tmp_path):
    session = _linear_session(tmp_path)
    entries = session.entries()
    # entries: [model_change, message(system?), ...]. Pick the first user message.
    user_id = next(
        e["id"]
        for e in entries
        if e.get("type") == "message" and e["message"].get("role") == "user"
    )
    roots = ConversationTree(entries, session.cursor).tree()
    harness = _ModalHarness(SessionTreeModal(roots))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        # Walk the cursor up to the user node, then Enter.
        for _ in range(len(entries) + 2):
            if tree.cursor_node is not None and tree.cursor_node.data == user_id:
                break
            await pilot.press("up")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == user_id


# --- TauBackend.navigate_tree ----------------------------------------------


def _backend() -> TauBackend:
    return TauBackend(
        {
            "model": "gpt-4o",
            "backend": "openai",
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "tools": [],
        }
    )


def _branch_point(session: Session) -> str:
    """The first user message id — the node we branch from."""
    return next(
        e["id"]
        for e in session.entries()
        if e.get("type") == "message" and e["message"].get("role") == "user"
    )


async def test_navigate_no_summary_appends_navigate_and_drops_branch(tmp_path):
    session = _linear_session(tmp_path)
    # Give the abandoned tip an extra message so navigating back genuinely drops it.
    session.append_message({"role": "user", "content": "abandon me"})
    target = _branch_point(session)
    before_ids = {e["id"] for e in session.entries()}
    old_leaf = session.cursor

    backend = _backend()
    new_messages = await backend.navigate_tree(session, target, summarize=False)

    entries = session.entries()
    navigate_entries = [e for e in entries if e.get("type") == "navigate"]
    assert len(navigate_entries) == 1
    assert navigate_entries[0]["targetId"] == target
    # Cursor moved to the target.
    assert session.cursor == target
    assert session.cursor != old_leaf
    # The abandoned branch is dropped from context but NOT from disk (append-only).
    assert before_ids <= {e["id"] for e in entries}
    texts = [_text(m) for m in new_messages]
    assert "abandon me" not in texts
    assert "hello" in texts  # the branch point survives
    # Return value is exactly ConversationTree.context_for(cursor).
    assert new_messages == ConversationTree(entries, session.cursor).context_for()


async def test_navigate_summarize_appends_branch_summary_inline(tmp_path):
    session = _linear_session(tmp_path)
    session.append_message({"role": "user", "content": "explore this dead end"})
    target = _branch_point(session)

    backend = _backend()
    captured: dict = {}

    async def fake_complete(model, context, options=None):
        captured["context"] = context
        return _fake_assistant("SUMMARY OF THE BRANCH")

    with patch("tau_ai.client.complete_simple", fake_complete):
        new_messages = await backend.navigate_tree(session, target, summarize=True)

    entries = session.entries()
    bs = [e for e in entries if e.get("type") == "branch_summary"]
    assert len(bs) == 1
    assert bs[0]["summary"] == "SUMMARY OF THE BRANCH"
    # Decision 5 fix 1: the summary parents at the branch point (from_id == target).
    assert bs[0]["fromId"] == target
    assert bs[0]["parentId"] == target
    # The summary is spliced INLINE into context (Decision 5 fix 2).
    texts = [_text(m) for m in new_messages]
    assert any("SUMMARY OF THE BRANCH" in t for t in texts)
    assert new_messages == ConversationTree(entries, session.cursor).context_for()


async def test_navigate_summarize_custom_instructions_reach_system_prompt(tmp_path):
    session = _linear_session(tmp_path)
    session.append_message({"role": "user", "content": "explore"})
    target = _branch_point(session)

    backend = _backend()
    captured: dict = {}

    async def fake_complete(model, context, options=None):
        captured["context"] = context
        return _fake_assistant("custom summary")

    with patch("tau_ai.client.complete_simple", fake_complete):
        await backend.navigate_tree(
            session,
            target,
            summarize=True,
            custom_instructions="Only mention file paths.",
        )

    system_msg = captured["context"]["messages"][0]
    assert system_msg["role"] == "system"
    assert "Only mention file paths." in system_msg["content"]


async def test_navigate_summarize_raises_on_empty_llm_response(tmp_path):
    # Fail-Early (§3.1): a failed/empty summary raises — no fabricated fallback.
    session = _linear_session(tmp_path)
    session.append_message({"role": "user", "content": "explore"})
    target = _branch_point(session)
    backend = _backend()

    async def fake_complete(model, context, options=None):
        return _fake_assistant("")

    with patch("tau_ai.client.complete_simple", fake_complete):
        with pytest.raises(RuntimeError, match="empty summary"):
            await backend.navigate_tree(session, target, summarize=True)
    # Nothing persisted for the failed call.
    assert all(e.get("type") != "branch_summary" for e in session.entries())


# --- re-render seam (§3.4) --------------------------------------------------


async def test_reload_messages_shows_post_navigate_context(tmp_path, monkeypatch):
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", lambda cfg: _backend())

    from tau_coding_agent.app import ChatDisplay, Parley

    app = Parley()
    app.config = {
        "models": {"m": {"backend": "openai", "model": "m"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        session = app.current_session
        session.append_message({"role": "user", "content": "keep me"})
        session.append_message({"role": "assistant", "content": "abandon"})
        target = _branch_point(session)

        new_messages = await app.current_backend.navigate_tree(
            session, target, summarize=False
        )
        app.messages = new_messages
        await app.query_one(ChatDisplay).reload_messages(app.messages)
        await pilot.pause()

        assert session.cursor == target
        assert new_messages == ConversationTree(
            session.entries(), session.cursor
        ).context_for()


# --- shared fakes -----------------------------------------------------------


def _text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _fake_assistant(text: str):
    from tau_ai.types import AssistantMessage, TextContent, Usage

    return AssistantMessage(
        content=[TextContent(text=text)] if text else [],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )
