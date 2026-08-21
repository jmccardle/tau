"""B1-d: creating an ``elide`` span from the TUI tree browser (W3).

W3 landed the ``elide`` node in the core — the summary-less generalization of the
compaction splice anchor — with no way to create one from the TUI, so a human could
not use it at all. These tests drive the fourth tree-browser mode end to end at the
action level (``Parley.action_browse_tree`` → ``TreeModeModal`` "elide" → a SECOND
node pick → ``TauBackend.elide_span``), and pin the two invariants that make it safe:

* the resulting context skips **exactly** the elided span, and
* ``entries()`` still contains every id ever minted (Decision 7 / T5, restated at the
  TUI level — an elide hides a span from a fold, never from the log).

Plus the two ways the flow must refuse rather than persist something incoherent: a
resume point that is not on the anchor's path (which would collapse the context to
EMPTY, silently) and a span that would hide nothing.

Reference: NODE-ADDRESSABLE-AGENTS.md W3 / T3 / T5, Decision 2 and 7;
SESSION-TREE-IMPLEMENTATION.md §3 (the browser + the §3.4 re-render seam).
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App
from textual.widgets import Button, Static

from tau_agent_core.conversation_tree import ConversationTree
from tau_coding_agent.app import ChatDisplay, Parley, SessionTreeModal, TreeModeModal
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
    """The whole point, end to end: pick anchor → "elide" → pick resume point, and
    the transcript loses precisely the entries before the resume point — while the
    log keeps all of them (T5)."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        before_ids = {e["id"] for e in session.entries()}
        anchor = session.cursor  # the tip (a3) — fold the history behind it
        resume = ids[4]  # u3

        pushed = _script(app, [anchor, "elide", resume])
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

        # The second pick reused the browser with the resume-point caption.
        assert isinstance(pushed[0], SessionTreeModal)
        assert isinstance(pushed[1], TreeModeModal)
        assert isinstance(pushed[2], SessionTreeModal)
        assert pushed[2]._title == "Elide: pick the resume point"


async def test_elide_at_an_interior_anchor_navigates_first(app, wait_for_workers_settled):
    """An anchor that is not the tip needs the leaf moved onto it before the elide
    can parent there — the same ``navigate`` append the "no summary" mode makes."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        anchor = ids[3]  # a2 — an interior node, not the cursor
        resume = ids[2]  # u2, its parent

        _script(app, [anchor, "elide", resume])
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
    """Regression for the guard that used to run BEFORE the mode pick: "already at
    that node" is a statement about branching, and eliding behind the current tip is
    the normal case, not an error."""
    async with app.run_test() as pilot:
        await pilot.pause()
        session, ids = await _seeded(app)
        notes = _notifications(app)

        _script(app, [session.cursor, "elide", ids[3]])
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

        _script(app, [ids[1], "elide", ids[5]])  # anchor a1, resume a3 (a descendant)
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

        _script(app, [session.cursor, "elide", first_kept])
        app.action_browse_tree()
        await wait_for_workers_settled(app)
        await pilot.pause()

        errors = [m for m, sev in notes if sev == "error"]
        assert len(errors) == 1 and "would hide nothing" in errors[0]
        assert not any(e.get("type") == "elide" for e in session.entries())


async def test_cancelling_the_resume_pick_appends_nothing(app, wait_for_workers_settled):
    async with app.run_test() as pilot:
        await pilot.pause()
        session, _ids = await _seeded(app)
        entries_before = session.entries()

        _script(app, [session.cursor, "elide", None])  # Esc on the second browser
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

        _script(app, [session.cursor, "elide", ids[4]])
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
    log.append_elide(ids[3])

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
    log.append_elide(ids[4])  # ids[4] is a descendant of the anchor: unreachable

    nodes = {n.id: n for n in _flatten(ConversationTree(log.entries(), log.cursor).tree())}
    elide_id = next(e["id"] for e in log.entries() if e["type"] == "elide")

    assert "not on this path" in nodes[elide_id].preview
    # …which is exactly what the fold does with it.
    assert ConversationTree(log.entries(), log.cursor).context_for() == []


def test_singular_entry_in_the_preview():
    log, ids = _linear_log()
    log.append_elide(ids[1])
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


async def test_tree_mode_modal_has_an_elide_button():
    harness = _ModalHarness(TreeModeModal())
    async with harness.run_test() as pilot:
        await pilot.pause()
        assert harness.screen.query_one("#mode-elide", Button)
        await pilot.click("#mode-elide")
        await pilot.pause()
    assert harness.result == "elide"


async def test_tree_modal_shows_the_caption_it_was_given():
    log, _ids = _linear_log()
    view = ConversationTree(log.entries(), log.cursor)
    modal = SessionTreeModal(
        view.tree(),
        resolve_entry=view.entry,
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
    harness = _ModalHarness(SessionTreeModal(view.tree(), resolve_entry=view.entry))
    async with harness.run_test() as pilot:
        await pilot.pause()
        title = harness.screen.query_one("#tree-browser-title", Static)
        assert str(title.content) == "Browse Conversation Tree"
