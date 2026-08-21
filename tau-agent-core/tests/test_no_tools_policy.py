"""``no_tools`` — the one value that separates ``--no-tools`` from ``-nbt``.

Ported from pi's regression #3592
(``coding-agent/test/suite/regressions/3592-no-builtin-tools-keeps-extension-tools.test.ts``),
which pins the same three cases: extension tools survive ``"builtin"``, nothing
survives ``"all"``, and the value propagates through the construction seam.

The τ defect this closes: ``_build_turn_tools`` merged extension-registered tools
in unconditionally, so an extension tool was offered under ``--no-tools`` exactly
as it was under ``--no-builtin-tools`` — the two flags were one flag, and nothing
in the tree said so.

The last test here is the one that makes the distinction worth having: under
``"all"`` the extension is still LOADED and its hooks still fire and still mutate
the turn. ``--no-tools`` withholds callable tools; it does not disable extensions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.sdk import _resolve_tools
from tau_agent_core.session_log import InMemorySessionLog

_BUILTINS = ["read", "write", "edit", "bash", "ls", "grep", "find"]


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


async def _echo(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}


def _tool_registering_ext(api) -> None:
    """The extension under test: it registers one callable tool."""
    api.register_tool(
        {
            "name": "dynamic_tool",
            "label": "Dynamic Tool",
            "description": "Tool registered by an extension",
            "parameters": {"type": "object", "properties": {}},
            "execute": _echo,
        }
    )


def _session(no_tools: str | None, *, builtins: list[str]) -> AgentSession:
    """Build a session the way ``TauBackend`` does.

    ``builtins`` is what ``resolve_tool_names`` already produced — BOTH flags
    empty it, which is exactly why the built-in list cannot distinguish them and
    ``no_tools`` has to.
    """
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=_resolve_tools(builtins) if builtins else [],
        extensions=[_tool_registering_ext],
        no_tools=no_tools,  # type: ignore[arg-type]
    )


def test_no_builtin_tools_keeps_the_extension_tool() -> None:
    """``-nbt``: built-ins gone, the extension's tool still offered."""
    session = _session("builtin", builtins=[])
    assert [t.name for t in session._build_turn_tools()] == ["dynamic_tool"]


def test_no_tools_withholds_the_extension_tool_too() -> None:
    """``-nt``: zero tools. This is the whole difference between the two flags."""
    session = _session("all", builtins=[])
    assert session._build_turn_tools() == []


def test_neither_flag_leaves_todays_behaviour_untouched() -> None:
    """Non-vacuity: with no policy, built-ins AND the extension tool are offered."""
    session = _session(None, builtins=_BUILTINS)
    assert [t.name for t in session._build_turn_tools()] == [*_BUILTINS, "dynamic_tool"]


def test_no_tools_suppression_does_not_skip_the_duplicate_check() -> None:
    """The suppression sits AFTER the duplicate scan on purpose.

    ``_build_turn_tools``'s docstring promises a broken tool list fails the same
    way regardless of what else is loaded. Returning early above the scan would
    have made ``--no-tools`` quietly mean "and stop validating", hiding a
    duplicate-name bug until the flag came off.
    """
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=_resolve_tools(["read"]) + _resolve_tools(["read"]),
        no_tools="all",
    )
    with pytest.raises(ValueError, match="Duplicate tool name 'read'"):
        session._build_turn_tools()


def test_unrecognised_policy_is_refused_rather_than_ignored() -> None:
    """Fail-Early: a typo'd policy would otherwise silently mean "no suppression"."""
    with pytest.raises(ValueError, match="no_tools must be"):
        AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            no_tools="none",  # type: ignore[arg-type]
        )


# ── the half that makes the change worth making ────────────────────────────


class _Stream:
    """Minimal async stream matching the ``stream_simple`` contract."""

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


async def test_under_no_tools_the_extension_still_loads_hooks_and_injections() -> None:
    """``--no-tools`` withholds TOOLS, not extensions.

    Run a real turn with ``no_tools="all"`` against an extension that registers a
    tool *and* subscribes a mutating ``before_agent_start`` hook, and assert three
    things about the same turn:

    - the model was offered no tools at all (not even the extension's), and
    - the hook fired anyway, and
    - its durable message injection reached the model's context.

    Prose in a docstring cannot establish that; a run that would have passed with
    extensions disabled would be worthless here, so the injected marker is checked
    in the payload the fake provider received.
    """
    fired: list[str] = []
    seen_tools: list[Any] = []

    def ext(api) -> None:
        _tool_registering_ext(api)
        api.on(
            "before_agent_start",
            lambda event, ctx: (
                fired.append("before_agent_start"),
                {"message": {"customType": "note", "content": "HOOK-MARKER"}},
            )[1],
        )

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=[],  # both flags empty the built-ins; "all" is what adds the rest
        extensions=[ext],
        no_tools="all",
    )

    final = AssistantMessage(
        content=[TextContent(text="done")],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )

    seen_messages: list[Any] = []

    async def fake(model, context, options=None):
        seen_tools.append(context.get("tools"))
        seen_messages.append(context.get("messages"))
        return _Stream([DoneEvent(final=final, usage=Usage())])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("say hi")

    # 1. Zero tools offered — the extension's tool was suppressed with the built-ins.
    assert seen_tools and all(t in (None, []) for t in seen_tools)
    # 2. The hook ran.
    assert fired == ["before_agent_start"]
    # 3. And its injection reached the payload the provider was handed — the
    #    hook's effect on the turn, not merely the fact that it was called.
    assert any("HOOK-MARKER" in str(m) for m in seen_messages)
