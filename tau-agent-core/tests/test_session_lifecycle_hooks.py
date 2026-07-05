"""E6 §2 (S41) — ``session_start`` / ``session_shutdown`` lifecycle hooks.

These two notify-grade hooks are dispatched through the session's
``ExtensionRunner`` (NOT the fire-and-forget notify ``EventBus``) so that a
handler's exception is SURFACED (the S44 regime) rather than swallowed. They
carry no return effect — they run for setup/teardown side effects (watchers,
``ctx.entries()`` reconstruction, exit commits).

This suite pins the agent-core seam:
  * ``api.on("session_start"/"session_shutdown", …)`` on a bucket-bound api lands
    in the runner (fires via ``emit_session_*``), not the bus;
  * ``AgentSession.emit_session_start/shutdown`` build the pi-shaped event dict
    (``{"type", "reason"}``) and fire the handlers;
  * the zero-handler fast path is a genuine no-op;
  * a throwing lifecycle handler is reported through the runner's ``on_error``.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S41 (anchor G1).
pi source of truth: coding-agent/src/core/extensions/types.ts (SessionStart/
ShutdownEvent), examples/extensions/auto-commit-on-exit.ts.
"""

from __future__ import annotations

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extensions.runner import ExtensionError
from tau_agent_core.session_log import InMemorySessionLog


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="gpt-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://example.invalid/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(*extensions) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=list(extensions),
    )


async def test_api_on_session_start_routes_to_runner_bucket() -> None:
    """``api.on("session_start")`` binds to the runner bucket — firing via
    ``emit_session_start`` (not the notify bus) reaches it."""
    seen: list[dict] = []

    def ext(api):
        api.on("session_start", lambda event, ctx: seen.append(event))

    session = _session(ext)
    await session.emit_session_start("startup")

    assert seen == [{"type": "session_start", "reason": "startup"}]


async def test_api_on_session_shutdown_routes_to_runner_bucket() -> None:
    seen: list[dict] = []

    def ext(api):
        api.on("session_shutdown", lambda event, ctx: seen.append(event))

    session = _session(ext)
    await session.emit_session_shutdown("quit")

    assert seen == [{"type": "session_shutdown", "reason": "quit"}]


async def test_default_reasons() -> None:
    """The reason defaults mirror pi (startup / quit) when the caller omits it."""
    starts: list[dict] = []
    shutdowns: list[dict] = []

    def ext(api):
        api.on("session_start", lambda event, ctx: starts.append(event))
        api.on("session_shutdown", lambda event, ctx: shutdowns.append(event))

    session = _session(ext)
    await session.emit_session_start()
    await session.emit_session_shutdown()

    assert starts == [{"type": "session_start", "reason": "startup"}]
    assert shutdowns == [{"type": "session_shutdown", "reason": "quit"}]


async def test_async_lifecycle_handler_is_awaited() -> None:
    seen: list[str] = []

    def ext(api):
        async def on_start(event, ctx):
            seen.append("started")

        api.on("session_start", on_start)

    session = _session(ext)
    await session.emit_session_start()
    assert seen == ["started"]


async def test_no_handler_is_a_no_op() -> None:
    """A session with no lifecycle handler fires nothing (has_handlers fast path)."""

    def ext(api):
        # Registers an unrelated hook only — no lifecycle handler.
        api.on("tool_result", lambda event, ctx: None)

    session = _session(ext)
    # Must not raise; simply a no-op.
    await session.emit_session_start()
    await session.emit_session_shutdown()


async def test_lifecycle_handler_error_surfaced_through_runner() -> None:
    """A throwing shutdown handler is reported via the runner's on_error, not
    swallowed and not propagated out of the session emit."""
    errors: list[ExtensionError] = []

    def ext(api):
        def boom(event, ctx):
            raise RuntimeError("exit commit failed")

        api.on("session_shutdown", boom)

    session = _session(ext)
    session._extension_runner.on_error(errors.append)

    # Does not raise.
    await session.emit_session_shutdown("quit")

    assert len(errors) == 1
    assert errors[0].event == "session_shutdown"
    assert "exit commit failed" in errors[0].error
