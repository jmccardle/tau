"""Finding 5 (phase-3 review): ``app.py``'s ``AgentSessionRuntime`` path is
reached by ~55 tests and asserted by none of them.

The reviewer proved this by mutation both directions against the existing
suite:

- Force ``_build_session_runtime`` to return ``None`` (every call site falls
  back to the pre-phase-3 ``_bind_backend_session`` path): the suite was
  BYTE-IDENTICAL to baseline.
- Make it raise when a real ``agent_session`` is present: 55 tests failed.

So the path executes; nothing asserted on what it ADDS. Every other TUI test
that touches ``action_new_chat``/``action_clear_chat``/``on_chat_selected``
uses a backend double with no ``agent_session`` attribute at all (or a bare
``MagicMock``/fake session, never a real ``AgentSession``) — ``getattr
(backend, "agent_session", None)`` is ``None``, ``_build_session_runtime``
returns ``None``, and every one of those tests exercises the OTHER branch.

This file uses the REAL production ``create_backend`` (``tau_coding_agent
.backends.create_backend`` — the same double ``test_extension_notify_and_
veto.py`` already established is network-free at construction time: no
turn is ever submitted here, so the model config's ``base_url`` is never
dialled), which gives ``app.current_backend.agent_session`` a genuine
``AgentSession`` — not a double standing in for one — so
``_build_session_runtime`` takes the runtime branch for real.

Reference: docs/REMOTE-CONTROL.md §4[6] H1-H4.
"""

from __future__ import annotations

import os

import pytest

from tau_llm.types import Usage
from tau_coding_agent.backends import create_backend


@pytest.fixture
def app(make_app):
    """A TauApp wired to a REAL TauBackend (network-free at construction)."""
    return make_app(create_backend=create_backend)


async def test_clear_chat_resets_dirtied_runtime_state(app, wait_for_workers_settled):
    """H3's reset set is not a no-op at THIS call site (``action_clear_chat``'s
    own docstring): unlike a freshly constructed ``AgentSession``, the one
    being cleared may carry usage/queued-message/streaming state left over
    from the conversation just finished. Dirties exactly that set — bypassing
    the turn machinery entirely, since this test only needs to prove the
    RESET runs — and asserts ``action_clear_chat`` actually clears it.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        session = app.current_backend.agent_session
        assert session is not None  # sanity: the runtime branch is live

        session._last_usage = Usage(input_tokens=9, output_tokens=9, total_tokens=18)
        session._pending_follow_up_messages = [{"role": "user", "content": "queued"}]
        session._pending_next_turn_messages = [{"role": "user", "content": "also queued"}]
        session._is_streaming = True

        await app.action_clear_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        assert session._last_usage is None
        assert session._pending_follow_up_messages == []
        assert session._pending_next_turn_messages == []
        assert session._is_streaming is False
        assert app._session_event_unsub is not None


async def test_clear_chat_veto_leaves_app_state_unchanged(app, wait_for_workers_settled):
    """H2: a ``session_before_switch`` extension hook may refuse a swap, and
    the app must treat that as a hard no-op — ``current_session``/``messages``
    exactly as they were, not silently replaced.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        session = app.current_backend.agent_session
        session_before = app.current_session
        messages_before = list(app.messages)

        bucket = session._extension_runner.register_extension("veto-ext")
        bucket.on("session_before_switch", lambda event, ctx: {"cancel": True})

        await app.action_clear_chat()
        await pilot.pause()

        assert app.current_session is session_before
        assert app.messages == messages_before


async def test_fork_moves_onto_a_new_session_and_leaves_the_source_alone(
    app, wait_for_workers_settled
):
    """``/fork`` performs the ``fork`` capability the RPC verb has always exposed.

    Until the capability/flow split the slash was an alias of ``/tree`` and nothing
    in any head reached ``AgentSessionRuntime.fork()``. This pins what it does: the
    app moves onto a NEW session id, and the source session — which the catalog's
    fork contract never touches — is still on disk with its own history.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        source = app.current_session
        source_id = source.id

        await app.action_fork_session()
        await pilot.pause()
        await wait_for_workers_settled(app)

        assert app.current_session.id != source_id
        assert app.messages == list(app.current_session.context)
        assert any(info.id == source_id for info in app.session_catalog.list(os.getcwd()))


async def test_fork_veto_leaves_app_state_unchanged(app, wait_for_workers_settled):
    """H2 again: a ``session_before_switch`` hook refuses, and the app does nothing."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        await wait_for_workers_settled(app)

        session = app.current_backend.agent_session
        session_before = app.current_session
        messages_before = list(app.messages)

        bucket = session._extension_runner.register_extension("veto-ext")
        bucket.on("session_before_switch", lambda event, ctx: {"cancel": True})

        await app.action_fork_session()
        await pilot.pause()

        assert app.current_session is session_before
        assert app.messages == messages_before


async def test_fork_without_a_runtime_says_so_rather_than_appearing_to_work(make_app):
    """Fail-Early: no runtime means no catalog to fork through."""
    notes: list[tuple[str, str]] = []
    application = make_app()
    async with application.run_test() as pilot:
        await pilot.pause()
        application._session_runtime = None
        application.notify = lambda message, **kw: notes.append(  # type: ignore[method-assign]
            (message, kw.get("severity", ""))
        )

        await application.action_fork_session()
        await pilot.pause()

        assert notes == [("Forking needs a persistent session", "warning")]
