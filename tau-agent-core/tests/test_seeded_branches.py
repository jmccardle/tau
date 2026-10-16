"""Seeded branches: a sub-agent that continues an agent turn instead of answering a prompt.

docs/SEEDED-BRANCHES.md. A planner's tool spawns a sub-agent from inside its own
execute. Hanging the branch from the planner's pending call message sent the child
an unanswered call (Mistral HTTP 400); a seeded branch hangs from that message's
parent and starts from a note, an injected call and its result. The provider is
faked at ``stream_simple``; everything else is real.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable
from unittest.mock import patch

import pytest
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.seed import (
    SEED_ORIGIN,
    TOOL_CALL_ID_LENGTH,
    new_tool_call_id,
    seed_tool_call,
)
from tau_agent_core.session_log import InMemorySessionLog

NOTE = {"customType": "planner", "content": "You are the sub-agent now; do the task."}


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


def _reply(text: str, calls: list[ToolCall] | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text), *(calls or [])],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="toolUse" if calls else "stop",
        timestamp=1_700_000_000_000,
        usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
    )


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


def _plain(messages: list[Any]) -> list[dict[str, Any]]:
    return [m if isinstance(m, dict) else m.model_dump() for m in messages]


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


class _Provider:
    """Answers each request with ``route(messages)``, and records every request."""

    def __init__(self, route: Callable[[list[dict[str, Any]]], Any]) -> None:
        self.route = route
        self.requests: list[list[dict[str, Any]]] = []

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        messages = _plain(context["messages"])
        self.requests.append(messages)
        answer = self.route(messages)
        if asyncio.iscoroutine(answer):
            answer = await answer
        return _Stream(answer if isinstance(answer, AssistantMessage) else _reply(answer))

    def child_requests(self) -> list[list[dict[str, Any]]]:
        return [r for r in self.requests if any(_text(m) == NOTE["content"] for m in r)]


def _is_child(messages: list[dict[str, Any]]) -> bool:
    return any(_text(m) == NOTE["content"] for m in messages)


def _parent_of(ctx: Any, entry_id: str | None) -> str | None:
    return next(e.get("parentId") for e in ctx.entries() if e.get("id") == entry_id)


def _planner_session(execute: Callable[..., Any]) -> tuple[AgentSession, InMemorySessionLog]:
    """A session whose model may call ``delegate``, whose execute is ``execute``."""

    def factory(api: Any) -> None:
        api.register_tool(
            {
                "name": "delegate",
                "description": "Hand a task to a sub-agent.",
                "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
                "execute": execute,
            }
        )

    log = InMemorySessionLog()
    session = AgentSession(
        session_log=log,
        model=_model(),
        system_prompt="SYS",
        tools=[],
        api_key="k",
        extensions=[factory],
    )
    return session, log


async def _prefix_session() -> tuple[AgentSession, InMemorySessionLog, str]:
    """A plain session holding user → planner-call (unanswered); returns the call's entry id."""
    log = InMemorySessionLog()
    session = AgentSession(session_log=log, model=_model(), system_prompt="SYS", tools=[])
    await session.cursor.append_message(
        {"role": "user", "content": [{"type": "text", "text": "plan"}], "timestamp": 1}
    )
    pending = await session.cursor.append_message(
        _reply("", [ToolCall(id="callP", name="delegate", arguments={})]).model_dump()
    )
    return session, log, pending


# --- the end-to-end shape -------------------------------------------------------


async def test_a_planners_tool_runs_a_seeded_sub_agent_with_a_valid_context():
    """The probe's shape: the child sees exactly system → user → note → call → result,
    finishes, and the planner's own call is answered by its tool's result."""
    seen: dict[str, Any] = {}

    async def delegate(tool_call_id, params, signal, on_update, ctx):
        seen["planner_call_entry"] = ctx.cursor.leaf
        result = await ctx.spawn_branch(
            _parent_of(ctx, ctx.cursor.leaf),
            seed=[NOTE, *seed_tool_call("start_subagent", {}, params["task"])],
            tools=[],
        )
        seen["result"] = result
        return {
            "content": [{"type": "text", "text": f"sub-agent said: {_text(result.messages[-1])}"}]
        }

    session, log = _planner_session(delegate)

    def route(messages: list[dict[str, Any]]) -> Any:
        if _is_child(messages):
            return "child done"
        if messages[-1].get("role") == "toolResult":
            return "planner done"
        return _reply("", [ToolCall(id="callP", name="delegate", arguments={"task": "write it"})])

    provider = _Provider(route)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await session.prompt("plan this")

    (child,) = provider.child_requests()
    assert [m["role"] for m in child] == ["system", "user", "user", "assistant", "toolResult"]
    assert _text(child[1]) == "plan this"
    assert _text(child[2]) == NOTE["content"]
    (call,) = [b for b in child[3]["content"] if b.get("type") == "toolCall"]
    assert call["name"] == "start_subagent"
    assert child[4]["tool_call_id"] == call["id"]
    assert _text(child[4]) == "write it"

    result = seen["result"]
    assert result.ok, result.error
    assert _text(result.messages[-1]) == "child done"

    planner_final = provider.requests[-1]
    assert [m["role"] for m in planner_final] == ["system", "user", "assistant", "toolResult"]
    assert planner_final[3]["tool_call_id"] == "callP"
    assert _text(planner_final[3]) == "sub-agent said: child done"
    assert NOTE["content"] not in str(planner_final), "the branch never reaches the planner"
    assert _text(session.cursor.context()[-1]) == "planner done"


async def test_the_seed_is_written_on_the_branch_and_the_head_does_not_move():
    """Tree-as-truth: a reload of the log reads the seed on the branch's path."""
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)
    provider = _Provider(lambda messages: "child done")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session.spawn(
            user_entry, seed=[NOTE, *seed_tool_call("start_subagent", {}, "task")], tools=[]
        )

    assert result.ok, result.error
    assert session.cursor.leaf == pending, "the head's cursor stays on its own path"
    reloaded = InMemorySessionLog(log.id)
    reloaded._entries = log.entries()
    branch = Cursor(reloaded, result.leaf).context()
    assert [m["role"] for m in branch] == ["user", "custom", "assistant", "toolResult", "assistant"]
    assert branch[2]["provider"] == SEED_ORIGIN
    assert "callP" not in str(branch), "the planner's pending call is not on the branch"
    assert [m["role"] for m in result.messages] == [
        "custom",
        "assistant",
        "toolResult",
        "assistant",
    ]


async def test_the_frame_is_recorded_ahead_of_the_seed():
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)

    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_Provider(lambda m: "ok").stream
    ):
        result = await session.spawn(user_entry, seed=[NOTE], tools=[], system_prompt="CHILD")

    from tau_agent_core.conversation_tree import ConversationTree

    path = ConversationTree(log.entries(), result.leaf).path(result.leaf)
    kinds = [e["type"] if e["type"] != "customEntry" else e["customType"] for e in path]
    assert "config" in kinds[kinds.index("message") :], "the branch's frame is on its path"
    assert kinds.index("config", kinds.index("message")) < kinds.index("customMessage")


async def test_prompt_and_seed_together_put_the_prompt_after_the_seed():
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)
    provider = _Provider(lambda messages: "ok")

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session.spawn(
            user_entry,
            "now summarise it",
            seed=[*seed_tool_call("read", {"path": "a"}, "contents")],
            tools=[],
        )

    assert result.ok, result.error
    roles = [m["role"] for m in provider.requests[0]]
    assert roles == ["system", "user", "assistant", "toolResult", "user"]
    assert _text(provider.requests[0][-1]) == "now summarise it"


# --- validation: Fail-Early before anything is written ----------------------------


async def _refused(session: AgentSession, log: InMemorySessionLog, at: Any, **kw: Any) -> str:
    before = [e["id"] for e in log.entries()]
    opened: list[Any] = []
    session._events.on("cursor_open", opened.append)
    provider = _Provider(lambda messages: "never")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        with pytest.raises(ValueError) as caught:
            await session.spawn(at, tools=[], **kw)
    assert [e["id"] for e in log.entries()] == before, "nothing was written"
    assert provider.requests == [], "no model was called"
    assert opened == [], "no cursor was opened"
    return str(caught.value)


async def test_spawning_at_the_pending_call_is_refused_naming_the_call():
    """The trap the probe hit: ``at`` is the planner's own unanswered call."""
    session, log, pending = await _prefix_session()

    error = await _refused(
        session, log, pending, seed=[NOTE, *seed_tool_call("start_subagent", {}, "task")]
    )

    assert "callP" in error


async def test_an_unpaired_call_in_the_seed_is_refused_naming_it():
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)
    call, _ = seed_tool_call("start_subagent", {}, "task")
    call_id = call["content"][0]["id"]

    error = await _refused(session, log, user_entry, seed=[NOTE, call])
    assert call_id in error

    error = await _refused(session, log, user_entry, seed=[call, NOTE])
    assert call_id in error, "a note between a call and its result breaks the pairing"


async def test_an_orphan_result_in_the_seed_is_refused_naming_it():
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)
    _, answer = seed_tool_call("start_subagent", {}, "task")

    error = await _refused(session, log, user_entry, seed=[answer])

    assert answer["tool_call_id"] in error


async def test_a_seed_reusing_an_id_on_the_path_is_refused():
    session, log, pending = await _prefix_session()
    answer = {
        "role": "toolResult",
        "tool_call_id": "callP",
        "tool_name": "delegate",
        "content": [{"type": "text", "text": "done"}],
    }
    reuse = _reply("", [ToolCall(id="callP", name="delegate", arguments={})]).model_dump()

    error = await _refused(session, log, pending, seed=[answer, reuse, answer])

    assert "callP" in error and "already used" in error


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({}, "give a prompt, a seed, or both"),
        ({"seed": []}, "seed is empty"),
        ({"seed": [{"role": "system", "content": "x"}]}, r"seed\[0\] has role 'system'"),
        ({"seed": ["text"]}, r"seed\[0\] is a str"),
        ({"seed": [{"role": "assistant", "content": []}]}, r"seed\[0\] is not a valid"),
        ({"seed": [_reply("I was saying").model_dump()]}, "continue its own reply"),
    ],
)
async def test_a_malformed_seed_is_refused(kw: dict[str, Any], match: str):
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)

    error = await _refused(session, log, user_entry, **kw)

    assert re.search(match, error), error


async def test_an_unknown_branch_point_is_refused():
    session, log, _ = await _prefix_session()

    error = await _refused(session, log, "nope", seed=[NOTE])

    assert "not found" in error


# --- ids ---------------------------------------------------------------------------


def test_injected_call_ids_fit_every_providers_format_and_do_not_collide():
    """Mistral takes exactly nine of [a-zA-Z0-9]; Anthropic and OpenAI accept that too."""
    ids = {new_tool_call_id() for _ in range(5000)}
    assert len(ids) == 5000
    assert all(re.fullmatch(r"[A-Za-z0-9]{%d}" % TOOL_CALL_ID_LENGTH, i) for i in ids)


def test_seed_tool_call_pairs_a_call_with_its_result():
    call, answer = seed_tool_call("start_subagent", {"x": 1}, "go", is_error=True)
    (block,) = call["content"]
    assert (block["type"], block["name"], block["arguments"]) == (
        "toolCall",
        "start_subagent",
        {"x": 1},
    )
    assert answer["tool_call_id"] == block["id"]
    assert answer["is_error"] is True
    assert call["stop_reason"] == "toolUse"


# --- concurrency, nesting, spending -----------------------------------------------


async def test_seeded_branches_run_concurrently_from_one_tool():
    """Three branches from one execute: each child's request waits for all three to
    arrive, so the test only finishes if they overlap."""
    arrived = asyncio.Event()
    count = {"n": 0}
    results: list[Any] = []

    async def delegate(tool_call_id, params, signal, on_update, ctx):
        parent = _parent_of(ctx, ctx.cursor.leaf)
        results.extend(
            await asyncio.gather(
                *[
                    ctx.spawn_branch(
                        parent,
                        seed=[NOTE, *seed_tool_call("start_subagent", {}, f"task {i}")],
                        tools=[],
                        label=f"child {i}",
                    )
                    for i in range(3)
                ]
            )
        )
        return {"content": [{"type": "text", "text": "all done"}]}

    session, log = _planner_session(delegate)

    async def child_answer() -> str:
        count["n"] += 1
        if count["n"] == 3:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=5)
        return "child done"

    def route(messages: list[dict[str, Any]]) -> Any:
        if _is_child(messages):
            return child_answer()
        if messages[-1].get("role") == "toolResult":
            return "planner done"
        return _reply("", [ToolCall(id="callP", name="delegate", arguments={})])

    provider = _Provider(route)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await session.prompt("plan")

    assert [r.ok for r in results] == [True, True, True], [r.error for r in results]
    assert len({r.cursor_id for r in results}) == 3
    call_ids = {
        b["id"]
        for r in results
        for m in r.messages
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("type") == "toolCall"
    }
    assert len(call_ids) == 3, "concurrent branches never share an injected id"


async def test_a_seeded_branch_can_spawn_a_seeded_branch():
    """Nesting: the child calls ``delegate`` too; its own spawn hangs from the parent
    of the child's pending call, which is the child's seed result."""
    depth_seen: list[int] = []
    results: list[Any] = []

    async def delegate(tool_call_id, params, signal, on_update, ctx):
        depth = int(params.get("depth", 0))
        depth_seen.append(depth)
        result = await ctx.spawn_branch(
            _parent_of(ctx, ctx.cursor.leaf),
            seed=[NOTE, *seed_tool_call("start_subagent", {}, f"depth {depth + 1}")],
            tools=["delegate"] if depth == 0 else [],
        )
        results.append(result)
        return {"content": [{"type": "text", "text": f"ok={result.ok}"}]}

    session, log = _planner_session(delegate)

    def route(messages: list[dict[str, Any]]) -> Any:
        if messages[-1].get("role") == "toolResult" and messages[-1].get("tool_name") == "delegate":
            return "done"
        last = _text(messages[-1])
        if last == "depth 1":
            return _reply("", [ToolCall(id="callC", name="delegate", arguments={"depth": 1})])
        if last == "depth 2":
            return "grandchild done"
        return _reply("", [ToolCall(id="callP", name="delegate", arguments={"depth": 0})])

    provider = _Provider(route)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await session.prompt("plan")

    assert depth_seen == [0, 1]
    inner, outer = results
    assert inner.ok and outer.ok, (inner.error, outer.error)
    grandchild = next(r for r in provider.requests if _text(r[-1]) == "depth 2")
    roles = [m["role"] for m in grandchild]
    assert roles == [
        "system",
        "user",
        "user",
        "assistant",
        "toolResult",
        "user",
        "assistant",
        "toolResult",
    ]
    assert "callC" not in str(grandchild), (
        "the child's pending call is not on the grandchild's path"
    )


async def test_spending_reaches_message_end_with_the_branch_cursor():
    session, log, pending = await _prefix_session()
    user_entry = _parent_of(session._extension_api.context, pending)
    seen: list[Any] = []
    session.subscribe(seen.append)

    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_Provider(lambda m: "ok").stream
    ):
        result = await session.spawn(user_entry, seed=[NOTE], tools=[])

    ends = [
        e
        for e in seen
        if e.type == "message_end" and isinstance(e.message, dict) and e.message.get("usage")
    ]
    assert ends, "the branch's completion reaches message_end"
    assert {e.cursor_id for e in ends} == {result.cursor_id}
    assert ends[-1].message["usage"]["total_tokens"] == 5
    assert {e.source for e in seen} == {"agent"}
