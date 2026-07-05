"""Tests for tau_agent_core.extensions.runner — the return-collecting dispatcher.

Covers the S5 Verify clause: collect + chain + short-circuit for the four mutating
hook events, plus the no-handler fast path (``has_handlers`` False and the emit
methods returning the identity result without doing work).

Also pins the pi-parity ordering contract (extensions in load order, handlers in
registration order) and the Fail-Early error surfacing.

Reference: docs/EXTENSIONS-IMPLEMENTATION.md E1.3 / §8 step S5.
pi source of truth: coding-agent/src/core/extensions/runner.ts.
"""

from __future__ import annotations

import pytest

from tau_agent_core.extension_types import ExtensionContext
from tau_agent_core.extensions.runner import (
    ExtensionError,
    ExtensionHandlers,
    ExtensionRunner,
)


# ----------------------------------------------------------------------
# Fast path — no handlers
# ----------------------------------------------------------------------


async def test_has_handlers_false_when_no_extensions() -> None:
    runner = ExtensionRunner()
    for event in ExtensionRunner.HOOK_EVENTS:
        assert runner.has_handlers(event) is False


async def test_no_handler_fast_path_returns_identity() -> None:
    """With no handlers each emit returns the identity result and does no work."""
    runner = ExtensionRunner()

    assert await runner.emit_tool_call({"type": "tool_call", "input": {}}) is None
    assert await runner.emit_tool_result({"type": "tool_result"}) is None
    assert await runner.emit_before_agent_start("hi", None, "SYS") is None


async def test_has_handlers_true_only_for_registered_event() -> None:
    runner = ExtensionRunner()
    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_call", lambda event, ctx: None)

    assert runner.has_handlers("tool_call") is True
    assert runner.has_handlers("tool_result") is False


# ----------------------------------------------------------------------
# tool_call — collect + short-circuit + in-place patch
# ----------------------------------------------------------------------


async def test_tool_call_block_short_circuits() -> None:
    runner = ExtensionRunner()
    seen: list[str] = []

    a = runner.register_extension("/ext/a.py")
    a.on("tool_call", lambda event, ctx: (seen.append("a"), {"block": True, "reason": "nope"})[1])
    b = runner.register_extension("/ext/b.py")
    b.on("tool_call", lambda event, ctx: seen.append("b"))

    result = await runner.emit_tool_call({"type": "tool_call", "input": {}})

    # The block result carries the handler's fields PLUS the S50 attribution:
    # the runner names the extension that vetoed (its bucket path) so the call-site
    # can render "⛔ blocked by <ext>" + emit the JSON veto record.
    assert result == {"block": True, "reason": "nope", "extension": "/ext/a.py"}
    assert seen == ["a"]  # second handler never ran (short-circuit)


async def test_tool_call_in_place_input_patch_is_visible_to_later_handlers() -> None:
    runner = ExtensionRunner()

    def patch(event: dict, ctx: object) -> None:
        event["input"]["path"] = "/patched"

    later_saw: dict = {}

    def observe(event: dict, ctx: object) -> None:
        later_saw["path"] = event["input"]["path"]

    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_call", patch)
    ext.on("tool_call", observe)

    event = {"type": "tool_call", "input": {"path": "/orig"}}
    result = await runner.emit_tool_call(event)

    assert result is None  # no handler returned a truthy result
    assert event["input"]["path"] == "/patched"  # mutation persisted
    assert later_saw["path"] == "/patched"  # later handler saw the mutation


async def test_tool_call_exception_propagates_fail_closed() -> None:
    """tool_call does NOT swallow — the throw propagates for the call-site to block."""
    runner = ExtensionRunner()

    def boom(event: dict, ctx: object) -> None:
        raise RuntimeError("kaboom")

    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_call", boom)

    with pytest.raises(RuntimeError, match="kaboom"):
        await runner.emit_tool_call({"type": "tool_call", "input": {}})


async def test_tool_call_last_truthy_result_wins_without_block() -> None:
    runner = ExtensionRunner()
    a = runner.register_extension("/ext/a.py")
    a.on("tool_call", lambda event, ctx: {"reason": "first"})
    b = runner.register_extension("/ext/b.py")
    b.on("tool_call", lambda event, ctx: {"reason": "second"})

    result = await runner.emit_tool_call({"type": "tool_call", "input": {}})
    assert result == {"reason": "second"}


# ----------------------------------------------------------------------
# tool_result — chained field patches, later sees earlier
# ----------------------------------------------------------------------


async def test_tool_result_chained_patch_later_sees_earlier() -> None:
    runner = ExtensionRunner()
    a = runner.register_extension("/ext/a.py")
    a.on("tool_result", lambda event, ctx: {"content": "patched-by-a"})

    seen_by_b: dict = {}

    def b_handler(event: dict, ctx: object) -> dict:
        seen_by_b["content"] = event["content"]
        return {"is_error": True}

    b = runner.register_extension("/ext/b.py")
    b.on("tool_result", b_handler)

    result = await runner.emit_tool_result(
        {"type": "tool_result", "content": "orig", "is_error": False, "details": None}
    )

    assert seen_by_b["content"] == "patched-by-a"  # b saw a's patch
    assert result == {"content": "patched-by-a", "details": None, "is_error": True}


async def test_tool_result_no_patch_returns_none() -> None:
    runner = ExtensionRunner()
    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_result", lambda event, ctx: None)

    result = await runner.emit_tool_result(
        {"type": "tool_result", "content": "orig", "is_error": False}
    )
    assert result is None


async def test_tool_result_handler_error_is_surfaced_not_dropped() -> None:
    runner = ExtensionRunner()
    errors: list[ExtensionError] = []
    runner.on_error(errors.append)

    def boom(event: dict, ctx: object) -> None:
        raise ValueError("bad")

    a = runner.register_extension("/ext/a.py")
    a.on("tool_result", boom)
    b = runner.register_extension("/ext/b.py")
    b.on("tool_result", lambda event, ctx: {"content": "recovered"})

    result = await runner.emit_tool_result({"type": "tool_result", "content": "orig"})

    assert result == {"content": "recovered", "details": None, "is_error": None}
    assert len(errors) == 1
    assert errors[0].extension_path == "/ext/a.py"
    assert errors[0].event == "tool_result"
    assert "bad" in errors[0].error


# ----------------------------------------------------------------------
# before_agent_start — system_prompt chains, messages accumulate
# ----------------------------------------------------------------------


async def test_before_agent_start_chains_prompt_and_accumulates_messages() -> None:
    runner = ExtensionRunner()

    chained: list[str] = []

    def a_handler(event: dict, ctx: object) -> dict:
        chained.append(event["system_prompt"])
        return {"system_prompt": event["system_prompt"] + " +A", "message": {"customType": "m1"}}

    def b_handler(event: dict, ctx: object) -> dict:
        chained.append(event["system_prompt"])
        return {"system_prompt": event["system_prompt"] + " +B", "message": {"customType": "m2"}}

    a = runner.register_extension("/ext/a.py")
    a.on("before_agent_start", a_handler)
    b = runner.register_extension("/ext/b.py")
    b.on("before_agent_start", b_handler)

    result = await runner.emit_before_agent_start("prompt", None, "SYS")

    assert chained == ["SYS", "SYS +A"]  # b saw a's chained system_prompt (last wins)
    assert result is not None
    assert result["system_prompt"] == "SYS +A +B"
    assert result["messages"] == [{"customType": "m1"}, {"customType": "m2"}]


async def test_before_agent_start_only_messages_leaves_system_prompt_none() -> None:
    runner = ExtensionRunner()
    ext = runner.register_extension("/ext/a.py")
    ext.on("before_agent_start", lambda event, ctx: {"message": {"customType": "m"}})

    result = await runner.emit_before_agent_start("p", None, "SYS")
    assert result == {"messages": [{"customType": "m"}], "system_prompt": None}


# ----------------------------------------------------------------------
# Ordering + async + ctx threading
#
# (These exercise generic dispatcher behaviour — load/registration order,
# async-await, and the bound ``ExtensionContext`` handed to every handler.
# They used to ride the ``context`` hook, removed in E5 §3.2 / S30; re-pointed
# at the surviving ``tool_result`` hook so the coverage is preserved.)
# ----------------------------------------------------------------------


async def test_load_and_registration_order_preserved() -> None:
    runner = ExtensionRunner()
    order: list[str] = []

    a = runner.register_extension("/ext/a.py")
    a.on("tool_result", lambda event, ctx: order.append("a1") or None)
    a.on("tool_result", lambda event, ctx: order.append("a2") or None)
    b = runner.register_extension("/ext/b.py")
    b.on("tool_result", lambda event, ctx: order.append("b1") or None)

    await runner.emit_tool_result({"type": "tool_result", "content": "x"})
    assert order == ["a1", "a2", "b1"]


async def test_async_handler_is_awaited() -> None:
    runner = ExtensionRunner()

    async def a_handler(event: dict, ctx: object) -> dict:
        return {"content": "async-patched"}

    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_result", a_handler)

    result = await runner.emit_tool_result({"type": "tool_result", "content": "orig"})
    assert result == {"content": "async-patched", "details": None, "is_error": None}


async def test_bound_context_is_passed_to_handlers() -> None:
    ctx = ExtensionContext(cwd="/work")
    runner = ExtensionRunner(context=ctx)
    received: list[object] = []

    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_result", lambda event, c: received.append(c) or None)

    await runner.emit_tool_result({"type": "tool_result", "content": "x"})
    assert received == [ctx]


async def test_set_context_rebinds() -> None:
    runner = ExtensionRunner()
    ctx = ExtensionContext(cwd="/late")
    runner.set_context(ctx)
    received: list[object] = []

    ext = runner.register_extension("/ext/a.py")
    ext.on("tool_result", lambda event, c: received.append(c) or None)

    await runner.emit_tool_result({"type": "tool_result", "content": "x"})
    assert received == [ctx]


async def test_constructor_accepts_prebuilt_extensions_in_order() -> None:
    a = ExtensionHandlers(path="/ext/a.py")
    b = ExtensionHandlers(path="/ext/b.py")
    order: list[str] = []
    a.on("tool_result", lambda event, ctx: order.append("a") or {"content": "a"})
    b.on("tool_result", lambda event, ctx: order.append("b") or {"content": "b"})

    runner = ExtensionRunner(extensions=[a, b])
    result = await runner.emit_tool_result({"type": "tool_result", "content": "orig"})

    assert order == ["a", "b"]
    assert result == {"content": "b", "details": None, "is_error": None}


# ----------------------------------------------------------------------
# Session-lifecycle hooks — notify-grade, error-surfaced (S41)
# ----------------------------------------------------------------------


def test_lifecycle_events_are_owned_by_the_runner() -> None:
    """``session_start`` / ``session_shutdown`` are the runner's lifecycle events."""
    assert ExtensionRunner.LIFECYCLE_EVENTS == ("session_start", "session_shutdown")
    # Disjoint from the mutating hooks — different dispatch semantics.
    assert set(ExtensionRunner.LIFECYCLE_EVENTS).isdisjoint(ExtensionRunner.HOOK_EVENTS)


async def test_lifecycle_no_handler_fast_path() -> None:
    """With no handlers ``has_handlers`` is False and the emits do nothing."""
    runner = ExtensionRunner()
    assert runner.has_handlers("session_start") is False
    assert runner.has_handlers("session_shutdown") is False
    # No handlers → the emit is a silent no-op (nothing to raise on).
    assert await runner.emit_session_start({"type": "session_start"}) is None
    assert await runner.emit_session_shutdown({"type": "session_shutdown"}) is None


async def test_session_start_fires_handlers_in_order() -> None:
    runner = ExtensionRunner()
    seen: list[str] = []

    a = runner.register_extension("/ext/a.py")
    a.on("session_start", lambda event, ctx: seen.append("a1"))
    a.on("session_start", lambda event, ctx: seen.append("a2"))
    b = runner.register_extension("/ext/b.py")
    b.on("session_start", lambda event, ctx: seen.append("b1"))

    assert runner.has_handlers("session_start") is True
    await runner.emit_session_start({"type": "session_start", "reason": "startup"})
    assert seen == ["a1", "a2", "b1"]


async def test_session_shutdown_fires_async_handler_and_passes_event() -> None:
    runner = ExtensionRunner()
    received: list[dict] = []

    async def handler(event: dict, ctx: object) -> None:
        received.append(event)

    ext = runner.register_extension("/ext/a.py")
    ext.on("session_shutdown", handler)

    await runner.emit_session_shutdown({"type": "session_shutdown", "reason": "quit"})
    assert received == [{"type": "session_shutdown", "reason": "quit"}]


async def test_lifecycle_return_value_is_discarded() -> None:
    """Notify-grade: a handler returning a dict has NO effect (return discarded)."""
    runner = ExtensionRunner()
    ext = runner.register_extension("/ext/a.py")
    ext.on("session_start", lambda event, ctx: {"message": "ignored", "block": True})

    # The emit returns None regardless of what the handler returned.
    assert await runner.emit_session_start({"type": "session_start"}) is None


async def test_lifecycle_handler_error_is_surfaced_not_swallowed() -> None:
    """A throwing lifecycle handler is reported via on_error, never dropped, and
    dispatch continues to the next handler (no fail-closed — nothing to gate on)."""
    runner = ExtensionRunner()
    errors: list[ExtensionError] = []
    runner.on_error(errors.append)
    seen: list[str] = []

    def boom(event: dict, ctx: object) -> None:
        raise RuntimeError("teardown failed")

    a = runner.register_extension("/ext/a.py")
    a.on("session_shutdown", boom)
    b = runner.register_extension("/ext/b.py")
    b.on("session_shutdown", lambda event, ctx: seen.append("b"))

    # Does NOT raise out of the dispatch (surfaced, not propagated).
    await runner.emit_session_shutdown({"type": "session_shutdown"})

    assert len(errors) == 1
    assert errors[0].extension_path == "/ext/a.py"
    assert errors[0].event == "session_shutdown"
    assert "teardown failed" in errors[0].error
    # The second extension's handler still ran despite the first one failing.
    assert seen == ["b"]
