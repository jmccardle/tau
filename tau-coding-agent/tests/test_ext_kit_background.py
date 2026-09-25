"""Tests for ``examples/ext_kit/background.py`` — backgrounding conferred on a tool.

Five things have to hold, and each is a claim the module's docstring makes:

* **Conferring costs nothing until elected.** A wrapped tool with ``background``
  absent or false delegates to the original, with the flag stripped from what the
  original sees, and the schema gains exactly one property.
* **Electing it returns at once.** The tool call returns a handle while the work is
  still running, and the provenance — handle, tool, args, start time — rides
  ``details``.
* **The completion is delivered through steer, once.** With the epoch unchanged the
  supervised task calls the submit closure with the finished result.
* **A session boundary refuses loudly.** A completion whose epoch no longer matches
  raises :class:`BackgroundSessionGoneError` through the error sink and does NOT
  reach the submit closure. This is the behaviour the open product question is
  parked behind, so it is pinned rather than assumed.
* **Depth is cleared, and a ten-deep chain survives.** ``DRIVING_SUBMISSION_DEPTH``
  is a ``ContextVar`` a spawned task inherits, so without clearing it
  ``next_submission_depth`` climbs once per completion and the eleventh raises. The
  chain test drives eleven links and asserts the depth seen inside every one is 0.

``cancel_all`` gets its own test: cancel-then-await must leave no task behind and no
outstanding record, because an orphan is the failure the registry exists to prevent.

One more, smaller but easy to get wrong: a tool that reports failure by RETURNING
``is_error`` rather than raising is delivered as "failed". A blocking call shows the
model an error result, so a backgrounded one reading as success would make
backgrounding change the meaning of the answer.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.submission import (
    DRIVING_SUBMISSION_DEPTH,
    MAX_SUBMISSION_DEPTH,
    next_submission_depth,
)

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import background as bg  # noqa: E402  (path insertion must precede the import)


def _text_result(text: str) -> dict[str, Any]:
    """A tool result carrying one text block.

    Args:
        text: The block's text.

    Returns:
        A result dict in the shape a tool's ``execute`` returns.
    """
    return {"content": [{"type": "text", "text": text}]}


def _definition(
    name: str = "slow",
    *,
    execute: Any = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An extension-shape tool definition for wrapping.

    Args:
        name: Tool name.
        execute: The five-argument extension execute; defaults to an immediate echo.
        properties: Schema properties; defaults to one ``q`` string.

    Returns:
        A ``register_tool``-shaped dict.
    """

    async def _echo(tool_call_id, params, signal=None, on_update=None, ctx=None):
        return _text_result(f"echo:{params!r}")

    return {
        "name": name,
        "label": name.capitalize(),
        "description": f"the {name} tool",
        "parameters": {
            "type": "object",
            "properties": properties if properties is not None else {"q": {"type": "string"}},
            "required": [],
        },
        "execute": execute if execute is not None else _echo,
    }


class TestConferring:
    """The wrapper is transparent until the model elects backgrounding."""

    def test_schema_gains_exactly_one_property(self):
        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable("slow", reg, lambda c, r: None, definition=_definition())
        props = defn["parameters"]["properties"]
        assert set(props) == {"q", bg.BACKGROUND_KEY}
        assert props[bg.BACKGROUND_KEY]["type"] == "boolean"

    def test_the_original_schema_is_not_mutated(self):
        reg = bg.BackgroundRegistry()
        original = _definition()
        bg.backgroundable("slow", reg, lambda c, r: None, definition=original)
        assert bg.BACKGROUND_KEY not in original["parameters"]["properties"]

    def test_description_and_label_default_to_the_wrapped_tool(self):
        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable("slow", reg, lambda c, r: None, definition=_definition())
        assert defn["description"] == "the slow tool"
        assert defn["label"] == "Slow"

    async def test_absent_flag_delegates_and_strips_nothing_else(self):
        seen: dict[str, Any] = {}

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            seen.update(params)
            return _text_result("ran")

        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow", reg, lambda c, r: None, definition=_definition(execute=_exec)
        )
        result = await defn["execute"]("call-1", {"q": "hello"})
        assert seen == {"q": "hello"}
        assert result == _text_result("ran")
        assert reg.outstanding() == []

    async def test_false_flag_delegates_and_the_tool_never_sees_the_flag(self):
        seen: dict[str, Any] = {}

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            seen.update(params)
            return _text_result("ran")

        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow", reg, lambda c, r: None, definition=_definition(execute=_exec)
        )
        await defn["execute"]("call-1", {"q": "hi", bg.BACKGROUND_KEY: False})
        assert seen == {"q": "hi"}
        assert bg.BACKGROUND_KEY not in seen


class TestElecting:
    """Electing backgrounding returns a handle at once and delivers later."""

    async def test_returns_a_handle_while_the_work_is_still_running(self):
        gate = asyncio.Event()

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("late")

        delivered: list[tuple[bg.BackgroundCall, Any]] = []
        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append((c, r)),
            definition=_definition(execute=_exec),
        )

        result = await defn["execute"]("call-1", {"q": "x", bg.BACKGROUND_KEY: True})

        assert "background" in result["content"][0]["text"]
        handle = result["details"]["background_handle"]
        assert handle.startswith(f"{bg.HANDLE_PREFIX}-slow-")
        assert delivered == []
        assert [c.handle for c in reg.outstanding()] == [handle]

        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(delivered) == 1
        assert reg.outstanding() == []

    async def test_details_carry_the_full_provenance(self):
        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            return _text_result("done")

        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow", reg, lambda c, r: None, definition=_definition(execute=_exec)
        )
        result = await defn["execute"]("call-1", {"q": "x", bg.BACKGROUND_KEY: True})
        details = result["details"]
        assert details["tool"] == "slow"
        assert details["args"] == {"q": "x"}
        assert details["started_at_ms"] > 0
        assert bg.BACKGROUND_KEY not in details["args"]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_the_delivered_text_names_the_handle_and_carries_the_result(self):
        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            return _text_result("the answer")

        delivered: list[str] = []
        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append(bg.completion_text(c, r)),
            definition=_definition(execute=_exec),
        )
        result = await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        handle = result["details"]["background_handle"]
        assert handle in delivered[0]
        assert "the answer" in delivered[0]
        assert "finished" in delivered[0]

    def test_a_returned_error_result_is_delivered_as_failed_not_finished(self):
        call = bg.BackgroundCall(handle="bg-slow-1", tool="slow", args={}, started_at_ms=1, epoch=0)
        failed = {"content": [{"type": "text", "text": "no such host"}], "is_error": True}
        text = bg.completion_text(call, failed)
        assert "failed" in text
        assert "finished" not in text
        assert "no such host" in text

    def test_a_non_dict_result_still_renders(self):
        call = bg.BackgroundCall(handle="bg-slow-1", tool="slow", args={}, started_at_ms=1, epoch=0)
        assert "42" in bg.completion_text(call, 42)


class TestSessionBoundary:
    """A completion whose session is gone refuses loudly rather than misdelivering."""

    async def test_epoch_mismatch_raises_and_does_not_deliver(self):
        gate = asyncio.Event()

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("orphan")

        errors: list[BaseException] = []
        delivered: list[Any] = []
        reg = bg.BackgroundRegistry(on_error=errors.append)
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append(r),
            definition=_definition(execute=_exec),
        )

        await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        reg.bump_epoch()
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert delivered == []
        assert len(errors) == 1
        assert isinstance(errors[0], bg.BackgroundSessionGoneError)
        assert errors[0].started_epoch == 0
        assert errors[0].current_epoch == 1
        assert "NOT" in str(errors[0])

    async def test_completing_after_the_swap_is_the_covered_half(self):
        """complete → new_session → error, the sequence tectum's responder3 hits.

        ``new_session`` reaches ``AgentSession.abort()`` unconditionally
        (``agent_session_runtime.py`` ``_apply_swap``), and ``abort`` clears
        ``_pending_steer_messages``. So a completion that has not yet been submitted
        when the swap happens must not be submitted into the new session. The swap is
        modelled here as the ``session_shutdown`` hook this module binds.
        """
        gate = asyncio.Event()

        async def _never_until(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("late answer")

        api = _FakeAPI({})
        errors: list[BaseException] = []
        reg = bg.register_backgrounding(
            api, definitions={"recall": _definition("recall", execute=_never_until)}
        )
        reg.on_error = errors.append

        await api.registered[0]["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        reg.bump_epoch()
        gate.set()
        for _ in range(4):
            await asyncio.sleep(0)

        assert api.submitted == []
        assert [type(e) for e in errors] == [bg.BackgroundSessionGoneError]

    async def test_completing_before_the_swap_is_the_UNCOVERED_half(self):
        """The epoch does NOT catch a steer queued before the swap clears it.

        Sequence: the turn ends, the completion submits while the session is idle so
        the epoch still matches, and only THEN does ``new_session`` clear the queue.
        This module sees a successful submit and reports nothing, because an
        ``ExtensionAPI`` has no way to learn that a queued steer was later dropped —
        there is no "your steer was cleared" hook.

        Pinned so the limitation is a stated fact rather than a surprise:
        ``docs/BACKGROUND-TOOLS.md`` §7. The fix is the persistent-session work, not
        anything this module can do.
        """
        api = _FakeAPI({})
        errors: list[BaseException] = []
        reg = bg.register_backgrounding(api, definitions={"recall": _definition("recall")})
        reg.on_error = errors.append

        await api.registered[0]["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        for _ in range(4):
            await asyncio.sleep(0)

        assert len(api.submitted) == 1
        assert errors == []

        reg.bump_epoch()
        assert errors == []

    async def test_a_failing_tool_reaches_the_error_sink_not_the_transcript(self):
        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            raise RuntimeError("the tool broke")

        errors: list[BaseException] = []
        delivered: list[Any] = []
        reg = bg.BackgroundRegistry(on_error=errors.append)
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append(r),
            definition=_definition(execute=_exec),
        )
        await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert delivered == []
        assert [str(e) for e in errors] == ["the tool broke"]

    def test_the_default_error_sink_raises_rather_than_dropping(self):
        reg = bg.BackgroundRegistry()
        with pytest.raises(ValueError, match="boom"):
            reg.on_error(ValueError("boom"))


class TestSubmissionDepth:
    """The spawned task must not inherit the spawning turn's submission depth."""

    async def test_depth_is_cleared_inside_the_spawned_task(self):
        seen: list[int] = []

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            seen.append(next_submission_depth(0))
            return _text_result("ok")

        reg = bg.BackgroundRegistry()
        defn = bg.backgroundable(
            "slow", reg, lambda c, r: None, definition=_definition(execute=_exec)
        )
        token = DRIVING_SUBMISSION_DEPTH.set(7)
        try:
            await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            DRIVING_SUBMISSION_DEPTH.reset(token)
        assert seen == [0]

    async def test_a_chain_longer_than_the_cap_never_climbs(self):
        depths: list[int] = []
        links = MAX_SUBMISSION_DEPTH + 1

        reg = bg.BackgroundRegistry()

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            depths.append(next_submission_depth(0))
            return _text_result("ok")

        defn = bg.backgroundable(
            "slow", reg, lambda c, r: None, definition=_definition(execute=_exec)
        )

        # Each link runs inside the previous link's context, which is what would
        # accumulate depth if the registry did not clear the var.
        async def _link(remaining: int) -> None:
            await defn["execute"](f"call-{remaining}", {bg.BACKGROUND_KEY: True})
            for _ in range(4):
                await asyncio.sleep(0)
            if remaining > 1:
                await _link(remaining - 1)

        token = DRIVING_SUBMISSION_DEPTH.set(0)
        try:
            await _link(links)
        finally:
            DRIVING_SUBMISSION_DEPTH.reset(token)

        assert len(depths) == links
        assert set(depths) == {0}
        assert links > MAX_SUBMISSION_DEPTH


class TestCancelAll:
    """Shutdown leaves no orphan."""

    async def test_cancel_all_cancels_waits_and_forgets(self):
        started = asyncio.Event()

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            started.set()
            await asyncio.Event().wait()
            return _text_result("never")

        delivered: list[Any] = []
        reg = bg.BackgroundRegistry(on_error=lambda exc: None)
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append(r),
            definition=_definition(execute=_exec),
        )
        await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        await started.wait()

        cancelled = await reg.cancel_all()

        assert cancelled == 1
        assert reg.outstanding() == []
        assert delivered == []

    async def test_cancel_all_with_nothing_outstanding_is_zero(self):
        reg = bg.BackgroundRegistry()
        assert await reg.cancel_all() == 0


class TestFailEarly:
    """Every refusal the module documents."""

    def test_unknown_builtin_name_raises(self):
        reg = bg.BackgroundRegistry()
        with pytest.raises(ValueError, match="unknown built-in tool"):
            bg.backgroundable("nope-not-a-tool", reg, lambda c, r: None)

    def test_definition_and_tool_together_raise(self):
        reg = bg.BackgroundRegistry()
        with pytest.raises(ValueError, match="not both"):
            bg.backgroundable(
                "slow", reg, lambda c, r: None, definition=_definition(), tool=object()
            )

    def test_definition_missing_execute_raises(self):
        reg = bg.BackgroundRegistry()
        defn = _definition()
        del defn["execute"]
        with pytest.raises(ValueError, match="missing required key 'execute'"):
            bg.backgroundable("slow", reg, lambda c, r: None, definition=defn)

    def test_a_tool_already_declaring_the_flag_raises(self):
        reg = bg.BackgroundRegistry()
        defn = _definition(properties={bg.BACKGROUND_KEY: {"type": "string"}})
        with pytest.raises(ValueError, match="two things"):
            bg.backgroundable("slow", reg, lambda c, r: None, definition=defn)


class _FakeAPI:
    """The narrowest ``ExtensionAPI`` stand-in ``register_backgrounding`` touches."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Record the config and prepare the capture lists.

        Args:
            config: The per-extension config slice.
        """
        self.config = config
        self.registered: list[dict[str, Any]] = []
        self.hooks: dict[str, Any] = {}
        self.submitted: list[dict[str, Any]] = []

    def register_tool(self, definition: dict[str, Any]) -> None:
        """Capture a registration.

        Args:
            definition: The tool definition.
        """
        self.registered.append(definition)

    def on(self, event: str, handler: Any) -> Any:
        """Capture a hook binding.

        Args:
            event: Hook name.
            handler: The handler.

        Returns:
            A no-op unsubscribe.
        """
        self.hooks[event] = handler
        return lambda: None

    async def submit(self, text: str, **kwargs: Any) -> None:
        """Capture a submission.

        Args:
            text: The utterance.
            **kwargs: The submission options.
        """
        self.submitted.append({"text": text, **kwargs})


class TestRegisterBackgrounding:
    """The extension entry point reads config and binds the lifecycle hooks."""

    def test_wraps_every_tool_named_in_config(self):
        api = _FakeAPI({"tools": ["read", "ls"]})
        bg.register_backgrounding(api)
        assert [d["name"] for d in api.registered] == ["read", "ls"]
        for defn in api.registered:
            assert bg.BACKGROUND_KEY in defn["parameters"]["properties"]

    def test_binds_both_lifecycle_hooks(self):
        api = _FakeAPI({"tools": ["read"]})
        bg.register_backgrounding(api)
        assert set(api.hooks) == {"session_start", "session_shutdown"}

    def test_naming_no_tools_raises(self):
        api = _FakeAPI({})
        with pytest.raises(ValueError, match="no tools named"):
            bg.register_backgrounding(api)

    def test_definitions_are_merged_with_names(self):
        api = _FakeAPI({"tools": ["read"]})
        bg.register_backgrounding(api, definitions={"recall": _definition("recall")})
        assert [d["name"] for d in api.registered] == ["read", "recall"]

    async def test_a_completion_submits_with_steer_and_correlation(self):
        api = _FakeAPI({})
        reg = bg.register_backgrounding(api, definitions={"recall": _definition("recall")})
        execute = api.registered[0]["execute"]
        result = await execute("call-1", {"q": "x", bg.BACKGROUND_KEY: True})
        for _ in range(4):
            await asyncio.sleep(0)

        assert len(api.submitted) == 1
        sent = api.submitted[0]
        assert sent["multitask_strategy"] == "steer"
        assert sent["correlation"]["background_handle"] == result["details"]["background_handle"]
        assert sent["correlation"]["tool"] == "recall"
        assert reg.outstanding() == []

    async def test_the_shutdown_hook_bumps_the_epoch_and_cancels(self):
        api = _FakeAPI({})
        reg = bg.register_backgrounding(api, definitions={"recall": _definition("recall")})

        async def _never(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await asyncio.Event().wait()

        api.registered.clear()
        reg2 = bg.register_backgrounding(
            api, definitions={"recall": _definition("recall", execute=_never)}
        )
        await api.registered[0]["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        assert len(reg2.outstanding()) == 1

        await api.hooks["session_shutdown"]()

        assert reg2.epoch == 1
        assert reg2.outstanding() == []
        assert api.submitted == []
        assert reg.epoch == 0
