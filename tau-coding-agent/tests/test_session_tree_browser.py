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
from tau_coding_agent.app import SessionTreeModal, TreeIntent
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
    tree = ConversationTree(session.entries(), session.cursor)
    harness = _ModalHarness(SessionTreeModal(tree))
    async with harness.run_test() as pilot:
        await pilot.pause()
        # The current leaf is highlighted; Enter selects it.
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == TreeIntent("navigate", (session.cursor,))


async def test_the_modal_answers_with_an_intent_and_not_a_bare_id(tmp_path):
    """§5.3 / §11.1: the return type is an action name plus the ids it applies to.

    Asserted as a TYPE and not only as a value, because the whole point of the
    widening is that a caller can no longer read the answer as "the node". Every
    operation §1.3 lists needs more than an id, and a caller that keeps treating the
    result as a string is the rewrite §5.3 exists to avoid — it would still pass a
    value comparison against ``ids[0]``.
    """
    session = _linear_session(tmp_path)
    tree = ConversationTree(session.entries(), session.cursor)
    harness = _ModalHarness(SessionTreeModal(tree))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    intent = harness.result
    assert isinstance(intent, TreeIntent)
    assert not isinstance(intent, str)
    # The degenerate case §5.3 names: one action, one id.
    assert intent.action == "navigate"
    assert intent.ids == (session.cursor,)
    assert intent.sole_id == session.cursor


async def test_tree_modal_escape_returns_none(tmp_path):
    session = _linear_session(tmp_path)
    tree = ConversationTree(session.entries(), session.cursor)
    harness = _ModalHarness(SessionTreeModal(tree))
    async with harness.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert harness.result is None


async def test_tree_modal_navigates_and_selects_interior_node(tmp_path):
    """Moving the cursor off the leaf and committing names the row it landed on.

    The action is ``revise`` and not ``navigate`` because the row it lands on is a
    USER message (PLAN-0.9.4 §4, item 2). What this test is about is the cursor
    walk and the id — the interior node reached by ``up`` is the node committed —
    so the action moved with the row's kind rather than the test's subject.
    """
    session = _linear_session(tmp_path)
    entries = session.entries()
    # entries: [model_change, message(system?), ...]. Pick the first user message.
    user_id = next(
        e["id"]
        for e in entries
        if e.get("type") == "message" and e["message"].get("role") == "user"
    )
    tree_view = ConversationTree(entries, session.cursor)
    harness = _ModalHarness(SessionTreeModal(tree_view))
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
    assert harness.result == TreeIntent("revise", (user_id,))


async def test_clicking_a_row_selects_it_without_leaving_the_browser(tmp_path):
    """Click selects; only Enter commits (TREE-BROWSER-AS-EDITOR.md §5.1).

    ``Tree._on_click`` sets ``cursor_line`` and then runs ``select_cursor``, which
    posts the same ``NodeSelected`` that ``Enter`` used to arrive by — so the old
    handler dismissed on a click and a reader could not point at a row to read it in
    the detail pane without being thrown out of the browser.

    Both halves are asserted here because either alone is satisfiable by a bug: a
    modal that ignores clicks entirely passes the first, and one that never opened
    passes the second.
    """
    session = _linear_session(tmp_path)
    view = ConversationTree(session.entries(), session.cursor)
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        for _ in range(5):
            await pilot.pause()
        first = tree.root.children[0]
        assert tree.cursor_node is not None and tree.cursor_node.data != first.data, (
            "the modal should open on the current leaf, not on the first row"
        )

        # Click the first row. Textual reports the click through the row's `line`
        # meta, which is what `Tree._on_click` reads; `pilot.click` on the widget
        # with an offset lands on that line.
        await pilot.click(tree, offset=(4, first.line))
        await pilot.pause()

        assert harness.result == "UNSET", "a click dismissed the browser"
        assert tree.cursor_node is first, "a click did not move the selection"

        # Enter is what answers, and it answers with what the click selected.
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == TreeIntent("navigate", (first.data,))


async def test_left_collapses_a_fork_and_then_steps_out_of_it(tmp_path):
    """``left`` folds the branch the cursor is inside (§5.2).

    Unbound in ``Tree.BINDINGS`` (textual 8.2.7: only ``shift+left`` is
    ``cursor_parent``), so the file-tree idiom was simply missing. After §2 the only
    nodes with widget children are forks, which makes this "fold this branch away"
    rather than "hide one message".
    """
    session = _linear_session(tmp_path)
    branch_point = _branch_point(session)
    # A second child of the branch point, so the tree has exactly one fork.
    session.append_at(
        branch_point, "message", {"message": {"role": "assistant", "content": "other"}}
    )
    view = ConversationTree(session.entries(), session.cursor)
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        for _ in range(5):
            await pilot.pause()
        fork = next(n for n in tree.root.children if n.data == branch_point)
        assert fork.children, "the branch point should be a widget parent"

        # The cursor opens on the leaf, which is inside the fork. `left` has nothing
        # to collapse there, so it steps out to the fork itself.
        assert tree.cursor_node is not None and tree.cursor_node.parent is fork
        await pilot.press("left")
        await pilot.pause()
        assert tree.cursor_node is fork

        # Now there IS something to collapse.
        assert fork.is_expanded
        await pilot.press("left")
        await pilot.pause()
        assert not fork.is_expanded
        # …and the browser is still open: collapsing is not choosing.
        assert harness.result == "UNSET"


def test_an_agent_spec_row_names_the_model_and_tools(tmp_path):
    """B1-e: the browser row for an ``agent_spec`` node (W2,
    NODE-ADDRESSABLE-AGENTS.md). It used to read ``customEntry: agent_spec`` —
    the node whose entire purpose is telling a reader WHICH agent produced the
    turns below it, saying nothing about which agent that was.
    """
    session = _linear_session(tmp_path)
    spec_id = session.append_custom_entry(
        "agent_spec",
        {
            "model": {"id": "gpt-4o", "provider": "openai", "context_window": 128000},
            "system_prompt_digest": "abc123",
            "tools": ["read", "grep"],
            "extensions": [],
            "cwd": str(tmp_path),
        },
    )
    nodes: dict[str, object] = {}

    def _collect(node) -> None:
        nodes[node.id] = node
        for child in node.children:
            _collect(child)

    for root in ConversationTree(session.entries(), session.cursor).tree():
        _collect(root)

    label = SessionTreeModal._label(nodes[spec_id])
    assert "gpt-4o" in label and "read, grep" in label


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

    with patch("tau_llm.client.complete_simple", fake_complete):
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

    with patch("tau_llm.client.complete_simple", fake_complete):
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

    with patch("tau_llm.client.complete_simple", fake_complete):
        with pytest.raises(RuntimeError, match="empty summary"):
            await backend.navigate_tree(session, target, summarize=True)
    # Nothing persisted for the failed call.
    assert all(e.get("type") != "branch_summary" for e in session.entries())


# --- re-render seam (§3.4) --------------------------------------------------


async def test_reload_messages_shows_post_navigate_context(make_app):
    from tau_coding_agent.app import ChatDisplay

    app = make_app(create_backend=lambda cfg: _backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        session = app.current_session
        session.append_message({"role": "user", "content": "keep me"})
        session.append_message({"role": "assistant", "content": "abandon"})
        target = _branch_point(session)

        new_messages = await app.current_backend.navigate_tree(session, target, summarize=False)
        app.messages = new_messages
        await app.query_one(ChatDisplay).reload_messages(app.messages)
        await pilot.pause()

        assert session.cursor == target
        assert new_messages == ConversationTree(session.entries(), session.cursor).context_for()


# --- shared fakes -----------------------------------------------------------


def _text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _fake_assistant(text: str):
    from tau_llm.types import AssistantMessage, TextContent, Usage

    return AssistantMessage(
        content=[TextContent(text=text)] if text else [],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


# ---------------------------------------------------------------------------
# PLAN-0.9.4 §4 item 2: Enter means something different on a user message.
#
# The SHAPE rules that go with these (turn groups, hidden `navigate` rows) are
# tested against the pure planner in test_tui_appearance.py; these two are about
# what the browser answers with, which needs the dismissal.
# ---------------------------------------------------------------------------


def _forked_tree():
    """One answer with two follow-ups tried under it, and the `navigate` between."""
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    ids = {}
    ids["q0"] = log.append_message({"role": "user", "content": "read /tmp/context_test"})
    ids["a0"] = log.append_message({"role": "assistant", "content": "No such file. Create one?"})
    ids["u1"] = log.append_message({"role": "user", "content": "Yes, write your favorite number."})
    ids["a1"] = log.append_message({"role": "assistant", "content": "Wrote `42`."})
    log.append_navigate(ids["a0"])
    ids["u2"] = log.append_message({"role": "user", "content": "Actually, check again!"})
    ids["a2"] = log.append_message({"role": "assistant", "content": "Whoops, it contains `42`."})
    return ConversationTree(log.entries(), log.cursor), ids


def _rows_of(tree_widget):
    def walk(node):
        for child in node.children:
            yield child
            yield from walk(child)

    return list(walk(tree_widget.root))


async def test_enter_on_an_assistant_row_still_says_navigate():
    """Continuing from BELOW a node is right for an agent or tool row — that is
    where the next turn goes. Only a user message means the other side."""
    view, ids = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        # `a1` is inside a turn group the cursor is not in, so it mounts collapsed
        # and `move_cursor` on an unlaid-out row does nothing. Open it first —
        # which is the gesture a reader performs to reach the same row.
        next(n for n in _rows_of(tree) if n.data == ids["u1"]).expand()
        await pilot.pause()
        tree.move_cursor(next(n for n in _rows_of(tree) if n.data == ids["a1"]))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == TreeIntent("navigate", (ids["a1"],))


async def test_enter_on_a_user_row_says_revise_and_still_names_that_row():
    """The id is the node POINTED AT, not its parent.

    The modal reports what was named and the CALLER knows the question (§5.3 /
    §11.1) — the elide flow asks a different question of this same browser and
    would be wrong if the parent were resolved here. Where the fork actually
    happens is ``Parley.action_browse_tree``, which reads the action.
    """
    view, ids = _forked_tree()
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", Tree)
        tree.move_cursor(next(n for n in _rows_of(tree) if n.data == ids["u2"]))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert harness.result == TreeIntent("revise", (ids["u2"],))


def test_message_text_gives_the_whole_message_and_the_preview_gives_one_line():
    """What ``revise`` prefills the input with. ``TreeNode.preview`` is the first
    line elided to a row; handing that back would return a truncated version of
    what the reader typed."""
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    typed = "first line\nsecond line\nthird line"
    entry_id = log.append_message({"role": "user", "content": typed})
    view = ConversationTree(log.entries(), log.cursor)
    assert view.message_text(entry_id) == typed
    node = next(n for n in view.tree() if n.id == entry_id)
    assert node.preview == "first line"
    # An entry with no message is a record, not text — and asking is not an error.
    nav = log.append_navigate(entry_id)
    assert ConversationTree(log.entries(), log.cursor).message_text(nav) == ""


# ---------------------------------------------------------------------------
# PLAN-0.9.4 §4 item 2, the caller's half: what `Parley.action_browse_tree` does
# with a `revise` intent. The modal names the user message; the fork happens here.
# ---------------------------------------------------------------------------


def _script_modals(app, values):
    """Answer the flow's modals from a script (same seam ``test_tree_elide`` uses).

    ``action_browse_tree`` is a worker whose every step is a ``push_screen_wait``,
    so scripting that one call drives the REAL action — the parent lookup, the
    append, the re-render and the prefill — without fighting the Tree cursor.
    """
    queue = list(values)

    async def fake_push_screen_wait(screen):
        return queue.pop(0)

    app.push_screen_wait = fake_push_screen_wait
    return queue


async def test_revising_a_user_message_forks_from_its_parent(make_app, wait_for_workers_settled):
    """The whole of item 2. Navigating ONTO the user message would make the next
    turn its child — two user turns in a row. The cursor lands on its parent
    instead, so what is typed next replaces it."""
    from tau_coding_agent.app import ChatInput

    app = make_app(create_backend=lambda cfg: _backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        session = app.current_session
        session.append_message({"role": "user", "content": "u1"})
        a1 = session.append_message({"role": "assistant", "content": "a1"})
        u2 = session.append_message({"role": "user", "content": "u2 as first typed"})
        session.append_message({"role": "assistant", "content": "a2"})

        _script_modals(app, [TreeIntent("revise", (u2,)), "navigate"])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        # The cursor moved to u2's PARENT, not to u2.
        assert session.entries()[-1]["type"] == "navigate"
        assert session.entries()[-1]["targetId"] == a1
        assert session.cursor == a1
        # …so the revised message is out of the fold, along with what it produced.
        kept = [
            e
            for e in ConversationTree(session.entries(), session.cursor).context_entries()
            if e.get("type") == "message"
        ]
        assert "u2 as first typed" not in [str(e["message"].get("content")) for e in kept]
        # …and it is back in the input, whole, ready to edit.
        assert app.query_one("#chat-input", ChatInput).text == "u2 as first typed"


async def test_navigating_to_an_assistant_message_leaves_the_input_alone(
    make_app, wait_for_workers_settled
):
    """The unchanged half. A ``navigate`` intent continues from below the node and
    stages nothing — putting an answer in the input would be putting words in the
    reader's mouth."""
    from tau_coding_agent.app import ChatInput

    app = make_app(create_backend=lambda cfg: _backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        session = app.current_session
        session.append_message({"role": "user", "content": "u1"})
        a1 = session.append_message({"role": "assistant", "content": "a1"})
        session.append_message({"role": "user", "content": "u2"})
        session.append_message({"role": "assistant", "content": "a2"})
        app.query_one("#chat-input", ChatInput).text = "half a thought"

        _script_modals(app, [TreeIntent("navigate", (a1,)), "navigate"])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert session.cursor == a1
        assert app.query_one("#chat-input", ChatInput).text == "half a thought"


async def test_revising_the_very_first_message_is_refused_and_says_why(
    make_app, wait_for_workers_settled
):
    """Fail-Early. The first entry has no parent to fork from, and the fallback
    that suggests itself — navigate onto it instead — is the exact gesture this
    action exists to stop making.

    Driven by handing the action a ``revise`` intent directly. A ``Session``
    always opens with a ``model_change``, so its root is never a user message and
    the browser would not emit ``revise`` for it — but a log the browser can be
    handed does have a parentless user message (an SDK-driven
    ``InMemorySessionLog`` starts with whatever was appended first), which is why
    the caller checks rather than assuming.
    """
    from tau_coding_agent.app import ChatInput

    app = make_app(create_backend=lambda cfg: _backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        session = app.current_session
        root = session.entries()[0]["id"]
        assert session.entries()[0].get("parentId") is None
        session.append_message({"role": "user", "content": "u1"})
        before = len(session.entries())

        said: list[str] = []
        app.notify = lambda message, *a, **k: said.append(str(message))
        _script_modals(app, [TreeIntent("revise", (root,))])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert len(session.entries()) == before, "nothing was appended"
        assert app.query_one("#chat-input", ChatInput).text == ""
        assert any("first message" in message for message in said), said
