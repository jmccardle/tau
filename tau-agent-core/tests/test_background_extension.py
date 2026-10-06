"""Tests for ``tau_agent_core.extensions_builtin.background`` — backgrounding conferred on a tool.

Five things have to hold, and each is a claim the module's docstring makes:

* **Conferring costs nothing until elected.** A wrapped tool with ``background``
  absent or false delegates to the original, with the flag stripped from what the
  original sees, and the schema gains exactly one property.
* **Electing it returns at once.** The tool call returns a handle while the work is
  still running, and the provenance — handle, tool, args, start time — rides
  ``details``.
* **The completion is delivered through steer, once.** The supervised task calls
  the submit closure with the finished result.
* **A result outlives its session, labelled.** Against a real ``AgentSession`` and
  a real ``new_session``, a completion lands in the new conversation and says which
  session, tool, arguments and start time it came from.
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
from typing import Any

import pytest

from tau_agent_core.submission import (
    DRIVING_SUBMISSION_DEPTH,
    MAX_SUBMISSION_DEPTH,
    next_submission_depth,
)

from tau_agent_core.extensions_builtin import background as bg

_SID = "session-a"


def _registry(**kwargs: Any) -> bg.BackgroundRegistry:
    """A registry pinned to session :data:`_SID`.

    Args:
        **kwargs: Passed through, e.g. ``on_error``.

    Returns:
        The registry.
    """
    return bg.BackgroundRegistry(session_id=lambda: _SID, **kwargs)


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
        reg = _registry()
        defn = bg.backgroundable("slow", reg, lambda c, r: None, definition=_definition())
        props = defn["parameters"]["properties"]
        assert set(props) == {"q", bg.BACKGROUND_KEY}
        assert props[bg.BACKGROUND_KEY]["type"] == "boolean"

    def test_the_original_schema_is_not_mutated(self):
        reg = _registry()
        original = _definition()
        bg.backgroundable("slow", reg, lambda c, r: None, definition=original)
        assert bg.BACKGROUND_KEY not in original["parameters"]["properties"]

    def test_description_and_label_default_to_the_wrapped_tool(self):
        reg = _registry()
        defn = bg.backgroundable("slow", reg, lambda c, r: None, definition=_definition())
        assert defn["description"] == "the slow tool"
        assert defn["label"] == "Slow"

    async def test_absent_flag_delegates_and_strips_nothing_else(self):
        seen: dict[str, Any] = {}

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            seen.update(params)
            return _text_result("ran")

        reg = _registry()
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

        reg = _registry()
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
        reg = _registry()
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

        reg = _registry()
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
        reg = _registry()
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
        call = bg.BackgroundCall(
            handle="bg-slow-1", tool="slow", args={}, started_at_ms=1, origin_session_id=_SID
        )
        failed = {"content": [{"type": "text", "text": "no such host"}], "is_error": True}
        text = bg.completion_text(call, failed)
        assert "failed" in text
        assert "finished" not in text
        assert "no such host" in text

    def test_a_non_dict_result_still_renders(self):
        call = bg.BackgroundCall(
            handle="bg-slow-1", tool="slow", args={}, started_at_ms=1, origin_session_id=_SID
        )
        assert "42" in bg.completion_text(call, 42)


class TestErrors:
    """A raising tool is delivered as failed; only the delivery path reaches the error sink."""

    async def test_a_raising_tool_is_delivered_as_a_failed_result(self):
        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            raise RuntimeError("the tool broke")

        errors: list[BaseException] = []
        delivered: list[Any] = []
        reg = _registry(on_error=errors.append)
        defn = bg.backgroundable(
            "slow",
            reg,
            lambda c, r: delivered.append(bg.completion_text(c, r)),
            definition=_definition(execute=_exec),
        )
        await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert errors == []
        assert len(delivered) == 1
        assert "slow failed" in delivered[0]
        assert "RuntimeError: the tool broke" in delivered[0]

    async def test_a_raising_delivery_reaches_the_error_sink_and_parks(self):
        errors: list[BaseException] = []
        reg = _registry(on_error=errors.append)

        def _deliver(call, result):
            raise RuntimeError("delivery broke")

        defn = bg.backgroundable("slow", reg, _deliver, definition=_definition())
        await defn["execute"]("call-1", {bg.BACKGROUND_KEY: True})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [str(e) for e in errors] == ["delivery broke"]
        assert reg.jobs()[0].state == bg.PARKED

    def test_the_default_error_sink_raises_rather_than_dropping(self):
        reg = _registry()
        with pytest.raises(ValueError, match="boom"):
            reg.on_error(ValueError("boom"))


class TestSubmissionDepth:
    """The spawned task must not inherit the spawning turn's submission depth."""

    async def test_depth_is_cleared_inside_the_spawned_task(self):
        seen: list[int] = []

        async def _exec(tool_call_id, params, signal=None, on_update=None, ctx=None):
            seen.append(next_submission_depth(0))
            return _text_result("ok")

        reg = _registry()
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

        reg = _registry()

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
        reg = _registry(on_error=lambda exc: None)
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
        reg = _registry()
        assert await reg.cancel_all() == 0


class TestFailEarly:
    """Every refusal the module documents."""

    def test_unknown_builtin_name_raises(self):
        reg = _registry()
        with pytest.raises(ValueError, match="unknown built-in tool"):
            bg.backgroundable("nope-not-a-tool", reg, lambda c, r: None, cwd="/tmp")

    def test_a_builtin_named_without_a_cwd_raises(self):
        reg = _registry()
        with pytest.raises(ValueError, match="needs cwd="):
            bg.backgroundable("read", reg, lambda c, r: None)

    def test_definition_and_tool_together_raise(self):
        reg = _registry()
        with pytest.raises(ValueError, match="not both"):
            bg.backgroundable(
                "slow", reg, lambda c, r: None, definition=_definition(), tool=object()
            )

    def test_definition_missing_execute_raises(self):
        reg = _registry()
        defn = _definition()
        del defn["execute"]
        with pytest.raises(ValueError, match="missing required key 'execute'"):
            bg.backgroundable("slow", reg, lambda c, r: None, definition=defn)

    def test_a_tool_already_declaring_the_flag_raises(self):
        reg = _registry()
        defn = _definition(properties={bg.BACKGROUND_KEY: {"type": "string"}})
        with pytest.raises(ValueError, match="two things"):
            bg.backgroundable("slow", reg, lambda c, r: None, definition=defn)


class _FakeCursor:
    """The cursor surface ``CursorDelivery`` reads, with no tree behind it."""

    def __init__(self, session_id: str = _SID, cursor_id: str = "c-head") -> None:
        """A cursor on ``session_id`` at no leaf."""
        self.id = cursor_id
        self.leaf = None
        self.owner = None
        self.label = ""
        self.session_id = session_id
        self.turn_lock = asyncio.Lock()
        self.steer_queue: list[Any] = []
        self.next_turn_queue: list[str] = []

    @property
    def busy(self) -> bool:
        """Whether the fake turn lock is held."""
        return self.turn_lock.locked()

    def entries(self) -> list[dict[str, Any]]:
        """No entries."""
        return []


class _FakeContext:
    """The ``ExtensionContext`` reads ``register_backgrounding`` makes."""

    def __init__(self) -> None:
        """Start on session :data:`_SID` in ``/tmp``, with one live cursor."""
        self.session_id = _SID
        self.cwd = "/tmp"
        self.cursor = _FakeCursor()
        self.cursors: tuple[Any, ...] = (self.cursor,)


class _FakeUI:
    """Captures notifications."""

    def __init__(self) -> None:
        """Start empty."""
        self.notes: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        """Capture one notification."""
        self.notes.append((message, level))


class _FakeAPI:
    """The narrowest ``ExtensionAPI`` stand-in ``register_backgrounding`` touches."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Record the config and prepare the capture lists.

        Args:
            config: The per-extension config slice.
        """
        self.config = config
        self.context = _FakeContext()
        self.ui = _FakeUI()
        self.registered: list[dict[str, Any]] = []
        self.hooks: dict[str, Any] = {}
        self.submitted: list[dict[str, Any]] = []

    def register_tool(self, definition: dict[str, Any]) -> None:
        """Capture a registration."""
        self.registered.append(definition)

    def on(self, event: str, handler: Any) -> Any:
        """Capture a hook binding; returns a no-op unsubscribe."""
        self.hooks[event] = handler
        return lambda: None

    async def submit(self, text: str, **kwargs: Any) -> Any:
        """Capture a submission and report it as a turn that ran."""
        from types import SimpleNamespace

        self.submitted.append({"text": text, **kwargs})
        return SimpleNamespace(accepted=True, messages=[{"role": "user"}], rejection_reason=None)


class TestRegisterBackgrounding:
    """The extension entry point reads config and binds the lifecycle hooks."""

    def test_wraps_every_tool_named_in_config(self):
        api = _FakeAPI({"tools": ["read", "ls"]})
        bg.register_backgrounding(api)
        assert [d["name"] for d in api.registered] == ["read", "ls", "bg"]
        for defn in api.registered[:2]:
            assert bg.BACKGROUND_KEY in defn["parameters"]["properties"]

    def test_binds_only_the_shutdown_hook(self):
        api = _FakeAPI({"tools": ["read"]})
        bg.register_backgrounding(api)
        assert set(api.hooks) == {"session_shutdown"}

    def test_a_named_builtin_is_constructed_for_the_sessions_cwd(self):
        api = _FakeAPI({"tools": ["read"]})
        api.context.cwd = "/srv/elsewhere"
        bg.register_backgrounding(api)
        assert api.registered[0]["name"] == "read"
        tool = bg._resolve_builtin("read", "/srv/elsewhere")
        assert tool.cwd == "/srv/elsewhere"

    def test_naming_no_tools_raises(self):
        api = _FakeAPI({})
        with pytest.raises(ValueError, match="no tools named"):
            bg.register_backgrounding(api)

    def test_definitions_are_merged_with_names(self):
        api = _FakeAPI({"tools": ["read"]})
        bg.register_backgrounding(api, definitions={"recall": _definition("recall")})
        assert [d["name"] for d in api.registered] == ["read", "recall", "bg"]

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
        assert sent["correlation"]["origin_session_id"] == _SID
        assert sent["correlation"]["crossed_session"] is False
        assert sent["correlation"]["route"] == "origin"
        assert sent["cursor"] is api.context.cursor
        assert reg.outstanding() == []

    async def test_a_completion_after_the_session_changed_is_labelled(self):
        gate = asyncio.Event()

        async def _later(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("the late answer")

        api = _FakeAPI({})
        bg.register_backgrounding(
            api, definitions={"recall": _definition("recall", execute=_later)}
        )
        await api.registered[0]["execute"]("call-1", {"q": "x", bg.BACKGROUND_KEY: True})
        api.context.session_id = "session-b"
        api.context.cursor = _FakeCursor("session-b", "c-new")
        api.context.cursors = (api.context.cursor,)
        gate.set()
        for _ in range(4):
            await asyncio.sleep(0)

        sent = api.submitted[0]
        assert sent["correlation"]["origin_session_id"] == _SID
        assert sent["correlation"]["crossed_session"] is True
        assert "from an earlier session" in sent["text"]
        assert _SID in sent["text"]
        assert '{"q": "x"}' in sent["text"]
        assert "the late answer" in sent["text"]
        assert sent["cursor"] is None

    async def test_the_shutdown_hook_cancels(self):
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

        assert reg2.outstanding() == []
        assert api.submitted == []
        assert reg.outstanding() == []


class TestRealSessionBoundary:
    """Against a real ``AgentSession`` and a real ``AgentSessionRuntime`` swap.

    A swap fires ``session_before_switch`` and no lifecycle hook, so these are the
    tests a hook-driven check would have passed while production did otherwise.
    """

    @staticmethod
    def _session(execute: Any) -> tuple[Any, Any, Any]:
        """A real session with ``recall`` backgrounded, its runtime, and its api.

        Args:
            execute: The wrapped ``recall`` execute.

        Returns:
            ``(session, runtime, holder)``; ``holder["registry"]`` is the registry.
        """
        from test_agent_session_runtime import _FakeCatalog, _model, _runtime

        from tau_agent_core.agent_session import AgentSession

        holder: dict[str, Any] = {}

        def _factory(api: Any) -> None:
            holder["registry"] = bg.register_backgrounding(
                api, definitions={"recall": _definition("recall", execute=execute)}
            )

        catalog = _FakeCatalog()
        session = AgentSession(
            session_log=catalog.create("/work", "m", "openai"),
            model=_model(),
            tools=[],
            extensions=[_factory],
        )
        return session, _runtime(session, catalog), holder

    @staticmethod
    def _user_texts(session: Any) -> list[str]:
        """Every user message's text in the session's current log.

        Args:
            session: The ``AgentSession``.

        Returns:
            The texts, in log order.
        """
        texts = []
        for entry in session.session_log.entries():
            message = entry.get("message") or {}
            if entry.get("type") == "message" and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                texts.append(content)
        return texts

    async def test_a_result_landing_after_new_session_is_delivered_there_labelled(self):
        from unittest.mock import patch

        from test_agent_session_runtime import _Stream

        gate = asyncio.Event()

        async def _later(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("the late answer")

        session, runtime, holder = self._session(_later)
        origin = session._extension_api.context.session_id

        async def _stream_simple(model, context, options=None):
            return _Stream("noted")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            defn = [d for d in _registered(session) if d.name == "recall"][0]
            await defn.execute("call-1", {"q": "x", bg.BACKGROUND_KEY: True})

            swapped = await runtime.new_session(persist=False)
            assert swapped["cancelled"] is False
            assert session._extension_api.context.session_id != origin

            gate.set()
            for _ in range(50):
                await asyncio.sleep(0)
                if holder["registry"].outstanding() == [] and self._user_texts(session):
                    break

        texts = self._user_texts(session)
        assert len(texts) == 1
        assert "from an earlier session" in texts[0]
        assert origin in texts[0]
        assert "the late answer" in texts[0]

    async def test_a_result_in_the_same_session_wakes_it_unlabelled(self):
        from unittest.mock import patch

        from test_agent_session_runtime import _Stream

        session, runtime, holder = self._session(None)

        async def _stream_simple(model, context, options=None):
            return _Stream("noted")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_stream_simple):
            defn = [d for d in _registered(session) if d.name == "recall"][0]
            await defn.execute("call-1", {"q": "x", bg.BACKGROUND_KEY: True})
            for _ in range(50):
                await asyncio.sleep(0)
                if self._user_texts(session):
                    break

        texts = self._user_texts(session)
        assert len(texts) == 1
        assert texts[0].startswith("[background bg-recall-1] recall finished:")
        assert "earlier session" not in texts[0]

    async def test_a_result_queued_into_a_turn_the_swap_aborts_survives(self):
        """Was a pinned loss (ROADMAP "A queued steer is lost"); now it is followed.

        The completion lands while a turn is in flight, so ``steer`` queues it. The
        swap's ``abort()`` clears the queue. The delivery follows the queued steer
        until the old cursor is idle, finds it on no path, and re-routes it: to the
        old cursor's next turn if that cursor is still open (``deliver_queued``
        runs it on the old tree), else to the new head, labelled
        (docs/BACKGROUND-TOOLS.md §6).
        """
        from unittest.mock import patch

        from test_agent_session_runtime import _Stream, _sub

        gate = asyncio.Event()
        turn_gate = asyncio.Event()

        async def _later(tool_call_id, params, signal=None, on_update=None, ctx=None):
            await gate.wait()
            return _text_result("the late answer")

        session, runtime, holder = self._session(_later)
        old_cursor = session.cursor

        async def _gated(model, context, options=None):
            await turn_gate.wait()
            return _Stream("done")

        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated):
            defn = [d for d in _registered(session) if d.name == "recall"][0]
            await defn.execute("call-1", {"q": "x", bg.BACKGROUND_KEY: True})

            turn = asyncio.create_task(session.submit(_sub("hello", "t-1")))
            await asyncio.sleep(0)
            assert session.is_streaming is True

            gate.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert holder["registry"].outstanding() == []
            assert len(old_cursor.steer_queue) == 1

            swap = asyncio.create_task(runtime.new_session(persist=False))
            await asyncio.sleep(0)
            turn_gate.set()
            await asyncio.wait_for(turn, timeout=5.0)
            await asyncio.wait_for(swap, timeout=5.0)
            for _ in range(200):
                await asyncio.sleep(0.001)
                if not holder["registry"]._tasks:
                    break
            await session.wait_for_deliveries()

        call = holder["registry"].jobs()[0]
        assert call.state in (bg.DELIVERED, bg.HELD_OVER), (call.state, call.note)
        old_texts = [
            t for t in _texts_of(old_cursor.log) if "[background bg-recall-1" in t
        ]
        new_texts = [t for t in self._user_texts(session) if "[background bg-recall-1" in t]
        assert len(old_texts) + len(new_texts) == 1, (old_texts, new_texts)
        if new_texts:
            assert "from an earlier session" in new_texts[0]


def _texts_of(log: Any) -> list[str]:
    """Every user message's text in ``log``, in log order."""
    texts = []
    for entry in log.entries():
        message = entry.get("message") or {}
        if entry.get("type") == "message" and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            texts.append(content)
    return texts


def _registered(session: Any) -> list[Any]:
    """The extension tool definitions a real session resolves for a turn.

    Args:
        session: The ``AgentSession``.

    Returns:
        The ``ExtensionToolDefinition`` objects in its registry.
    """
    return list(session._extension_api._registry._tools.values())
