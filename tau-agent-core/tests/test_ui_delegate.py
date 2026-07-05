"""E5 §4 (S33) — the extension UI delegate + the veto's render signal.

Two claims, proven against real ``AgentSession`` / ``AgentLoop`` machinery:

1. ``AgentSession.set_ui_delegate`` flips the session's ONE shared
   ``ExtensionUI`` into TUI mode, so EVERY bound extension's ``api.ui.notify(...)``
   reaches the delegate (they all share ``_extension_api.context``). With NO
   delegate the same ``notify`` falls to the headless stderr sink.
2. A ``tool_call`` veto now emits a ``tool_execution_start`` (in addition to the
   ``tool_execution_end(is_error=True)`` it always emitted) — the render signal a
   front-end needs to mount a widget for the blocked call. Before S33 the veto
   emitted only the end event, so the TUI had no ToolBox to fold the blocked
   result into and dropped it silently.

Reference: docs/EXTENSIONS-E5-WIRING.md §4, S33.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


# ---------------------------------------------------------------------------
# 1. set_ui_delegate wiring
# ---------------------------------------------------------------------------


class _RecordingDelegate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.calls.append((message, level))


def test_set_ui_delegate_wires_notify_for_every_bound_extension() -> None:
    """One ``set_ui_delegate`` call routes ALL bound extensions' notify to the delegate."""
    apis: list[Any] = []

    def ext_a(api: Any) -> None:
        apis.append(api)

    def ext_b(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext_a, ext_b],
    )
    delegate = _RecordingDelegate()
    session.set_ui_delegate(delegate)

    # The shared ExtensionUI flipped into TUI mode, and BOTH bound apis expose it.
    assert session._extension_api.context._ui._mode == "tui"
    assert apis[0].ui is apis[1].ui

    apis[0].ui.notify("from a", "warning")
    apis[1].ui.notify("from b", "error")

    assert delegate.calls == [("from a", "warning"), ("from b", "error")]


def test_headless_notify_falls_to_stderr_when_no_delegate(capsys) -> None:
    """With no delegate set, ``api.ui.notify`` surfaces on stderr (headless sink)."""
    apis: list[Any] = []

    def ext(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext],
    )
    assert session._extension_api.context._ui._mode == "headless"

    apis[0].ui.notify("headless message", "error")

    err = capsys.readouterr().err
    assert "headless message" in err
    assert "error" in err


# ---------------------------------------------------------------------------
# 1b. Headless dialog policy on the shared session UI (S48)
# ---------------------------------------------------------------------------


async def test_headless_dialog_raises_by_default_for_bound_extension() -> None:
    """A bound extension's ``ctx.ui.confirm`` RAISES headless with no policy (S48)."""
    from tau_agent_core.extension_types import HeadlessDialogError

    apis: list[Any] = []

    def ext(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext],
    )
    # No delegate, no policy → Fail-Early raise rather than a silent auto-approve.
    import pytest

    with pytest.raises(HeadlessDialogError):
        await apis[0].ui.confirm("Delete?", "are you sure")


async def test_set_headless_ui_defaults_honored_for_every_bound_extension() -> None:
    """One ``set_headless_ui_defaults`` call governs ALL bound extensions (S48)."""
    apis: list[Any] = []

    def ext_a(api: Any) -> None:
        apis.append(api)

    def ext_b(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext_a, ext_b],
    )
    session.set_headless_ui_defaults({"confirm": "yes", "select": "first", "input": "default"})

    # Both bound apis share the one shared ExtensionUI, so the policy reaches both.
    assert apis[0].ui is apis[1].ui
    assert await apis[0].ui.confirm("t", "m") is True
    assert await apis[1].ui.select("t", ["x", "y"]) == "x"
    assert await apis[0].ui.input("t", "def") == "def"


def test_set_headless_ui_defaults_rejects_bad_policy() -> None:
    """An invalid policy token surfaces as ``ValueError`` (Fail-Early, S48)."""
    import pytest

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
    )
    with pytest.raises(ValueError):
        session.set_headless_ui_defaults({"confirm": "maybe"})


# ---------------------------------------------------------------------------
# 2. A veto emits a render signal (tool_execution_start)
# ---------------------------------------------------------------------------


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


async def test_veto_emits_start_and_error_end_so_the_tui_can_render_it() -> None:
    """A vetoed call emits BOTH a tool_execution_start and an is_error end.

    The start is what lets the front-end mount a ToolBox; the is_error end folds
    in as the visibly-blocked result. Before S33 only the end was emitted, so the
    veto had no widget and was dropped.
    """
    events: list[Any] = []

    def ext(api: Any) -> None:
        api.register_tool(
            {
                "name": "guarded",
                "description": "a guarded tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": lambda *a, **k: {"content": [{"type": "text", "text": "ran"}]},
            }
        )

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext],
    )
    session._extension_runner.register_extension("mem:veto").on(
        "tool_call",
        lambda event, ctx: {"block": True, "reason": "denied by policy"},
    )
    session.subscribe(lambda e: events.append(e))

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("guarded", {}),
    ):
        await session.prompt("call the guarded tool")

    starts = [
        e
        for e in events
        if e.type == "tool_execution_start" and e.tool_name == "guarded"
    ]
    ends = [
        e
        for e in events
        if e.type == "tool_execution_end" and e.tool_name == "guarded"
    ]
    # The vetoed call surfaces a start (render signal) paired with an is_error end.
    assert len(starts) == 1
    assert len(ends) == 1
    assert ends[0].is_error is True
    assert ends[0].tool_call_id == starts[0].tool_call_id
    assert "denied by policy" in str(ends[0].result)
