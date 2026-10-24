"""Handler policies (docs/STEP-DEBUGGER.md §4, #83).

Timeouts use short ``Bounded`` policies registered per test, so nothing sleeps
for the built-ins' 30 s or 15 s; the built-ins' error outcomes run as registered.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.entry_annotations import AnnotationError
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.extensions.runner import ExtensionHandlers
from tau_agent_core.policies import (
    BUILTIN_POLICIES,
    Bounded,
    DeadlineExpired,
    Failed,
    HandlerPolicyError,
    PausableDeadline,
    PolicySite,
    Proceeded,
    Returned,
    Unbounded,
    enclosing_deadlines,
    pause_enclosing_deadlines,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.suspension import (
    TurnSuspended,
    current_dispatch_mark,
    mark_suspension_raised,
)
from tau_agent_core.tools.base import AgentTool

_TS = 1_700_000_000_000
FAST = 0.05
PRODUCER_CHANNEL = "ext:test_handler_policies._producer:hit"


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


def _assistant(content: list[Any], stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(),
    )


def _text(text: str) -> AssistantMessage:
    return _assistant([TextContent(text=text)])


def _calls(*names: str) -> AssistantMessage:
    return _assistant(
        [ToolCall(id=f"c-{n}", name=n, arguments={}) for n in names], stop_reason="toolUse"
    )


def _script(*finals: AssistantMessage):
    queue = list(finals)

    async def fake(model, context, options=None):
        return _Stream([DoneEvent(final=queue.pop(0), usage=Usage())])

    return fake


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


def _session(*extensions: Any, **kwargs: Any) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions), **kwargs
    )


def _tool(name: str, ran: list[str]) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        ran.append(name)
        return f"{name} ok"

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _messages(session: AgentSession) -> list[dict[str, Any]]:
    return [e for e in session.session_log.entries() if e.get("type") == "message"]


def _agent_ends(into: list[Any]):
    def factory(api: Any) -> None:
        api.on("agent_end", lambda event: into.append(event))

    return factory


def _site() -> PolicySite:
    return PolicySite("tool_call", "ext", lambda text: None)


# ── the policies on their own ───────────────────────────────────────────────


def test_the_built_ins_carry_the_section_4_2_numbers() -> None:
    assert BUILTIN_POLICIES["gating"] == Bounded(timeout_s=30, on_timeout="fail", on_error="fail")
    assert BUILTIN_POLICIES["advisory"] == Bounded(
        timeout_s=15, on_timeout="proceed", on_error="proceed"
    )
    assert BUILTIN_POLICIES["human"] == Unbounded(on_error="fail")
    assert {"gating", "advisory", "human"} <= set(_session()._registry.policy_names())


@pytest.mark.parametrize(
    ("policy", "kind"),
    [
        (Bounded(FAST, "fail", "fail"), Failed),
        (Bounded(FAST, "proceed", "proceed"), Proceeded),
    ],
)
async def test_bounded_turns_a_timeout_and_an_error_into_its_modes(policy: Any, kind: Any) -> None:
    async def slow() -> None:
        await asyncio.sleep(10)

    async def boom() -> None:
        raise RuntimeError("boom")

    started = time.monotonic()
    timed_out = await policy.run(slow, _site())
    assert time.monotonic() - started < 1
    assert isinstance(timed_out, kind)
    errored = await policy.run(boom, _site())
    assert isinstance(errored, kind)
    if kind is Failed:
        assert isinstance(timed_out.error, DeadlineExpired)
        assert isinstance(errored.error, RuntimeError)
    else:
        assert timed_out.record.startswith("DeadlineExpired: timed out after 0.05s")
        assert errored.record == "RuntimeError: boom"


async def test_bounded_returns_the_value_and_unbounded_fails_on_error() -> None:
    async def fine() -> int:
        return 7

    async def boom() -> None:
        raise ValueError("no")

    assert await Bounded(1, "fail", "fail").run(fine, _site()) == Returned(7)
    failed = await Unbounded().run(boom, _site())
    assert isinstance(failed, Failed) and isinstance(failed.error, ValueError)


def test_bad_policy_arguments_are_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        Bounded(0, "fail", "fail")
    with pytest.raises(ValueError):
        Bounded(1, "retry", "fail")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Unbounded(on_error="shrug")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Proceeded("  ")


# ── pausable deadlines ───────────────────────────────────────────────────────


async def test_a_paused_deadline_does_not_count_the_pause() -> None:
    deadline = PausableDeadline(0.1)

    async def call() -> str:
        deadline.pause()
        deadline.pause()
        await asyncio.sleep(0.15)
        deadline.resume()
        assert deadline.paused
        await asyncio.sleep(0.05)
        deadline.resume()
        await asyncio.sleep(0.02)
        return "done"

    assert await deadline.run(call) == "done"
    assert not deadline.expired


async def test_a_deadline_resumed_already_past_its_time_fires_at_once() -> None:
    deadline = PausableDeadline(0.02)
    reached: list[str] = []

    async def call() -> None:
        time.sleep(0.05)
        deadline.pause()
        assert deadline.remaining() <= 0
        deadline.resume()
        reached.append("resumed")
        await asyncio.sleep(0)
        reached.append("after")

    with pytest.raises(DeadlineExpired):
        await deadline.run(call)
    assert reached == ["resumed"]
    assert deadline.expired


def test_resume_without_pause_is_refused() -> None:
    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="without a matching pause"):
            PausableDeadline(1).resume()

    asyncio.run(scenario())


async def test_pause_enclosing_deadlines_pauses_the_whole_stack() -> None:
    outer = PausableDeadline(0.05)
    inner = PausableDeadline(0.05)
    seen: list[tuple[PausableDeadline, ...]] = []

    async def inner_call() -> str:
        with pause_enclosing_deadlines() as paused:
            seen.append(paused)
            assert all(d.paused for d in paused)
            await asyncio.sleep(0.12)
        return "inner"

    async def outer_call() -> str:
        return str(await inner.run(inner_call))

    assert enclosing_deadlines() == ()
    assert await outer.run(outer_call) == "inner"
    assert seen == [(outer, inner)]
    assert not outer.expired and not inner.expired


async def test_an_inner_deadline_is_capped_by_the_outer_one() -> None:
    outer = PausableDeadline(FAST)
    inner_saw: list[str] = []
    caps: list[float] = []

    async def inner_call() -> None:
        await asyncio.sleep(10)

    async def outer_call() -> None:
        inner = PausableDeadline(10)
        caps.append(inner.remaining())
        try:
            await inner.run(inner_call)
        except asyncio.CancelledError:
            inner_saw.append("cancelled")
            raise
        except DeadlineExpired:
            inner_saw.append("own timeout")

    started = time.monotonic()
    with pytest.raises(DeadlineExpired) as raised:
        await outer.run(outer_call)
    assert time.monotonic() - started < 1
    assert raised.value.deadline is outer
    assert inner_saw == ["cancelled"]
    assert caps[0] <= FAST


async def test_turn_suspended_crosses_the_policed_task_boundary() -> None:
    async def suspends() -> None:
        raise TurnSuspended()

    with pytest.raises(TurnSuspended):
        await PausableDeadline(1).run(suspends)
    with pytest.raises(TurnSuspended):
        await Bounded(1, "proceed", "proceed").run(suspends, _site())
    with pytest.raises(TurnSuspended):
        await Unbounded(on_error="proceed").run(suspends, _site())


# ── registration ─────────────────────────────────────────────────────────────


def _api() -> ExtensionAPI:
    return ExtensionAPI(hook_handlers=ExtensionHandlers(path="probe.py"))


def test_an_unknown_policy_is_refused_at_registration() -> None:
    api = _api()
    with pytest.raises(ValueError, match="no handler policy named 'nope'"):
        api.on("tool_call", lambda e, ctx: None, policy="nope")
    with pytest.raises(ValueError, match="no handler policy named 'nope'"):
        api.on("ext:other:topic", lambda payload: None, policy="nope")

    def factory(api: Any) -> None:
        api.on("turn_end", lambda e, ctx: None, policy="nope")

    with pytest.raises(ValueError, match="nope"):
        _session(factory)


def test_a_policy_on_a_notify_event_and_a_duplicate_name_are_refused() -> None:
    api = _api()
    with pytest.raises(ValueError, match="notify event"):
        api.on("message_end", lambda event: None, policy="gating")
    api.register_policy("mine", Bounded(1, "fail", "fail"))
    with pytest.raises(ValueError, match="already registered"):
        api.register_policy("mine", Bounded(1, "fail", "fail"))
    with pytest.raises(ValueError, match="already registered"):
        api.register_policy("gating", Bounded(1, "fail", "fail"))
    with pytest.raises(TypeError):
        api.register_policy("broken", object())  # type: ignore[arg-type]


def test_a_handler_with_no_policy_is_stored_unwrapped_and_unsubscribes() -> None:
    api = _api()
    bucket = api._hook_handlers
    assert bucket is not None

    def handler(e: Any, ctx: Any) -> None:
        return None

    off = api.on("tool_call", handler)
    assert bucket.handlers["tool_call"] == [handler]
    off()
    off_policed = api.on("tool_call", handler, policy="gating")
    assert bucket.handlers["tool_call"][0].handler is handler
    off_policed()
    assert bucket.handlers["tool_call"] == []


async def test_no_policy_keeps_todays_isolation() -> None:
    def isolated(api: Any) -> None:
        def on_turn_end(event: Any, ctx: Any) -> None:
            raise RuntimeError("observer broke")

        async def on_hit(payload: Any) -> None:
            raise RuntimeError("subscriber broke")

        api.on("turn_end", on_turn_end)
        api.on(PRODUCER_CHANNEL, on_hit)

    ran: list[str] = []
    session = _session(_producer, isolated, tools=[_tool("probe", ran)])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("probe"), _text("ok"))
    ):
        await session.prompt("go")
    assert ran == ["probe"]
    assert _producer_saw == []
    assert session.extension_warnings() == []


# ── outcomes at each site ────────────────────────────────────────────────────

_producer_saw: list[str] = []


def _producer(api: Any) -> None:
    """Emits on its ``hit`` channel from ``tool_call``, recording what ``emit`` raised."""

    async def on_call(event: dict[str, Any], ctx: Any) -> None:
        try:
            await api.emit("hit", {"tool": event["tool_name"]})
        except HandlerPolicyError as err:
            _producer_saw.append(err.detail)
            raise

    api.on("tool_call", on_call)


@pytest.fixture(autouse=True)
def _reset_producer() -> None:
    _producer_saw.clear()


def _policed(site: str, policy: str, cause: str):
    def factory(api: Any) -> None:
        if policy.endswith("_fast"):
            mode = "fail" if policy.startswith("gating") else "proceed"
            api.register_policy(policy, Bounded(FAST, mode, mode))

        async def handler(*args: Any) -> None:
            if cause == "timeout":
                await asyncio.sleep(10)
            raise RuntimeError("boom")

        api.on(PRODUCER_CHANNEL if site == "ext" else site, handler, policy=policy)

    return factory


CASES = [
    ("gating", "error", "fail"),
    ("advisory", "error", "proceed"),
    ("human", "error", "fail"),
    ("gating_fast", "timeout", "fail"),
    ("advisory_fast", "timeout", "proceed"),
]


def _detail(cause: str) -> str:
    return "DeadlineExpired: timed out after 0.05s" if cause == "timeout" else "RuntimeError: boom"


def _ext_name(session: AgentSession, index: int) -> str:
    return session._extension_runner._extensions[index].name


@pytest.mark.parametrize(("policy", "cause", "mode"), CASES)
async def test_tool_call(policy: str, cause: str, mode: str) -> None:
    ran: list[str] = []
    session = _session(_policed("tool_call", policy, cause), tools=[_tool("probe", ran)])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("probe"), _text("ok"))
    ):
        await session.prompt("go")
    result = _messages(session)[2]["message"]
    if mode == "fail":
        assert ran == []
        assert result["is_error"] is True
        assert "Extension failed, blocking execution" in result["content"][0]["text"]
        assert _detail(cause) in result["content"][0]["text"]
        assert session.extension_warnings() == []
    else:
        assert ran == ["probe"]
        assert result["is_error"] is False
        [warning] = session.extension_warnings()
        assert (warning.source, warning.key) == (_ext_name(session, 0), "policy:tool_call")
        assert warning.message == f"tool_call handler proceeded: {_detail(cause)}"


@pytest.mark.parametrize(("policy", "cause", "mode"), CASES)
async def test_annotate(policy: str, cause: str, mode: str) -> None:
    ends: list[Any] = []
    session = _session(_policed("annotate", policy, cause), _agent_ends(ends))
    name = _ext_name(session, 0)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        if mode == "fail":
            with pytest.raises(AnnotationError):
                await session.prompt("go")
        else:
            await session.prompt("go")
    user = _messages(session)[0]
    if mode == "fail":
        assert user["ext"][name] == {"error": _detail(cause)}
        assert [e.end_reason for e in ends] == ["error"]
    else:
        assert user["ext"][name] == {"error": _detail(cause), "proceeded": True}
        assert [e.end_reason for e in ends] == ["done"]
        assert session.extension_warnings() == []


@pytest.mark.parametrize("site", ["turn_end", "entry_written"])
@pytest.mark.parametrize(("policy", "cause", "mode"), CASES)
async def test_turn_end_and_entry_written(site: str, policy: str, cause: str, mode: str) -> None:
    ends: list[Any] = []
    session = _session(_policed(site, policy, cause), _agent_ends(ends))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        if mode == "fail":
            with pytest.raises(HandlerPolicyError) as raised:
                await session.prompt("go")
        else:
            await session.prompt("go")
    if mode == "fail":
        assert raised.value.event == site
        assert raised.value.detail == _detail(cause)
        assert [e.end_reason for e in ends] == ["error"]
    else:
        assert [e.end_reason for e in ends] == ["done"]
        [warning] = session.extension_warnings()
        assert warning.key == f"policy:{site}"


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize(("policy", "cause", "mode"), CASES)
async def test_tool_result(policy: str, cause: str, mode: str, parallel: bool) -> None:
    ran: list[str] = []
    ends: list[Any] = []
    tools = [_tool("one", ran), _tool("two", ran)]
    session = _session(
        _policed("tool_result", policy, cause),
        _agent_ends(ends),
        tools=tools,
        tool_execution_mode="parallel" if parallel else "sequential",
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_calls("one", "two"), _text("ok")),
    ):
        if mode == "fail":
            with pytest.raises(HandlerPolicyError):
                await session.prompt("go")
        else:
            await session.prompt("go")
    results = [m["message"] for m in _messages(session) if m["message"]["role"] == "toolResult"]
    assert sorted(r["tool_call_id"] for r in results) == ["c-one", "c-two"]
    if mode == "fail":
        assert [e.end_reason for e in ends] == ["error"]
        assert results[0]["is_error"] is True
        if parallel:
            assert ran == ["one", "two"]
        else:
            assert ran == ["one"]
            assert results[1]["content"][0]["text"] == ABORTED_TOOL_RESULT
    else:
        assert [e.end_reason for e in ends] == ["done"]
        assert all(r["is_error"] is False for r in results)
        [warning] = session.extension_warnings()
        assert warning.key == "policy:tool_result"


@pytest.mark.parametrize(("policy", "cause", "mode"), CASES)
async def test_ext_channel(policy: str, cause: str, mode: str) -> None:
    ran: list[str] = []
    session = _session(_producer, _policed("ext", policy, cause), tools=[_tool("probe", ran)])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("probe"), _text("ok"))
    ):
        await session.prompt("go")
    if mode == "fail":
        assert _producer_saw == [_detail(cause)]
        assert ran == []
    else:
        assert _producer_saw == []
        assert ran == ["probe"]
        [warning] = session.extension_warnings()
        assert (warning.source, warning.key) == (
            _ext_name(session, 1),
            f"policy:{PRODUCER_CHANNEL}",
        )


def _nest_outer(api: Any) -> None:
    api.register_policy("outer_fast", Bounded(FAST, "fail", "fail"))

    async def on_call(event: dict[str, Any], ctx: Any) -> None:
        await api.emit("hit", {})

    api.on("tool_call", on_call, policy="outer_fast")


def _nest_inner(api: Any) -> None:
    api.register_policy("inner_slow", Bounded(10, "proceed", "proceed"))

    async def on_hit(payload: Any) -> None:
        await asyncio.sleep(10)

    api.on("ext:test_handler_policies._nest_outer:hit", on_hit, policy="inner_slow")


async def test_a_nested_timeout_is_the_outer_policys() -> None:
    ran: list[str] = []
    session = _session(_nest_outer, _nest_inner, tools=[_tool("probe", ran)])
    started = time.monotonic()
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_calls("probe"), _text("ok"))
    ):
        await session.prompt("go")
    assert time.monotonic() - started < 2
    assert ran == []
    assert "timed out after 0.05s" in _messages(session)[2]["message"]["content"][0]["text"]
    assert session.extension_warnings() == []


# ── the dispatch mark and the suspension unwind ──────────────────────────────


async def test_the_dispatch_mark_closes_once_the_policed_task_returns() -> None:
    marks: list[Any] = []
    finished: list[str] = []

    def recorder(api: Any) -> None:
        api.register_policy("fast", Bounded(FAST, "proceed", "proceed"))

        async def quick(event: Any, ctx: Any) -> None:
            mark = current_dispatch_mark()
            assert mark is not None and not mark.closed
            marks.append(mark)

        async def slow(event: Any, ctx: Any) -> None:
            marks.append(current_dispatch_mark())
            try:
                await asyncio.sleep(10)
            finally:
                finished.append("slow")

        api.on("turn_end", quick, policy="fast")
        api.on("user_turn_end", slow, policy="fast")

    session = _session(recorder)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")
    assert len(marks) == 2
    assert all(m is not None and m.closed for m in marks)
    assert finished == ["slow"]
    assert current_dispatch_mark() is None


async def test_turn_suspended_reaches_the_loop_through_a_policed_handler() -> None:
    ends: list[Any] = []

    def suspender(api: Any) -> None:
        async def on_turn_end(event: Any, ctx: Any) -> None:
            mark_suspension_raised("tau-debugger")
            raise TurnSuspended()

        api.on("turn_end", on_turn_end, policy="advisory")

    session = _session(suspender, _agent_ends(ends))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(TurnSuspended):
            await session.prompt("go")
    assert [e.end_reason for e in ends] == ["suspended"]
    assert session.extension_warnings() == []


async def test_a_policy_that_swallows_the_unwind_is_caught() -> None:
    from tau_agent_core.suspension import SuspensionSwallowedError

    class Swallowing:
        async def run(self, call: Any, site: PolicySite) -> Any:
            try:
                await call()
            except TurnSuspended:
                return Returned(None)
            return Returned(None)

    def suspender(api: Any) -> None:
        api.register_policy("swallowing", Swallowing())

        async def on_turn_end(event: Any, ctx: Any) -> None:
            mark_suspension_raised("tau-debugger")
            raise TurnSuspended()

        api.on("turn_end", on_turn_end, policy="swallowing")

    session = _session(suspender)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(SuspensionSwallowedError):
            await session.prompt("go")


# ── review round 2 ───────────────────────────────────────────────────────────


async def test_a_bounded_handler_calling_prompt_hits_the_reentrant_guard_at_once() -> None:
    ends: list[Any] = []

    def reentrant(api: Any) -> None:
        api.register_policy("slow_gate", Bounded(5, "fail", "fail"))

        async def on_turn_end(event: Any, ctx: Any) -> None:
            await ctx.prompt("again")

        api.on("turn_end", on_turn_end, policy="slow_gate")

    session = _session(reentrant, _agent_ends(ends))
    started = time.monotonic()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        with pytest.raises(HandlerPolicyError) as raised:
            await session.prompt("go")
    assert time.monotonic() - started < 2
    assert "reentrant self-submission" in raised.value.detail
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert [e.end_reason for e in ends] == ["error"]


async def test_a_task_a_handler_spawns_is_not_the_turn() -> None:
    from tau_agent_core.policies import runs_on_behalf_of

    owner = asyncio.current_task()
    assert owner is not None
    seen: dict[str, bool] = {}

    async def spawned() -> None:
        seen["spawned"] = runs_on_behalf_of(owner)

    async def call() -> None:
        seen["policed"] = runs_on_behalf_of(owner)
        await asyncio.create_task(spawned())

    await PausableDeadline(1).run(call)
    assert seen == {"policed": True, "spawned": False}


def _shutdown_failer(api: Any) -> None:
    def on_shutdown(event: Any, ctx: Any) -> None:
        raise RuntimeError("teardown broke")

    api.on("session_shutdown", on_shutdown, policy="human")


_shut_down: list[str] = []


def _shutdown_peer(api: Any) -> None:
    api.on("session_shutdown", lambda event, ctx: _shut_down.append("peer"))


async def test_a_failed_lifecycle_handler_does_not_stop_its_peers() -> None:
    _shut_down.clear()
    errors: list[Any] = []
    session = _session(_shutdown_failer, _shutdown_peer)
    session._extension_runner.on_error(errors.append)
    await session._extension_runner.emit_session_shutdown({"type": "session_shutdown"})
    assert _shut_down == ["peer"]
    assert [e.event for e in errors] == ["session_shutdown"]
    [warning] = session.extension_warnings()
    assert (warning.source, warning.key) == (_ext_name(session, 0), "policy:session_shutdown")
    assert warning.message == "session_shutdown handler failed: RuntimeError: teardown broke"
