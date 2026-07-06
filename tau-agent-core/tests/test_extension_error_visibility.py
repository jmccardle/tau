"""S44 (E6) — error visibility: hook / notify handler failures must surface.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S44 (anchors G3 + G12).

Two regressions this locks down:

- **G12** — ``ExtensionRunner.on_error`` had no caller, so a raising mutating /
  lifecycle hook only ever reached a bare stderr print. The session now binds a
  listener that routes the ``ExtensionError`` to ``ctx.ui.notify`` at ``warning``
  level: a TUI notice when a delegate is set, a structured stderr line headless.
- **G3** — the notify ``EventBus`` swallowed handler exceptions silently
  (``events.py`` "Fail silently"). It now reports ``(exc, channel)`` through
  :meth:`EventBus.on_error`, which the session wires to the SAME surface, so a
  raising observer is as visible as a raising mutating hook.

Fail-Early: a hook error is never silent. And — because an error notice is
transient chrome, not conversation state — surfacing one must NOT mutate the
active path (the tree-as-truth invariant); ``test_error_surface_is_ephemeral``
guards that.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.session_log import InMemorySessionLog


def _make_model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


class _CapturingDelegate:
    """A minimal UI delegate that records ``notify`` calls (TUI-mode double)."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append((message, level))


def _raise(exc: Exception):
    """A zero-arg body that raises ``exc`` — for one-line lambda handlers."""

    def _body(*_args, **_kwargs):
        raise exc

    return _body


# ---------------------------------------------------------------------------
# EventBus: stop swallowing (G3)
# ---------------------------------------------------------------------------


def test_bus_surfaces_raising_handler_to_on_error_listener():
    """A raising notify handler reaches on_error with ``(exc, channel)``."""
    bus = EventBus()
    seen: list[tuple[str, str]] = []
    bus.on_error(lambda exc, channel: seen.append((str(exc), channel)))

    sibling_ran: list[str] = []
    bus.on("agent_start", _raise(ValueError("boom")))
    bus.on("agent_start", lambda e: sibling_ran.append("ran"))

    asyncio.run(bus.emit(AgentEvent(type="agent_start", timestamp=0)))

    assert seen == [("boom", "agent_start")]
    # The failure surfaces but does NOT abort the sibling handler (fire-and-forget
    # for the siblings; visible for the failure).
    assert sibling_ran == ["ran"]


async def test_bus_surfaces_async_raising_handler():
    """An async handler that raises surfaces just like a sync one (not swallowed)."""
    bus = EventBus()
    seen: list[str] = []
    bus.on_error(lambda exc, channel: seen.append(str(exc)))

    async def bad(_event):
        raise RuntimeError("async boom")

    bus.on("all", bad)
    await bus.emit(AgentEvent(type="agent_start", timestamp=0))

    assert seen == ["async boom"]


def test_bus_without_listener_writes_stderr_not_silent(capsys):
    """With no on_error listener the bus still refuses the silent drop → stderr."""
    bus = EventBus()
    bus.on("all", _raise(RuntimeError("unhandled boom")))

    asyncio.run(bus.emit(AgentEvent(type="agent_start", timestamp=0)))

    err = capsys.readouterr().err
    assert "unhandled boom" in err
    assert "agent_start" in err


def test_emit_channel_surfaces_error():
    """``emit_channel`` (session-lifecycle routing) also surfaces, not swallows."""
    bus = EventBus()
    seen: list[tuple[str, str]] = []
    bus.on_error(lambda exc, channel: seen.append((str(exc), channel)))
    bus.on("session_event", _raise(ValueError("channel boom")))

    asyncio.run(bus.emit_channel("session_event", {"type": "x"}))

    assert seen == [("channel boom", "session_event")]


def test_bus_on_error_unsubscribe_stops_delivery():
    """Unsubscribing an on_error listener removes it (parity with ``on``)."""
    bus = EventBus()
    seen: list[str] = []
    unsub = bus.on_error(lambda exc, channel: seen.append(str(exc)))
    unsub()
    bus.on("all", _raise(ValueError("boom")))

    # No listener now → falls back to stderr, but the removed listener is not called.
    asyncio.run(bus.emit(AgentEvent(type="agent_start", timestamp=0)))
    assert seen == []


# ---------------------------------------------------------------------------
# AgentSession: wire the runner + bus onto one on_error surface (G12 + G3)
# ---------------------------------------------------------------------------


def test_session_routes_mutating_hook_error_to_tui_delegate():
    """A raising ``tool_result`` hook paints a TUI warning notice (G12)."""

    def ext(api):
        api.on("tool_result", _raise(ValueError("hook boom")))

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_make_model(), extensions=[ext]
    )
    delegate = _CapturingDelegate()
    session.set_ui_delegate(delegate)

    # The runner catches the handler exception and surfaces it; the patch result is
    # still None (nothing modified), and the loop would pass the original through.
    result = asyncio.run(
        session._extension_runner.emit_tool_result({"content": "x"})
    )
    assert result is None

    assert len(delegate.notifications) == 1
    message, level = delegate.notifications[0]
    assert level == "warning"
    assert "hook boom" in message
    assert "tool_result" in message


def test_session_routes_mutating_hook_error_to_headless_stderr(capsys):
    """With no delegate the same error becomes a structured headless stderr line."""

    def ext(api):
        api.on("before_agent_start", _raise(RuntimeError("prompt-hook boom")))

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_make_model(), extensions=[ext]
    )
    # No set_ui_delegate → ctx.ui stays in headless (stderr) mode.
    asyncio.run(session._extension_runner.emit_before_agent_start("hi", None, "SYS"))

    err = capsys.readouterr().err
    assert "warning" in err
    assert "prompt-hook boom" in err
    assert "before_agent_start" in err


def test_session_routes_lifecycle_hook_error():
    """A raising ``session_shutdown`` (notify-grade) hook still surfaces (S41 × S44)."""

    def ext(api):
        api.on("session_shutdown", _raise(ValueError("teardown boom")))

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_make_model(), extensions=[ext]
    )
    delegate = _CapturingDelegate()
    session.set_ui_delegate(delegate)

    asyncio.run(session.emit_session_shutdown("quit"))

    assert any(
        "teardown boom" in message and level == "warning"
        for message, level in delegate.notifications
    )


@pytest.mark.usefixtures("fake_llm")
def test_notify_handler_error_surfaces_during_prompt():
    """A raising notify observer surfaces (G3) yet the turn completes normally."""

    def ext(api):
        api.on("agent_start", _raise(ValueError("observer boom")))

    session = AgentSession(
        session_log=InMemorySessionLog(), model=_make_model(), extensions=[ext]
    )
    delegate = _CapturingDelegate()
    session.set_ui_delegate(delegate)

    messages = asyncio.run(session.prompt("hello"))

    # The loop ran to completion despite the exploding observer …
    assert len(messages) > 0
    # … and the failure did not vanish — it painted a warning notice.
    assert any(
        "observer boom" in message and level == "warning"
        for message, level in delegate.notifications
    )


def test_error_surface_is_ephemeral():
    """Surfacing an error must NOT append a node to the path (tree-as-truth).

    An error notice is transient chrome, not conversation state — the S44 surface
    routes to ``ctx.ui.notify`` only, never a durable append. The active path is
    byte-identical before and after a hook explodes.
    """

    def ext(api):
        api.on("tool_result", _raise(ValueError("hook boom")))

    log = InMemorySessionLog()
    session = AgentSession(session_log=log, model=_make_model(), extensions=[ext])
    before = list(log.entries())

    asyncio.run(session._extension_runner.emit_tool_result({"content": "x"}))

    assert list(log.entries()) == before
