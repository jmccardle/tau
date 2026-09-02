"""Branch-from-selection, copy and paste, from the browser (§6, §7).

Step 7 of TREE-BROWSER-AS-EDITOR.md's build order — the one the earlier steps were
shaped to make cheap. Three gestures land here:

* ``ctrl+B`` builds a branch out of the marked messages. The marks may have gaps in
  them, which is the shape §6.1 proves an elide cannot express: what an elide
  removes is always a prefix, so keeping ``m1`` while skipping ``m2..m3`` needs
  minted entries.
* ``c`` and ``v`` copy a subtree and re-create it elsewhere. A paste edits the TREE
  and not the context — it never moves the cursor — so the browser re-opens on the
  grown tree rather than returning to a transcript that did not change.

The invariants under test are the same three the elide has, restated for a gesture
that mints: nothing is re-parented (I1 holds because every ``parentId`` is still
written once, at append), nothing is erased, and a refusal leaves the log untouched.

The pure half — which marks are kept and which are copied, and why a selection is
refused — is ``tau-agent-core/tests/test_tree_surgery.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App
from textual.widgets import Static, Tree

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.app import (
    BranchModeModal,
    ChatDisplay,
    Parley,
    SessionTreeModal,
    TreeIntent,
)
from tau_coding_agent.backends import TauBackend


def _backend() -> TauBackend:
    """A real TauBackend — neither commit makes a model call, so no network."""
    return TauBackend(
        {
            "model": "gpt-4o",
            "backend": "openai",
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "tools": [],
        }
    )


@pytest.fixture
def app(make_app):
    return make_app(create_backend=lambda cfg: _backend())


# --- harnesses (the idiom test_tree_elide.py established) -------------------


class _ModalHarness(App):
    """Push one modal, record what it dismisses with."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


def _widget_rows(node):
    for child in node.children:
        yield child
        yield from _widget_rows(child)


async def _goto(harness, pilot, entry_id):
    """Expand every group, then put the cursor on ``entry_id``'s row."""
    tree = harness.screen.query_one("#tree-browser-tree", Tree)
    for node in list(_widget_rows(tree.root)):
        if node.allow_expand:
            node.expand()
    await pilot.pause()
    tree.move_cursor(next(n for n in _widget_rows(tree.root) if n.data == entry_id))
    await pilot.pause()
    return tree


def _script(app: Parley, values: list[Any]) -> list[Any]:
    """Answer the flow's modals from a script; return the screens it pushed."""
    pushed: list[Any] = []
    queue = list(values)

    async def fake_push_screen_wait(screen):
        pushed.append(screen)
        return queue.pop(0)

    app.push_screen_wait = fake_push_screen_wait  # type: ignore[method-assign]
    return pushed


def _notifications(app: Parley) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    original = app.notify

    def fake_notify(message, *args, **kwargs):
        seen.append((str(message), str(kwargs.get("severity", "information"))))
        return original(message, *args, **kwargs)

    app.notify = fake_notify  # type: ignore[method-assign]
    return seen


def _texts(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    return out


def _linear_log():
    """``sys → m0 → m1 → m2 → m3 → m4`` on an in-memory log."""
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "system", "content": "SYS"})
    ids = [log.append_message({"role": "user", "content": f"m{i}"}) for i in range(5)]
    return log, ids


async def _seeded(app: Parley) -> tuple[Any, list[str]]:
    """A fresh chat with three user/assistant pairs; returns the session + ids."""
    await app.action_new_chat()
    session = app.current_session
    ids = [
        session.append_message({"role": "user", "content": "u1"}),
        session.append_message({"role": "assistant", "content": "a1"}),
        session.append_message({"role": "user", "content": "u2"}),
        session.append_message({"role": "assistant", "content": "a2"}),
        session.append_message({"role": "user", "content": "u3"}),
        session.append_message({"role": "assistant", "content": "a3"}),
    ]
    return session, ids


# --- the browser gestures ---------------------------------------------------


async def test_ctrl_b_answers_with_every_marked_id_in_row_order():
    log, ids = _linear_log()
    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        for entry_id in (ids[3], ids[0]):  # marked out of order, on purpose
            await _goto(harness, pilot, entry_id)
            modal.action_toggle_mark()
        await pilot.press("ctrl+b")
        await pilot.pause()
    assert harness.result == TreeIntent("branch", (ids[0], ids[3]))


async def test_ctrl_b_with_nothing_marked_says_so_and_stays_open():
    log, _ids = _linear_log()
    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert harness.result == "UNSET"
    assert said and "Nothing marked" in said[0]


async def test_marking_an_assistant_marks_its_tool_result_too():
    """The select-time pairing: an assistant that made a call and the result
    answering it are one unit, so the rows light up together and a reader cannot
    build the half-selection the commit would have to refuse."""
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "system", "content": "SYS"})
    log.append_message({"role": "user", "content": "u1"})
    call = log.append_message(
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}],
        }
    )
    result = log.append_message(
        {"role": "toolResult", "tool_call_id": "c1", "content": [{"type": "text", "text": "ok"}]}
    )

    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, call)
        modal.action_toggle_mark()
        assert modal._marked == {call, result}
        # …and unmarking either end releases the whole group, or the reader would
        # have to unmark twice to undo one keystroke.
        modal.action_toggle_mark()
        assert modal._marked == set()


async def test_c_paints_the_copied_subtree_and_v_offers_to_paste_it():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "system", "content": "SYS"})
    u1 = log.append_message({"role": "user", "content": "u1"})
    a1 = log.append_message({"role": "assistant", "content": "a1"})
    u2 = log.append_message({"role": "user", "content": "u2"})

    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, a1)
        await pilot.press("c")
        await pilot.pause()
        # The zone is the subtree, which is what a paste would re-create.
        assert modal._copied == a1
        assert modal._copied_zone() == frozenset({a1, u2})
        # The offer appears on a row the copy can legally land on…
        await _goto(harness, pilot, u1)
        readout = harness.screen.query_one("#tree-browser-marks", Static)
        assert "v: paste 2 copied entries" in str(readout.content)
        # …and not on a row inside the copy itself, which cannot take it.
        await _goto(harness, pilot, u2)
        assert "paste" not in str(readout.content)


async def test_v_answers_with_the_copied_node_and_the_target():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    u1 = log.append_message({"role": "user", "content": "u1"})
    a1 = log.append_message({"role": "assistant", "content": "a1"})

    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, a1)
        await pilot.press("c")
        await _goto(harness, pilot, u1)
        await pilot.press("v")
        await pilot.pause()
    assert harness.result == TreeIntent("paste", (a1, u1))


async def test_v_with_nothing_copied_says_what_c_is_for():
    log, ids = _linear_log()
    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        await _goto(harness, pilot, ids[2])
        await pilot.press("v")
        await pilot.pause()
        assert harness.result == "UNSET"
    assert said and "Nothing copied" in said[0]


async def test_a_structural_row_cannot_be_copied():
    log, ids = _linear_log()
    log.append_elide(ids[2], covered_entries=1, covered_tokens=4, agent_spec_id=None)
    elide_id = next(e["id"] for e in log.entries() if e["type"] == "elide")

    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor))
    harness = _ModalHarness(modal)
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        await _goto(harness, pilot, elide_id)
        await pilot.press("c")
        await pilot.pause()
        assert modal._copied is None
    assert said and "cannot be copied" in said[0]


async def test_the_clipboard_can_be_handed_back_when_the_browser_re_opens():
    """How one copy reaches two destinations: the CALLER carries the clipboard
    across the re-open, because the modal owns nothing durable (§11.1)."""
    log, ids = _linear_log()
    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor), copied=ids[1])
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        assert modal._copied == ids[1]


async def test_a_clipboard_naming_a_vanished_entry_is_dropped():
    """Fail-Early's other half: the id is checked against the tree it is handed
    to, so a stale clipboard paints nothing rather than raising on the first
    repaint."""
    log, _ids = _linear_log()
    modal = SessionTreeModal(ConversationTree(log.entries(), log.cursor), copied="gone")
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        assert modal._copied is None


# --- the commits (TauBackend) -----------------------------------------------


def test_commit_branch_keeps_the_prefix_and_mints_the_rest():
    log, ids = _linear_log()
    backend = _backend()

    messages = backend.commit_branch(log, [ids[0], ids[3], ids[4]], drop_context=False)

    assert _texts(messages) == ["SYS", "m0", "m3", "m4"]
    # m0 is used in place; m3 and m4 are copies naming their sources.
    copies = [e for e in log.entries() if e.get("copiedFrom")]
    assert [e["copiedFrom"] for e in copies] == [ids[3], ids[4]]
    # Nothing was re-parented and nothing was erased (I1, T5).
    assert {e["id"] for e in log.entries()} >= set(ids)
    assert log.entries()[4]["parentId"] == ids[2]


def test_commit_branch_with_drop_context_leaves_the_system_prompt_and_the_branch():
    log, ids = _linear_log()
    backend = _backend()

    messages = backend.commit_branch(log, [ids[1], ids[3]], drop_context=True)

    assert _texts(messages) == ["SYS", "m1", "m3"]
    assert [e["type"] for e in log.entries()][-1] == "elide"


def test_commit_branch_mints_nothing_for_a_contiguous_selection():
    """§6.3 case A: the selection is already an ancestor chain, so the branch is a
    cursor move plus an elide. This is why the elide survives §6 — it is the one
    form that preserves every id's identity."""
    log, ids = _linear_log()
    backend = _backend()

    messages = backend.commit_branch(log, ids[2:], drop_context=True)

    assert _texts(messages) == ["SYS", "m2", "m3", "m4"]
    assert [e for e in log.entries() if e.get("copiedFrom")] == []
    # The tip is the elide, appended straight onto the selection's last message —
    # no navigate, because the attach point was already the cursor.
    tip = log.entries()[-1]
    assert tip["type"] == "elide" and tip["parentId"] == ids[4]
    assert log.cursor == tip["id"]


def test_commit_branch_refuses_half_a_tool_call_and_appends_nothing():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "system", "content": "SYS"})
    u1 = log.append_message({"role": "user", "content": "u1"})
    call = log.append_message(
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}],
        }
    )
    log.append_message(
        {"role": "toolResult", "tool_call_id": "c1", "content": [{"type": "text", "text": "ok"}]}
    )
    before = [dict(e) for e in log.entries()]

    with pytest.raises(ValueError, match="unanswered tool call"):
        _backend().commit_branch(log, [u1, call], drop_context=False)

    assert log.entries() == before


def test_paste_subtree_recreates_the_shape_without_moving_the_cursor():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    log.append_message({"role": "system", "content": "SYS"})
    u1 = log.append_message({"role": "user", "content": "u1"})
    a1 = log.append_message({"role": "assistant", "content": "a1"})
    u2 = log.append_message({"role": "user", "content": "u2"})
    fork = log.append_at(a1, "message", {"message": {"role": "user", "content": "u2-alt"}})
    cursor_before = log.cursor
    context_before = ConversationTree(log.entries(), log.cursor).context_for()

    minted = _backend().paste_subtree(log, a1, u1)

    assert len(minted) == 3  # a1 and its two children
    by_id = {e["id"]: e for e in log.entries()}
    assert by_id[minted[0]]["parentId"] == u1
    assert {by_id[m]["parentId"] for m in minted[1:]} == {minted[0]}
    assert [by_id[m]["copiedFrom"] for m in minted] == [a1, u2, fork]
    # The tree grew; the conversation did not move.
    assert log.cursor == cursor_before
    assert ConversationTree(log.entries(), log.cursor).context_for() == context_before


def test_paste_subtree_refuses_to_paste_into_itself():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    a1 = log.append_message({"role": "assistant", "content": "a1"})
    u2 = log.append_message({"role": "user", "content": "u2"})
    before = [dict(e) for e in log.entries()]

    with pytest.raises(ValueError, match="into its own subtree"):
        _backend().paste_subtree(log, a1, u2)

    assert log.entries() == before


# --- the flows (Parley.action_browse_tree) ----------------------------------


async def test_the_branch_flow_asks_for_a_mode_then_re_renders(app, wait_for_workers_settled):
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        notes = _notifications(app)

        pushed = _script(
            app,
            [TreeIntent("branch", (ids[0], ids[4], ids[5])), "only"],
        )
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert [type(screen) for screen in pushed] == [SessionTreeModal, BranchModeModal]
        assert _texts(app.messages)[-3:] == ["u1", "u3", "a3"]
        assert app.messages == ConversationTree(session.entries(), session.cursor).context_for()
        assert app.query_one(ChatDisplay) is not None
        assert any("Branched from 3 marked messages" in m for m, _ in notes)


async def test_cancelling_the_mode_chooser_writes_nothing(app, wait_for_workers_settled):
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        before = [dict(e) for e in session.entries()]

        _script(app, [TreeIntent("branch", (ids[0], ids[4])), None])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert session.entries() == before


async def test_the_paste_flow_re_opens_the_browser_on_the_grown_tree(app, wait_for_workers_settled):
    """A paste changes the tree and not the context, so returning to the
    conversation would show the reader nothing at all. The second browser is opened
    with the clipboard still held, which is what lets one copy reach two places."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        notes = _notifications(app)
        context_before = list(app.messages)

        # u3 (ids[4]) and its answer, copied up under u1 — a target OUTSIDE the
        # copied subtree, which is the only legal direction (a paste into its own
        # subtree would put the copy and the original on one path).
        pushed = _script(
            app,
            [TreeIntent("paste", (ids[4], ids[0])), None],  # paste, then cancel the re-open
        )
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert [type(screen) for screen in pushed] == [SessionTreeModal, SessionTreeModal]
        assert pushed[1]._copied == ids[4]
        assert [e.get("copiedFrom") for e in session.entries() if e.get("copiedFrom")] == [
            ids[4],
            ids[5],
        ]
        assert app.messages == context_before
        assert any("Pasted 2 entries" in m for m, _ in notes)
