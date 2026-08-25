"""B1-d: creating an ``elide`` span from the TUI tree browser (W3).

W3 landed the ``elide`` node in the core — the summary-less generalization of the
compaction splice anchor — with no way to create one from the TUI, so a human could
not use it at all. These tests drive it end to end at the action level
(``Parley.action_browse_tree`` → an ``elide`` :class:`TreeIntent` →
``TauBackend.elide_span``), and pin the two invariants that make it safe:

* the resulting context skips **exactly** the elided span, and
* ``entries()`` still contains every id ever minted (Decision 7 / T5, restated at the
  TUI level — an elide hides a span from a fold, never from the log).

Plus the two ways the flow must refuse rather than persist something incoherent: a
resume point that is not on the anchor's path (which would collapse the context to
EMPTY, silently) and a span that would hide nothing.

**The elide is one gesture inside the browser now** (PLAN-0.9.4 §4). It was a mode
button that re-opened this same browser for a second node pick, and an illegal pair
was reported after both screens had closed. ``ctrl+E`` reads the cursor and the
marked node, refuses an illegal pair while the reader can still see the tree
(``SessionTreeModal`` section below), and dismisses with both ids. The backend's own
refusals are still exercised, from a hand-built intent — which is not a synthetic
case: the modal checks against a tree built when it opened, and the session is live
underneath it.

Reference: NODE-ADDRESSABLE-AGENTS.md W3 / T3 / T5, Decision 2 and 7;
SESSION-TREE-IMPLEMENTATION.md §3 (the browser + the §3.4 re-render seam).
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App
from textual.widgets import Button, Static

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.app import (
    ChatDisplay,
    Parley,
    SessionTreeModal,
    TreeIntent,
    TreeModeModal,
)
from tau_coding_agent.backends import TauBackend


def _backend() -> TauBackend:
    """A real TauBackend — ``elide_span`` makes no model call, so no network."""
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
    """The shared ``make_app``, with a real backend (needed for elide_span)."""
    return make_app(create_backend=lambda cfg: _backend())


def _script(app: Parley, values: list[Any]) -> list[Any]:
    """Answer the flow's modals from a script; return the screens it pushed.

    ``action_browse_tree`` is a worker whose every step is a ``push_screen_wait``;
    scripting that one seam drives the REAL action (validation, appends, re-render,
    notifications) without fighting the Tree widget's cursor. The pushed screens are
    returned so a test can assert WHICH browser was shown for the second pick.
    """
    pushed: list[Any] = []
    queue = list(values)

    async def fake_push_screen_wait(screen):
        pushed.append(screen)
        return queue.pop(0)

    app.push_screen_wait = fake_push_screen_wait  # type: ignore[method-assign]
    return pushed


def _pick(node_id: str) -> TreeIntent:
    """What :class:`SessionTreeModal` answers a node pick with (§5.3 / §11.1).

    Spelled out at every call site rather than wrapped inside :func:`_script`,
    because the flow under test is exactly the part that had to learn the wider
    return type: a script that silently boxed a bare id would keep passing against
    an ``action_browse_tree`` that still read one.
    """
    return TreeIntent("navigate", (node_id,))


def _elide(anchor: str, first_kept: str) -> TreeIntent:
    """What ``ctrl+E`` in the browser answers with: both ends, in that order.

    ``anchor`` is where the fold jumps FROM and the conversation continues;
    ``first_kept`` is the oldest entry it keeps. :meth:`SessionTreeModal.action_elide`
    is what decides which of the two nodes the reader named is which — see the
    modal-level tests below.
    """
    return TreeIntent("elide", (anchor, first_kept))


def _notifications(app: Parley) -> list[tuple[str, str]]:
    """Record ``notify`` calls as ``(message, severity)``."""
    seen: list[tuple[str, str]] = []
    original = app.notify

    def fake_notify(message, *args, **kwargs):
        seen.append((str(message), str(kwargs.get("severity", "information"))))
        return original(message, *args, **kwargs)

    app.notify = fake_notify  # type: ignore[method-assign]
    return seen


async def _seeded(app: Parley) -> tuple[Any, list[str]]:
    """A fresh chat with six appended messages; returns the session + their ids."""
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


def _kept_ids(session) -> list[str]:
    return [e["id"] for e in ConversationTree(session.entries(), session.cursor).context_entries()]


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


# --- the happy path ---------------------------------------------------------


async def test_elide_action_skips_exactly_the_span_and_keeps_every_entry(
    app, wait_for_workers_settled
):
    """The whole point, end to end: one ``elide`` intent naming both ends, and the
    transcript loses precisely the entries before the resume point — while the log
    keeps all of them (T5)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        before_ids = {e["id"] for e in session.entries()}
        anchor = session.cursor  # the tip (a3) — fold the history behind it
        resume = ids[4]  # u3

        pushed = _script(app, [_elide(anchor, resume)])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        # Exactly one elide entry, anchored where we said, and NO navigate: the
        # anchor was already the cursor.
        elides = [e for e in session.entries() if e.get("type") == "elide"]
        assert len(elides) == 1
        assert elides[0]["firstKeptId"] == resume
        assert elides[0]["parentId"] == anchor
        assert [e for e in session.entries() if e.get("type") == "navigate"] == []

        # The context skips EXACTLY the span: the anchor plus u3, a3 — nothing else.
        assert _kept_ids(session) == [elides[0]["id"], ids[4], ids[5]]
        assert _texts(app.messages) == ["u3", "a3"]

        # T5: every id ever minted is still in entries().
        assert before_ids <= {e["id"] for e in session.entries()}
        # …including every elided one, by name.
        assert set(ids) <= {e["id"] for e in session.entries()}

        # §3.4 re-render seam: the working list IS the new fold, and the display was
        # reloaded from it.
        assert app.messages == ConversationTree(session.entries(), session.cursor).context_for()
        assert app.query_one(ChatDisplay) is not None

        # ONE screen. No mode chooser (an elide was never one of the three branch
        # modes) and no second browser (the intent already carries both ends).
        assert [type(screen) for screen in pushed] == [SessionTreeModal]


async def test_elide_at_an_interior_anchor_navigates_first(app, wait_for_workers_settled):
    """An anchor that is not the tip needs the leaf moved onto it before the elide
    can parent there — the same ``navigate`` append the "no summary" mode makes."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        anchor = ids[3]  # a2 — an interior node, not the cursor
        resume = ids[2]  # u2, its parent

        _script(app, [_elide(anchor, resume)])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        navigates = [e for e in session.entries() if e.get("type") == "navigate"]
        elides = [e for e in session.entries() if e.get("type") == "elide"]
        assert [n["targetId"] for n in navigates] == [anchor]
        assert elides[0]["parentId"] == anchor
        # Kept: the anchor's line from u2 through a2. u3/a3 are off the new path
        # (the cursor moved back), u1/a1 and the system prompt are elided.
        assert _kept_ids(session) == [elides[0]["id"], ids[2], ids[3]]
        assert _texts(app.messages) == ["u2", "a2"]


async def test_elide_anchored_at_the_current_cursor_is_not_refused(app, wait_for_workers_settled):
    """ "Already at that node" is a statement about BRANCHING, and eliding behind the
    current tip is the normal case, not an error. The elide returns before that
    guard is reached; this pins that it stays that way."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        notes = _notifications(app)

        _script(app, [_elide(session.cursor, ids[3])])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert "Already at that node" not in [m for m, _ in notes]
        assert len([e for e in session.entries() if e.get("type") == "elide"]) == 1


# --- the refusals -----------------------------------------------------------


async def test_elide_with_unreachable_resume_point_notifies_and_appends_nothing(
    app, wait_for_workers_settled
):
    """The corruption case: a resume point that is a DESCENDANT of the anchor is
    never found by the fold's forward scan over the anchor's ancestors, so the whole
    context would silently come back EMPTY. It must be refused before any append."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        entries_before = session.entries()
        messages_before = list(app.messages)
        notes = _notifications(app)

        # anchor a1, resume a3 (a descendant). `SessionTreeModal` refuses this pair
        # by name and never dismisses with it, so the intent is built by hand — the
        # reachable version of this is a pair that went stale under a live session.
        _script(app, [_elide(ids[1], ids[5])])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        errors = [m for m, sev in notes if sev == "error"]
        assert len(errors) == 1
        assert "not on the path to anchor" in errors[0]

        # Nothing was appended — not the elide, and not the navigate either.
        assert session.entries() == entries_before
        assert not any(e.get("type") in ("elide", "navigate") for e in session.entries())
        # …and the rendered transcript is untouched.
        assert app.messages == messages_before


async def test_elide_that_would_hide_nothing_is_refused(app, wait_for_workers_settled):
    """A resume point that is already the first entry the fold keeps produces a node
    that changes nothing — the silent no-op, indistinguishable from a real fold."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, _ids = await _seeded(app)
        first_kept = _kept_ids(session)[0]
        notes = _notifications(app)

        _script(app, [_elide(session.cursor, first_kept)])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        errors = [m for m, sev in notes if sev == "error"]
        assert len(errors) == 1 and "would hide nothing" in errors[0]
        assert not any(e.get("type") == "elide" for e in session.entries())


async def test_cancelling_the_browser_appends_nothing(app, wait_for_workers_settled):
    async with app.run_test() as pilot:
        await pilot.pause()
        session, _ids = await _seeded(app)
        entries_before = session.entries()

        _script(app, [None])  # Esc on the browser
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert session.entries() == entries_before


async def test_backend_without_elide_span_warns(app, monkeypatch, wait_for_workers_settled):
    """The getattr capability gate, matching ``navigate_tree``'s. A backend that has
    ``navigate_tree`` but not ``elide_span`` says so instead of raising."""

    class _PartialBackend:
        async def navigate_tree(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("not the path under test")

    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        notes = _notifications(app)
        app.current_backend = _PartialBackend()

        _script(app, [_elide(session.cursor, ids[4])])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        assert [m for m, sev in notes if sev == "warning"] == [
            "This backend does not support eliding"
        ]
        assert not any(e.get("type") == "elide" for e in session.entries())


# --- direct backend checks (no Textual) -------------------------------------


def _linear_log():
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    ids = [log.append_message({"role": "user", "content": f"m{i}"}) for i in range(5)]
    return log, ids


def test_elide_span_rejects_unknown_ids():
    log, ids = _linear_log()
    backend = _backend()

    with pytest.raises(ValueError, match="elide anchor 'nope' not found"):
        backend.elide_span(log, "nope", ids[2])
    with pytest.raises(ValueError, match="elide resume point 'nope' not found"):
        backend.elide_span(log, ids[3], "nope")
    assert not any(e.get("type") in ("elide", "navigate") for e in log.entries())


def test_elide_span_accepts_the_anchor_itself_as_the_resume_point():
    """The degenerate-but-coherent end of the range: keep only the anchor."""
    log, ids = _linear_log()
    backend = _backend()

    messages = backend.elide_span(log, ids[4], ids[4])

    assert _texts(messages) == ["m4"]
    assert {e["id"] for e in log.entries()} >= set(ids)


def test_elide_span_returns_the_same_fold_the_tree_computes():
    log, ids = _linear_log()
    backend = _backend()

    messages = backend.elide_span(log, log.cursor, ids[3])

    assert messages == ConversationTree(log.entries(), log.cursor).context_for()


# --- the browser row for an existing elide ----------------------------------


def test_elide_node_preview_names_the_hidden_span():
    """An ``elide`` carries no summary, so without this it renders as a bare
    ``(elide)`` and is illegible in the browser."""
    log, ids = _linear_log()
    # §11.3's provenance keywords: what this elide covers, recorded at the write.
    # m0, m1, m2 at one estimated token each ("m0" is two characters).
    log.append_elide(ids[3], covered_entries=3, covered_tokens=3, agent_spec_id=None)

    nodes = {n.id: n for n in _flatten(ConversationTree(log.entries(), log.cursor).tree())}
    elide_id = next(e["id"] for e in log.entries() if e["type"] == "elide")
    preview = nodes[elide_id].preview

    # m0, m1, m2 are hidden; the fold resumes at m3.
    assert preview == f"hides 3 entries, resumes at {ids[3]}"
    # And the browser row is no longer a bare "(elide)".
    assert SessionTreeModal._label(nodes[elide_id]).startswith("elide: hides 3 entries")


def test_elide_node_preview_reports_an_unreachable_boundary():
    """A hand-written log CAN hold the boundary this TUI flow refuses to create; the
    row says what that node actually does (keep nothing) rather than counting it."""
    log, ids = _linear_log()
    log.append_navigate(ids[1])
    # ids[4] is a descendant of the anchor: unreachable, so the span covers nothing
    # — which is what the recorded provenance says too.
    log.append_elide(ids[4], covered_entries=0, covered_tokens=0, agent_spec_id=None)

    nodes = {n.id: n for n in _flatten(ConversationTree(log.entries(), log.cursor).tree())}
    elide_id = next(e["id"] for e in log.entries() if e["type"] == "elide")

    assert "not on this path" in nodes[elide_id].preview
    # …which is exactly what the fold does with it.
    assert ConversationTree(log.entries(), log.cursor).context_for() == []


def test_singular_entry_in_the_preview():
    log, ids = _linear_log()
    # Resuming at m1 hides m0 alone: one entry, one estimated token.
    log.append_elide(ids[1], covered_entries=1, covered_tokens=1, agent_spec_id=None)
    nodes = {n.id: n for n in _flatten(ConversationTree(log.entries(), log.cursor).tree())}
    elide_id = next(e["id"] for e in log.entries() if e["type"] == "elide")
    assert nodes[elide_id].preview == f"hides 1 entry, resumes at {ids[1]}"


def _flatten(roots):
    out = []
    stack = list(roots)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


# --- the modals themselves (Textual Pilot) ----------------------------------


class _ModalHarness(App):
    """test_session_tree_browser's harness: push one modal, record its dismissal."""

    def __init__(self, modal) -> None:
        super().__init__()
        self._modal = modal
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._store)

    def _store(self, value) -> None:
        self.result = value


async def test_the_mode_chooser_no_longer_offers_an_elide():
    """It offered one on every node, whether or not that node could anchor a fold,
    and choosing it re-opened the whole browser to ask for the second end
    (PLAN-0.9.4 §4). The gesture moved into the browser; the button is gone, and so
    is the mode string — a chooser that still answered ``"elide"`` would reach an
    ``action_browse_tree`` that no longer has a branch for it."""
    from textual.css.query import NoMatches

    harness = _ModalHarness(TreeModeModal())
    async with harness.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            harness.screen.query_one("#mode-elide", Button)
        assert [b.id for b in harness.screen.query(Button)] == [
            "mode-navigate",
            "mode-summarize",
            "mode-custom",
            "mode-cancel",
        ]


async def test_tree_modal_shows_the_caption_it_was_given():
    log, _ids = _linear_log()
    view = ConversationTree(log.entries(), log.cursor)
    modal = SessionTreeModal(
        view,
        title="Elide: pick the resume point",
        help_text="H",
    )
    harness = _ModalHarness(modal)
    async with harness.run_test() as pilot:
        await pilot.pause()
        title = harness.screen.query_one("#tree-browser-title", Static)
        help_text = harness.screen.query_one("#tree-browser-help", Static)
        assert str(title.content) == "Elide: pick the resume point"
        assert str(help_text.content) == "H"


async def test_tree_modal_default_caption_is_unchanged():
    log, _ids = _linear_log()
    view = ConversationTree(log.entries(), log.cursor)
    harness = _ModalHarness(SessionTreeModal(view))
    async with harness.run_test() as pilot:
        await pilot.pause()
        title = harness.screen.query_one("#tree-browser-title", Static)
        assert str(title.content) == "Browse Conversation Tree"


# ---------------------------------------------------------------------------
# The gesture itself: ctrl+E inside the browser (PLAN-0.9.4 §4)
# ---------------------------------------------------------------------------


def _widget_rows(node):
    for child in node.children:
        yield child
        yield from _widget_rows(child)


async def _open(view):
    """Open the browser on ``view``, fully unfolded, and hand back (harness, modal).

    Every turn group is expanded first: a group mounts collapsed unless the cursor
    is inside it, and ``move_cursor`` onto a row whose line has never been laid out
    is a no-op. Expanding is the gesture a reader performs to reach such a row.
    """
    modal = SessionTreeModal(view)
    harness = _ModalHarness(modal)
    return harness, modal


async def _goto(harness, pilot, entry_id):
    from textual.widgets import Tree

    tree = harness.screen.query_one("#tree-browser-tree", Tree)
    for node in list(_widget_rows(tree.root)):
        if node.allow_expand:
            node.expand()
    await pilot.pause()
    tree.move_cursor(next(n for n in _widget_rows(tree.root) if n.data == entry_id))
    await pilot.pause()
    return tree


def _forked_log():
    """``u1 → a1 → u2 → a2``, plus a second branch ``b1`` hanging off ``a1``.

    The shape that separates "on one line of the conversation" from "in the same
    tree": ``b1`` and ``a2`` are cousins, and no elide can name both.
    """
    from tau_agent_core.session_log import InMemorySessionLog

    log = InMemorySessionLog()
    u1 = log.append_message({"role": "user", "content": "u1"})
    a1 = log.append_message({"role": "assistant", "content": "a1"})
    u2 = log.append_message({"role": "user", "content": "u2"})
    a2 = log.append_message({"role": "assistant", "content": "a2"})
    b1 = log.append_at(a1, "message", {"message": {"role": "assistant", "content": "b1"}})
    return log, u1, a1, u2, a2, b1


async def test_ctrl_e_with_no_mark_folds_the_history_behind_the_current_tip():
    """The ordinary elide, and the one that would otherwise cost a mark to say.

    With nothing marked the other end is the current leaf, so putting the cursor
    on a node and pressing the key means "keep from here to where I am, drop what
    is older". The leaf is the anchor because it is the deeper of the two.
    """
    log, ids = _linear_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, ids[2])
        modal.action_elide()
        await pilot.pause()
    assert harness.result == TreeIntent("elide", (ids[4], ids[2]))


async def test_the_deeper_node_is_the_anchor_whichever_order_they_were_marked():
    """The reader marks one end and puts the cursor on the other, and does not have
    to remember which they picked first — the tree decides."""
    log, ids = _linear_log()
    expected = TreeIntent("elide", (ids[3], ids[1]))

    for mark, cursor in ((ids[1], ids[3]), (ids[3], ids[1])):
        harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
        async with harness.run_test() as pilot:
            for _ in range(4):
                await pilot.pause()
            await _goto(harness, pilot, mark)
            modal.action_toggle_mark()
            await _goto(harness, pilot, cursor)
            modal.action_elide()
            await pilot.pause()
        assert harness.result == expected, f"marked {mark}, cursor on {cursor}"


async def test_two_nodes_on_different_branches_are_refused_and_the_browser_stays_open():
    """The reported problem: an illegal second pick used to be discovered after the
    browser had closed, as an error over a conversation whose shape was no longer
    on screen. It is refused here, by name, with the tree still up."""
    log, _u1, _a1, _u2, a2, b1 = _forked_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        await _goto(harness, pilot, b1)
        modal.action_toggle_mark()
        await _goto(harness, pilot, a2)
        modal.action_elide()
        await pilot.pause()
        assert harness.result == "UNSET", "the browser must stay open"
        assert said and "different branches" in said[0]


async def test_a_span_that_would_hide_nothing_is_refused_by_name():
    """The legal-but-empty pair — the one illegality the greying does not cover,
    because computing it per row costs a context walk per row."""
    log, ids = _linear_log()
    view = ConversationTree(log.entries(), log.cursor)
    first_kept = view.context_entries()[0]["id"]
    harness, modal = await _open(view)
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        await _goto(harness, pilot, first_kept)
        modal.action_elide()
        await pilot.pause()
        assert harness.result == "UNSET"
        assert said and "hide nothing" in said[0]


async def test_marking_one_node_greys_the_rows_that_cannot_pair_with_it():
    """`b1` and `a2` are cousins, so marking one puts the other out of reach — and
    says so on the row rather than at the keypress.

    Only while exactly one node is marked: a reader who is browsing has not asked
    which rows could form a span, and greying half the tree at them would be an
    answer to a question nobody put.
    """
    from tau_coding_agent.app import ZoneTree

    log, u1, a1, u2, a2, b1 = _forked_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        tree = harness.screen.query_one("#tree-browser-tree", ZoneTree)
        assert tree.zones.ineligible == frozenset(), "nothing marked, nothing greyed"

        await _goto(harness, pilot, b1)
        modal.action_toggle_mark()
        await pilot.pause()
        # b1's line is u1 → a1 → b1. u2 and a2 are on the other branch.
        assert tree.zones.ineligible == frozenset({u2, a2})

        await _goto(harness, pilot, a2)
        modal.action_toggle_mark()
        await pilot.pause()
        assert tree.zones.ineligible == frozenset(), "two marks name no span"


async def test_the_help_line_offers_the_elide_only_where_it_is_legal():
    """The tooltip that was asked for: it appears on a row the key would work on
    and nowhere else, and it says how much the fold would drop."""
    from textual.widgets import Static

    log, u1, a1, _u2, a2, b1 = _forked_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        marks = harness.screen.query_one("#tree-browser-marks", Static)

        await _goto(harness, pilot, b1)
        modal.action_toggle_mark()
        await pilot.pause()
        await _goto(harness, pilot, a2)
        assert "ctrl+E" not in str(marks.content), "a cousin cannot be the other end"

        # `u1` is on b1's line but is the FIRST entry, so resuming there hides
        # nothing and the offer is withheld for the second reason.
        await _goto(harness, pilot, u1)
        assert "ctrl+E" not in str(marks.content)

        # `a1` pairs with the marked `b1`. The fold itself drops only `u1`, but
        # the anchor is `b1` and the cursor is not there, so `u2` and `a2` leave
        # the context too — and the line says so, both the count and the move.
        await _goto(harness, pilot, a1)
        assert "ctrl+E: keep this span, drop the other 3 entries, and move back to it" in str(
            marks.content
        )


async def test_three_marks_are_refused_with_a_sentence_about_two_ends():
    log, ids = _linear_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    said: list[str] = []
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        harness.notify = lambda message, **kw: said.append(str(message))  # type: ignore[method-assign]
        for entry_id in ids[:3]:
            await _goto(harness, pilot, entry_id)
            modal.action_toggle_mark()
        await _goto(harness, pilot, ids[4])
        modal.action_elide()
        await pilot.pause()
        assert harness.result == "UNSET"
        assert said and "An elide has two ends" in said[0]


async def test_the_ctrl_e_key_actually_reaches_the_action():
    """The binding, not the method. The App binds ``ctrl+e`` to the extension
    chord, so this screen's binding has to win — and it is ``priority`` rather
    than relying on which of two non-priority bindings textual reaches first."""
    log, ids = _linear_log()
    harness, _modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, ids[2])
        await pilot.press("ctrl+e")
        await pilot.pause()
    assert harness.result == TreeIntent("elide", (ids[4], ids[2]))


async def test_the_ctrl_d_key_folds_the_detail_pane():
    """``ctrl+d``, and not the ``ctrl+m`` that was asked for: a terminal sends the
    same byte for ``Enter`` and ``Ctrl+M`` (textual's ``KEY_ALIASES`` maps
    ``enter`` to ``ctrl+m``), and ``Enter`` is this screen's commit key."""
    from tau_coding_agent.app import TreeDetailPane

    log, _ids = _linear_log()
    harness, _modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        pane = harness.screen.query_one(TreeDetailPane)
        assert pane.display is True
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert pane.display is False
        assert harness.result == "UNSET", "folding the pane must not commit anything"


async def test_enter_commits_without_also_folding_the_detail_pane():
    """The reason ``ctrl+m`` could not be used, asserted rather than only argued.

    Read off ``_detail_folded`` rather than the pane's ``display``: ``Enter``
    dismisses the screen, and a detached screen reports zero height, which hides
    the pane through the layout rule for reasons that have nothing to do with the
    key. ``_detail_folded`` is the reader's choice and only the toggle writes it.
    """
    log, ids = _linear_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test(size=(120, 40)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await _goto(harness, pilot, ids[2])
        await pilot.press("enter")
        await pilot.pause()
        assert modal._detail_folded is False
    assert harness.result is not None and harness.result != "UNSET"


async def test_the_offer_counts_what_leaves_the_context_not_what_the_fold_hides():
    """The reported confusion, and the defect underneath it.

    ``_linear_log`` is ``m0..m4`` with the cursor at ``m4``. Pairing ``m1`` with
    ``m3`` keeps ``[m1,m2,m3]`` — the two ends bracket what is KEPT. Two entries
    leave: ``m0`` to the fold, and ``m4`` because the anchor is not the tip, so
    the conversation resumes at ``m3`` and abandons what came after.

    The line used to count only the fold's own drop
    (``context_entries(anchor)``), which cannot see the one the cursor move takes
    — it would have said 1 here, and 1 of 3 on the six-entry case in the report.
    """
    from textual.widgets import Static

    log, ids = _linear_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        marks = harness.screen.query_one("#tree-browser-marks", Static)

        await _goto(harness, pilot, ids[1])
        modal.action_toggle_mark()
        await _goto(harness, pilot, ids[3])
        await pilot.pause()

        plan = modal._elide_plan(ids[3])
        assert plan is not None
        assert (plan.anchor, plan.first_kept) == (ids[3], ids[1])
        assert plan.folded == 1, "the fold itself drops only the first entry"
        assert plan.dropped == 2, "…but m4 goes with the cursor"
        assert plan.moves_cursor is True
        assert "drop the other 2 entries, and move back to it" in str(marks.content)


async def test_an_elide_at_the_tip_drops_only_the_prefix_and_says_so():
    """The ordinary case: the anchor IS the tip, nothing is abandoned, and the
    line does not warn about a move that is not happening."""
    from textual.widgets import Static

    log, ids = _linear_log()
    harness, modal = await _open(ConversationTree(log.entries(), log.cursor))
    async with harness.run_test() as pilot:
        for _ in range(4):
            await pilot.pause()
        marks = harness.screen.query_one("#tree-browser-marks", Static)

        await _goto(harness, pilot, ids[3])
        await pilot.pause()

        plan = modal._elide_plan(ids[3])
        assert plan is not None
        assert (plan.anchor, plan.first_kept) == (ids[4], ids[3])
        assert plan.folded == plan.dropped == 3
        assert plan.moves_cursor is False
        assert "drop the other 3 entries" in str(marks.content)
        assert "move back to it" not in str(marks.content)


def test_the_two_ends_bracket_what_is_kept():
    """The whole question, at the backend where the answer lives.

    Pairing 2 with 4 over ``[1..6]`` yields ``[2,3,4]``, not ``[1,5,6]``. An
    elide is the summary-less compaction anchor and a compaction keeps a tail, so
    the kept region is always one contiguous run ending at the anchor — cutting a
    span out of the MIDDLE is not a shape this operation can express.
    """
    log, ids = _linear_log()
    ids = ids + [log.append_message({"role": "user", "content": "m5"})]
    backend = _backend()
    backend.elide_span(log, ids[3], ids[1])
    kept = [
        e.get("message", {}).get("content")
        for e in ConversationTree(log.entries(), log.cursor).context_entries()
        if e.get("type") == "message"
    ]
    assert kept == ["m1", "m2", "m3"]
