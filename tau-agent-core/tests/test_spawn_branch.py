"""ctx.spawn_branch — a sub-agent is an owned cursor on the spawner's session.

docs/CURSORS.md §6, from JMFTS-INTEGRATION-PLAN.md §9.2. The sub-agent's turn is an
ordinary submission on its own cursor, run under a TurnFrame, so the provider is
faked at ``stream_simple`` and everything else is real. What is pinned is the
behaviour that must not SILENTLY regress: tool scoping, failure containment, the
structural isolation of a branch's work, and the cursor's lifecycle.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentToolResult
from tau_llm.types import Model


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.label = name
        self.description = f"the {name} tool"
        self.parameters = {"type": "object", "properties": {}}
        self.execution_mode = "parallel"
        self.calls: list[dict] = []

    async def execute(self, tool_call_id, args, signal=None, on_update=None):
        self.calls.append(args)
        return AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": "ok"}],
        ).model_dump()


async def _session(tools: list) -> tuple[AgentSession, InMemorySessionLog]:
    log = InMemorySessionLog()
    session = AgentSession(
        session_log=log, model=_model(), system_prompt="", tools=tools, api_key="k"
    )
    await session.cursor.append_message(
        {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
    )
    return session, log


class _Stream:
    def __init__(self, message: AssistantMessage) -> None:
        self._message = message

    def __aiter__(self):
        async def _gen():
            text = "".join(b.text for b in self._message.content if isinstance(b, TextContent))
            yield TextDeltaEvent(delta=text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


def _reply(text: str, calls: list[ToolCall] | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text), *(calls or [])],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="toolUse" if calls else "stop",
        timestamp=1_700_000_000_000,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Provider:
    """A ``stream_simple`` stand-in that records every request it was sent."""

    def __init__(self, answer: Any = "verdict") -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.requests.append(context)
        answer = self.answer(len(self.requests)) if callable(self.answer) else self.answer
        if isinstance(answer, BaseException):
            raise answer
        return _Stream(answer if isinstance(answer, AssistantMessage) else _reply(answer))

    def tool_names(self, index: int = 0) -> list[str]:
        return [t.name for t in self.requests[index]["tools"] or []]

    def system_prompt(self, index: int = 0) -> str:
        first = self.requests[index]["messages"][0]
        return first["content"] if isinstance(first, dict) and first.get("role") == "system" else ""


def _lifecycle(session: AgentSession) -> list[tuple[str, Any]]:
    """Every cursor_open / cursor_close this session's bus publishes."""
    seen: list[tuple[str, Any]] = []
    session._events.on("cursor_open", lambda cursor: seen.append(("open", cursor)))
    session._events.on("cursor_close", lambda cursor: seen.append(("close", cursor)))
    return seen


async def test_asking_for_an_unavailable_tool_raises_before_any_model_call():
    """Fail-Early, and BEFORE the model runs. A sub-agent silently missing a tool it was
    told to use does not error — it returns a confident wrong answer ("I couldn't find
    it"), which reads exactly like a real verdict."""
    session, log = await _session([_Tool("lookup")])
    before = [e["id"] for e in log.entries()]
    lifecycle = _lifecycle(session)

    with pytest.raises(ValueError, match="not available on this session"):
        await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["lookup", "bash"]
        )

    assert [e["id"] for e in log.entries()] == before, "nothing was written"
    assert lifecycle == [], "no cursor was opened for a sub-agent that never started"


async def test_the_allowlist_is_a_hard_filter():
    """Sub-agents share the process and cwd, so 'inherit the parent's tools' would hand a
    retrieval evaluator `write` and `bash`. Only the named tools reach the provider."""
    session, log = await _session([_Tool("lookup"), _Tool("write")])
    provider = _Provider()

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["lookup"]
        )

    assert result.ok
    assert provider.tool_names() == ["lookup"], "write must not be handed to the sub-agent"


async def _extension_session() -> tuple[AgentSession, InMemorySessionLog]:
    """A session in the shape a host uses when it owns every tool it offers.

    ``tools=[]`` plus ``no_tools="builtin"`` suppresses the built-ins and leaves
    extension registrations alone — the supported way to hand a model a small,
    purpose-built vocabulary and nothing else. It is also the shape under which
    ``session._tools`` is empty while the model is being offered two tools.
    """
    log = InMemorySessionLog()
    session = AgentSession(
        session_log=log,
        model=_model(),
        system_prompt="",
        tools=[],
        no_tools="builtin",
        api_key="k",
    )

    async def _execute(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "ok"}]}

    for name in ("say", "remember"):
        session._extension_api.register_tool(
            {
                "name": name,
                "description": f"the {name} tool",
                "parameters": {"type": "object", "properties": {}},
                "execute": _execute,
            }
        )
    await session.cursor.append_message(
        {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
    )
    return session, log


async def test_a_branch_may_hold_a_tool_that_came_from_an_extension():
    """The allowlist is checked against what a TURN offers, not against `_tools`.

    `_tools` is only the constructor's list; an extension's registrations are merged in
    by `_build_turn_tools`. On a session whose tools all arrive that way — `tools=[]`
    plus `no_tools="builtin"` — reading `_tools` made every non-empty allowlist raise.
    """
    session, log = await _extension_session()
    assert [t.name for t in session._tools] == [], "the shape the bug needs"
    provider = _Provider()

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["remember"]
        )

    assert result.ok
    assert provider.tool_names() == ["remember"], "scoping still applies — `say` must not cross"


async def test_the_refusal_still_fires_and_names_the_extension_tools():
    """The `available:` list is what makes the refusal actionable."""
    session, log = await _extension_session()

    with pytest.raises(ValueError, match=r"\['bash'\].*available: \['remember', 'say'\]"):
        await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["remember", "bash"]
        )


async def test_a_failing_sub_agent_is_contained_and_marks_its_branch():
    """§9.2/5. A raise here would mean one bad evaluator in a fan-out kills the
    spawner's whole turn. The failure comes back as a RESULT, and the branch records it."""
    session, log = await _session([])
    tip = session.cursor.leaf
    provider = _Provider(RuntimeError("the sub-agent exploded"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(tip, "go", tools=[])

    assert result.ok is False
    assert "exploded" in (result.error or "")
    assert session.cursor.leaf == tip, "a failed branch must not move the spawner's cursor"
    marks = [e for e in log.entries() if e.get("customType") == "branch_error"]
    assert len(marks) == 1, "the branch is marked, so the failure is visible in the tree"
    assert marks[0]["data"] == {"label": result.label, "error": result.error}


async def test_the_sub_agents_work_never_reaches_the_spawners_context():
    session, log = await _session([])
    tip = session.cursor.leaf
    provider = _Provider("SUB-AGENT ONLY")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(tip, "go", tools=[])

    assert result.ok is True
    assert all("branchOf" not in e for e in log.entries()), "no durable branch marker"
    assert session.cursor.leaf == tip, "the spawner's cursor did not move"
    assert "SUB-AGENT ONLY" not in str(session.cursor.context())
    assert result.leaf is not None
    assert "SUB-AGENT ONLY" in str(Cursor(log, result.leaf).context())


async def test_the_sub_agents_frame_is_recorded_where_its_turn_begins():
    """docs/CURSORS.md §6: the frame is a config entry on the branch, not a constructor
    argument nobody can read back."""
    session, log = await _session([_Tool("lookup"), _Tool("write")])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider().stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["lookup"]
        )

    from tau_agent_core.session_log import config_at

    assert result.leaf is not None
    assert config_at(log.entries(), result.leaf)["tools"] == ["lookup"]
    assert config_at(log.entries(), session.cursor.leaf).get("tools") != ["lookup"]


async def test_system_prompt_defaults_to_the_parents_but_can_be_overridden():
    """W1 (NODE-ADDRESSABLE-AGENTS.md §5/W1): passing a prompt forks with a different
    spec; omitting it runs under the spawner's."""
    session, log = await _session([])
    session._system_prompt = "the parent's prompt"
    provider = _Provider()

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await session._extension_api.context.spawn_branch(session.cursor.leaf, "go", tools=[])
        await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=[], system_prompt="you are a critic"
        )

    assert provider.system_prompt(0) == "the parent's prompt"
    assert provider.system_prompt(1) == "you are a critic"


async def test_max_turns_bounds_the_sub_agent():
    """A looping sub-agent must not be able to burn the spawner's budget."""
    session, log = await _session([_Tool("lookup")])
    provider = _Provider(
        lambda n: _reply("again", [ToolCall(id=f"c{n}", name="lookup", arguments={})])
    )

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=["lookup"], max_turns=3
        )

    assert result.ok, result.error
    assert len(provider.requests) == 3


async def test_a_sub_agent_opens_and_closes_exactly_one_owned_cursor():
    session, log = await _session([])
    lifecycle = _lifecycle(session)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider().stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=[]
        )

    (kind_open, opened), (kind_close, closed) = lifecycle
    assert (kind_open, kind_close) == ("open", "close")
    assert opened is closed and opened.id == result.cursor_id
    assert opened.owner is session.cursor
    assert opened not in session.cursors


async def test_a_failing_sub_agent_still_closes_its_cursor():
    session, log = await _session([])
    lifecycle = _lifecycle(session)
    provider = _Provider(RuntimeError("the provider dropped the connection"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=[]
        )

    assert result.ok is False
    assert [kind for kind, _ in lifecycle] == ["open", "close"]


async def test_a_cancelled_sub_agent_still_closes_its_cursor():
    """``abort()`` cancels every forked task and ``CancelledError`` is not an
    ``Exception``, so the containment handler never sees it — the ``finally`` does."""
    session, log = await _session([])
    lifecycle = _lifecycle(session)
    running = asyncio.Event()

    async def _hang(model: Any, context: Any, options: Any = None) -> _Stream:
        running.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_hang):
        task = asyncio.get_running_loop().create_task(
            session._extension_api.context.spawn_branch(session.cursor.leaf, "go", tools=[])
        )
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [kind for kind, _ in lifecycle] == ["open", "close"]


async def test_aborting_the_spawner_aborts_the_sub_agent():
    """The owner relation is the abort cascade (docs/CURSORS.md §6)."""
    session, log = await _session([])
    lifecycle = _lifecycle(session)
    running = asyncio.Event()
    release = asyncio.Event()

    async def _gated(model: Any, context: Any, options: Any = None) -> _Stream:
        running.set()
        await release.wait()
        return _Stream(_reply("late"))

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_gated):
        task = asyncio.get_running_loop().create_task(
            session._extension_api.context.spawn_branch(session.cursor.leaf, "go", tools=[])
        )
        await running.wait()
        sub_cursor = lifecycle[0][1]
        session.abort(session.cursor)
        assert sub_cursor.abort_signal.is_aborted()
        release.set()
        await task


async def test_every_event_of_the_sub_agents_turn_carries_its_cursor():
    session, log = await _session([])
    seen: list[Any] = []
    session.subscribe(seen.append)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_Provider().stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, "go", tools=[]
        )

    assert seen and {e.cursor_id for e in seen} == {result.cursor_id}
    assert {e.source for e in seen} == {"agent"}
