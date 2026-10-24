"""``api.suspend``: the in-process primitive (docs/STEP-DEBUGGER.md §3.1-§3.3, M1e).

Every suspension here is answered by a subscriber to ``suspension_open``, the
way a head answers one, or by the turn's abort.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.cursor import Cursor
from tau_agent_core.events import AgentEvent
from tau_agent_core.policies import Bounded
from tau_agent_core.session_log import InMemorySessionLog, is_incomplete
from tau_agent_core.submission import Submission
from tau_agent_core.suspensions import (
    NoResponderError,
    SuspensionBroker,
    SuspensionRefused,
    validate_suspension_spec,
)
from tau_agent_core.tools.base import AgentTool

_TS = 1_700_000_000_000
FAST = 0.05

SPEC: dict[str, Any] = {
    "title": "Break",
    "text": "look",
    "evidence": [{"label": "why", "text": "because"}],
    "fields": [{"name": "note", "kind": "text"}],
    "actions": [{"label": "step"}, {"label": "abort"}],
}


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


def _session(*extensions: Any, responder: bool = True, **kwargs: Any) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions), **kwargs
    )
    if responder:
        session.add_suspension_responder("test")
    return session


def _answering(
    session: AgentSession,
    answers: list[tuple[str, dict[str, Any]]],
    seen: list[AgentEvent] | None = None,
) -> None:
    """Answer each opened (not queued) suspension with the next ``(action, values)``."""
    queue = list(answers)

    def on_event(event: AgentEvent) -> None:
        if seen is not None:
            seen.append(event)
        if event.type != "suspension_open" or event.suspension is None:
            return
        if event.suspension["queued"]:
            return
        action, values = queue.pop(0)
        suspension_id = event.suspension["id"]
        asyncio.get_running_loop().call_soon(
            lambda: session.answer_suspension(suspension_id, action, values, {"client": "test"})
        )

    session.subscribe(on_event)


def _suspensions(session: AgentSession) -> list[dict[str, Any]]:
    return [e for e in session.session_log.entries() if e.get("type") == "suspension"]


def _messages(session: AgentSession) -> list[dict[str, Any]]:
    return [e["message"] for e in session.session_log.entries() if e.get("type") == "message"]


def _suspender(site: str, results: list[Any], *, policy: str | None = None) -> Callable:
    def factory(api: Any) -> None:
        async def handler(event: Any, ctx: Any) -> None:
            try:
                results.append(await api.suspend(SPEC))
            except Exception as err:
                results.append(err)
                raise

        api.on(site, handler, policy=policy)

    return factory


# ── the spec ────────────────────────────────────────────────────────────────


def test_the_spec_is_the_ask_spec_plus_evidence_and_target() -> None:
    spec = validate_suspension_spec({**SPEC, "targetId": "e1"})
    assert spec["evidence"] == [{"label": "why", "text": "because"}]
    assert spec["targetId"] == "e1"
    assert [a["label"] for a in spec["actions"]] == ["step", "abort"]
    with pytest.raises(ValueError, match="evidence"):
        validate_suspension_spec({**SPEC, "evidence": [{"label": "x"}]})
    with pytest.raises(ValueError, match="actions"):
        validate_suspension_spec({"title": "t"})


# ── the lifecycle ────────────────────────────────────────────────────────────


async def test_an_answered_suspension_returns_its_resolution_and_finalizes_its_side_entry() -> None:
    results: list[Any] = []
    seen: list[AgentEvent] = []
    session = _session(_suspender("entry_written", results))
    _answering(session, [("step", {"note": "fine"})] * 2, seen)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert [r["action"] for r in results] == ["step", "step"]
    assert results[0]["values"] == {"note": "fine"}
    assert results[0]["answered_by"] == {"client": "test"}
    entries = _suspensions(session)
    assert len(entries) == 2
    user_entry = next(e for e in session.session_log.entries() if e.get("type") == "message")
    first = entries[0]
    assert first["parentId"] == user_entry["id"]
    assert not is_incomplete(first)
    assert first["site"] == "entry_written"
    assert first["entryId"] == user_entry["id"]
    assert first["resolution"]["id"] == first["id"] == results[0]["id"]
    assert first["reason"] == "answered"
    assert first["continuation"]["phase"] == "dispatch:entry_written"
    kinds = [(e.type, e.reason) for e in seen if e.type.startswith("suspension_")]
    assert kinds == [("suspension_open", None), ("suspension_closed", "answered")] * 2
    assert session.cursor.leaf != first["id"]


async def test_a_second_answer_converges_on_already_resolved() -> None:
    results: list[Any] = []
    replies: list[dict[str, Any]] = []
    session = _session(_suspender("turn_end", results))

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]

            def both() -> None:
                replies.append(session.answer_suspension(sid, "step", {"note": "a"}))
                replies.append(session.answer_suspension(sid, "step", {"note": "b"}))

            asyncio.get_running_loop().call_soon(both)

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert replies[0]["accepted"] is True
    assert replies[1] == {
        "accepted": False,
        "reason": "already resolved",
        "resolution": replies[0]["resolution"],
    }
    assert results[0]["values"] == {"note": "a"}


async def test_answers_are_checked_before_anything_is_recorded() -> None:
    results: list[Any] = []
    errors: list[str] = []
    session = _session(_suspender("turn_end", results))

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]

            def answer() -> None:
                for action, values in (("jump", {"note": ""}), ("step", {}), ("step", {"x": 1})):
                    try:
                        session.answer_suspension(sid, action, values)
                    except ValueError as err:
                        errors.append(str(err))
                session.answer_suspension(sid, "step", {"note": "ok"})

            asyncio.get_running_loop().call_soon(answer)

    session.subscribe(on_event)
    with pytest.raises(ValueError, match="no suspension"):
        session.answer_suspension("nope", "step")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert len(errors) == 3
    assert "not one of its actions" in errors[0]
    assert results[0]["values"] == {"note": "ok"}


_ctx_results: list[Any] = []


def _ctx_suspender(api: Any) -> None:
    async def handler(event: Any, ctx: Any) -> None:
        _ctx_results.append(await ctx.suspend(SPEC))

    api.on("turn_end", handler)


async def test_ctx_suspend_raises_under_the_running_handlers_extension() -> None:
    _ctx_results.clear()
    session = _session(_ctx_suspender)
    _answering(session, [("step", {"note": ""})])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    [entry] = _suspensions(session)
    assert entry["extension"] == "test_suspend._ctx_suspender"
    assert _ctx_results[0]["action"] == "step"


# ── where it may be called (§3.2) ────────────────────────────────────────────


@pytest.mark.parametrize("site", ["input", "tool_result", "annotate", "before_agent_start"])
async def test_a_hook_before_its_entry_is_written_is_not_a_site(site: str) -> None:
    results: list[Any] = []
    here: list[bool] = []

    def factory(api: Any) -> None:
        async def handler(event: Any, ctx: Any) -> None:
            here.append(api.can_suspend_here())
            try:
                await api.suspend(SPEC)
            except SuspensionRefused as err:
                results.append(err)

        api.on(site, handler)

    ran: list[str] = []
    session = _session(factory, tools=[_tool("one", ran)])
    script = _script(_calls("one"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert results and all(isinstance(r, SuspensionRefused) for r in results)
    assert f"{site!r} is not a suspension site" in str(results[0])
    assert here and not any(here)
    assert _suspensions(session) == []


async def test_a_notify_event_handler_inherits_no_permission() -> None:
    results: list[Any] = []

    def factory(api: Any) -> None:
        async def on_end(event: Any) -> None:
            if event.message and event.message.get("role") == "assistant":
                try:
                    await api.suspend(SPEC)
                except SuspensionRefused as err:
                    results.append(err)

        async def on_turn_end(event: Any, ctx: Any) -> None:
            await session_ref[0]._events.emit(
                AgentEvent(type="message_end", timestamp=1, message={"role": "assistant"})
            )

        api.on("message_end", on_end)
        api.on("turn_end", on_turn_end)

    session_ref: list[AgentSession] = []
    session = _session(factory)
    session_ref.append(session)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert results and "no hook dispatch is open" in str(results[0])


async def test_a_task_a_hook_spawned_is_refused_during_and_after_the_hook() -> None:
    results: list[Any] = []
    spawned: list[asyncio.Task[Any]] = []

    def factory(api: Any) -> None:
        async def later() -> None:
            try:
                await api.suspend(SPEC)
            except SuspensionRefused as err:
                results.append(err)

        async def handler(event: Any, ctx: Any) -> None:
            spawned.append(asyncio.create_task(later()))
            await asyncio.sleep(0)

        api.on("turn_end", handler)

    session = _session(factory)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    await asyncio.gather(*spawned)
    assert len(results) == 1
    assert "spawned" in str(results[0]) or "has returned" in str(results[0])


_hits: list[Any] = []


def _producer(api: Any) -> None:
    async def on_turn_end(event: Any, ctx: Any) -> None:
        await api.emit("hit", {"why": "x"})

    api.on("turn_end", on_turn_end)


def _debugger(api: Any) -> None:
    async def on_hit(payload: Any) -> None:
        _hits.append(await api.suspend(SPEC))

    api.on("ext:test_suspend._producer:hit", on_hit, policy="human")


async def test_an_ext_channel_handler_reached_from_a_site_may_suspend() -> None:
    _hits.clear()
    session = _session(_producer, _debugger)
    _answering(session, [("step", {"note": ""})])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    [entry] = _suspensions(session)
    assert entry["extension"] == "test_suspend._debugger"
    assert entry["site"] == "turn_end"
    assert _hits[0]["action"] == "step"


async def test_a_tool_call_suspension_records_the_call_and_its_effective_args() -> None:
    ran: list[str] = []

    def patcher(api: Any) -> None:
        def on_call(event: Any, ctx: Any) -> None:
            event["input"]["patched"] = True

        api.on("tool_call", on_call)

    results: list[Any] = []
    session = _session(patcher, _suspender("tool_call", results), tools=[_tool("one", ran)])
    _answering(session, [("step", {"note": ""})])
    script = _script(_calls("one"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    [entry] = _suspensions(session)
    assert entry["call"] == {"id": "c-one", "effective_args": {"patched": True}}
    assert (entry["continuation"]["phase"], entry["continuation"]["handler"]) == (
        "dispatch:tool_call",
        1,
    )
    assert ran == ["one"]


async def test_a_nested_tool_call_and_a_tool_body_refuse_cleanly() -> None:
    """Code mode's nested calls (#94 bypasses hooks today) and a tool body are never sites."""
    refused: list[str] = []
    ran: list[str] = []

    def factory(api: Any) -> None:
        async def on_call(event: Any, ctx: Any) -> dict[str, Any] | None:
            if event.get("parent_tool_call_id") is None:
                return None
            assert not api.can_suspend_here()
            try:
                await api.suspend(SPEC)
            except SuspensionRefused as err:
                refused.append(str(err))
            return None

        async def outer(
            tool_call_id: str, params: Any, signal: Any, on_update: Any, ctx: Any
        ) -> str:
            assert not api.can_suspend_here()
            try:
                await api.suspend(SPEC)
            except SuspensionRefused as err:
                refused.append(str(err))
            await api.context.execute_tool("one", {})
            return "outer ok"

        api.on("tool_call", on_call)
        api.register_tool(
            {
                "name": "outer",
                "label": "outer",
                "description": "outer",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": outer,
            }
        )

    session = _session(factory, tools=[_tool("one", ran)])
    script = _script(_calls("outer"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert len(refused) == 2
    assert "no hook dispatch is open" in refused[0]
    assert "nested tool call" in refused[1]
    assert ran == ["one"]
    assert _suspensions(session) == []


# ── responders and unsteppable turns (§3.8, §3.9) ────────────────────────────


async def test_with_no_live_responder_it_raises_no_responder() -> None:
    results: list[Any] = []
    session = _session(_suspender("turn_end", results), responder=False)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert isinstance(results[0], NoResponderError)
    assert _suspensions(session) == []


async def test_a_submission_without_allow_user_input_has_no_live_responder() -> None:
    results: list[Any] = []
    can: list[bool] = []

    def probe(api: Any) -> None:
        api.on("input", lambda event, ctx: can.append(api.can_suspend()))

    session = _session(probe, _suspender("turn_end", results))
    sub = Submission(text="go", source="extension", submitter="cron", submission_id="s1")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.submit(sub), timeout=2)
    assert isinstance(results[0], NoResponderError)
    assert "allow_user_input" in str(results[0])
    assert can == [False]


async def test_store_history_off_is_unsteppable() -> None:
    results: list[Any] = []
    session = _session(_suspender("turn_end", results))
    sub = Submission(
        text="go",
        source="interactive",
        submitter="human",
        submission_id="s1",
        allow_user_input=True,
        store_history=False,
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.submit(sub), timeout=2)
    assert type(results[0]) is SuspensionRefused
    assert "store_history" in str(results[0])


async def test_a_refusal_at_tool_call_blocks_the_call() -> None:
    results: list[Any] = []
    ran: list[str] = []
    session = _session(_suspender("tool_call", results), responder=False, tools=[_tool("one", ran)])
    script = _script(_calls("one"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert ran == []
    result = next(m for m in _messages(session) if m["role"] == "toolResult")
    assert "NoResponderError" in result["content"][0]["text"] or "responder" in str(
        result["content"][0]["text"]
    )


# ── abort (§3.2.1, §3.6) ─────────────────────────────────────────────────────


async def test_an_abort_answer_at_tool_call_aborts_the_turn_and_runs_no_tool() -> None:
    results: list[Any] = []
    ran: list[str] = []
    ends: list[AgentEvent] = []
    session = _session(
        _suspender("tool_call", results), tools=[_tool("one", ran), _tool("two", ran)]
    )
    session.subscribe(lambda e: ends.append(e) if e.type == "agent_end" else None)
    _answering(session, [("abort", {"note": ""})])
    script = _script(_calls("one", "two"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert results[0]["action"] == "abort"
    assert ran == []
    texts = [m["content"][0]["text"] for m in _messages(session) if m["role"] == "toolResult"]
    assert texts == [ABORTED_TOOL_RESULT, ABORTED_TOOL_RESULT]
    assert [e.end_reason for e in ends] == ["aborted"]


async def test_the_turns_abort_resolves_the_open_suspension_as_abort() -> None:
    results: list[Any] = []
    seen: list[AgentEvent] = []
    session = _session(_suspender("turn_end", results))

    def on_event(event: AgentEvent) -> None:
        seen.append(event)
        if event.type == "suspension_open":
            asyncio.get_running_loop().call_soon(session.abort)

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert results[0]["action"] == "abort"
    assert results[0]["answered_by"] is None
    [closed] = [e for e in seen if e.type == "suspension_closed"]
    assert closed.reason == "aborted"
    [entry] = _suspensions(session)
    assert entry["reason"] == "aborted"


async def test_session_shutdown_closes_an_open_suspension_as_cursor_closed() -> None:
    results: list[Any] = []
    seen: list[AgentEvent] = []
    session = _session(_suspender("turn_end", results))
    opened = asyncio.Event()

    def on_event(event: AgentEvent) -> None:
        seen.append(event)
        if event.type == "suspension_open":
            opened.set()

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        turn = asyncio.create_task(session.prompt("go"))
        await asyncio.wait_for(opened.wait(), timeout=2)
        await asyncio.wait_for(session.emit_session_shutdown(), timeout=2)
        await asyncio.wait_for(turn, timeout=2)
    assert results[0]["action"] == "abort"
    [closed] = [e for e in seen if e.type == "suspension_closed"]
    assert closed.reason == "cursor_closed"


# ── deadlines pause (§4.3) ───────────────────────────────────────────────────


def _gated_producer(api: Any) -> None:
    api.register_policy("fast_gate", Bounded(FAST, "fail", "fail"))

    async def on_call(event: Any, ctx: Any) -> None:
        await api.emit("hit", {})
        await asyncio.sleep(FAST / 5)

    api.on("tool_call", on_call, policy="fast_gate")


def _gated_debugger(api: Any) -> None:
    async def on_hit(payload: Any) -> None:
        _hits.append(await api.suspend(SPEC))

    api.on("ext:test_suspend._gated_producer:hit", on_hit, policy="human")


async def test_an_enclosing_gating_deadline_pauses_while_the_human_reads() -> None:
    _hits.clear()
    ran: list[str] = []
    session = _session(_gated_producer, _gated_debugger, tools=[_tool("one", ran)])

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]
            asyncio.get_running_loop().call_later(
                FAST * 4, lambda: session.answer_suspension(sid, "step", {"note": ""})
            )

    session.subscribe(on_event)
    started = time.monotonic()
    script = _script(_calls("one"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert time.monotonic() - started >= FAST * 4
    assert _hits[0]["action"] == "step"
    assert ran == ["one"]


# ── write groups (tau-search's review note) ──────────────────────────────────


@pytest.mark.parametrize("action", ["step", "abort"])
async def test_a_mid_group_suspension_still_dispatches_the_groups_later_entries(
    action: str,
) -> None:
    written: list[str] = []

    def injector(api: Any) -> None:
        def before(event: Any, ctx: Any) -> dict[str, Any]:
            return {"message": {"customType": "note", "content": "after the user"}}

        api.on("before_agent_start", before)

    def stepper(api: Any) -> None:
        async def on_written(event: Any, ctx: Any) -> None:
            written.append(event["kind"])
            if len(written) == 1:
                await api.suspend(SPEC)

        api.on("entry_written", on_written)

    session = _session(injector, stepper)
    _answering(session, [(action, {"note": ""})])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert written[:2] == ["user", "custom"]


# ── the broker on its own: per-cursor FIFO (§3.2.1) ──────────────────────────


async def _broker() -> tuple[
    SuspensionBroker, Cursor, list[tuple[str, dict[str, Any]]], list[Cursor]
]:
    log = InMemorySessionLog()
    cursor = Cursor(log, None)
    await cursor.append_message({"role": "user", "content": "hi"})
    events: list[tuple[str, dict[str, Any]]] = []
    aborted: list[Cursor] = []

    async def emit(kind: str, suspension: Any, extra: dict[str, Any]) -> None:
        events.append((kind, {**suspension.state(), **extra}))

    def abort(target: Cursor) -> None:
        aborted.append(target)
        target.abort_signal.abort("user")

    return SuspensionBroker(emit, abort), cursor, events, aborted


def _raise(broker: SuspensionBroker, cursor: Cursor) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(
        broker.raise_and_wait(
            cursor=cursor,
            signal=cursor.abort_signal,
            spec=validate_suspension_spec(SPEC),
            site="turn_end",
            extension="x",
            call=None,
            entry_id=None,
            continuation={},
        )
    )


async def test_a_second_suspension_on_a_cursor_waits_its_turn() -> None:
    broker, cursor, events, _ = await _broker()
    first, second = _raise(broker, cursor), _raise(broker, cursor)
    await asyncio.sleep(0.01)
    [open_first, queued_second] = broker.pending(cursor, "cursor")
    assert (open_first.queued, queued_second.queued) == (False, True)
    with pytest.raises(ValueError, match="queued"):
        broker.answer(queued_second.id, "step", {"note": ""})
    broker.answer(open_first.id, "step", {"note": "1"})
    assert (await first)["values"] == {"note": "1"}
    await asyncio.sleep(0.01)
    assert [(k, e["queued"]) for k, e in events if k == "suspension_open"] == [
        ("suspension_open", False),
        ("suspension_open", True),
        ("suspension_open", False),
    ]
    broker.answer(queued_second.id, "step", {"note": "2"})
    assert (await second)["values"] == {"note": "2"}
    assert broker.pending() == []


async def test_an_abort_answer_resolves_every_queued_suspension_as_abort() -> None:
    broker, cursor, events, aborted = await _broker()
    first, second = _raise(broker, cursor), _raise(broker, cursor)
    await asyncio.sleep(0.01)
    open_first = broker.pending()[0]
    broker.answer(open_first.id, "abort", {"note": ""})
    assert (await first)["action"] == "abort"
    resolution = await second
    assert resolution["action"] == "abort" and resolution["answered_by"] is None
    assert aborted == [cursor]
    closed = [(e["id"], e["reason"]) for k, e in events if k == "suspension_closed"]
    assert [reason for _, reason in closed] == ["answered", "aborted"]
    assert [(k, e["queued"]) for k, e in events if e["id"] == closed[1][0]] == [
        ("suspension_open", True),
        ("suspension_closed", True),
    ]


async def test_an_already_aborted_turn_raises_nothing() -> None:
    broker, cursor, events, _ = await _broker()
    cursor.abort_signal.abort("user")
    resolution = await _raise(broker, cursor)
    assert resolution["id"] is None and resolution["action"] == "abort"
    assert events == []
    assert [e for e in cursor.entries() if e.get("type") == "suspension"] == []


async def test_a_cancelled_waiter_closes_its_suspension_as_abandoned() -> None:
    broker, cursor, events, _ = await _broker()
    task = _raise(broker, cursor)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(k, e.get("reason")) for k, e in events][-1] == ("suspension_closed", "abandoned")
    [entry] = [e for e in cursor.entries() if e.get("type") == "suspension"]
    assert entry["reason"] == "abandoned" and not is_incomplete(entry)


# ── lock_changed (§3.4) ──────────────────────────────────────────────────────


async def test_a_change_of_lock_holder_is_a_lock_changed_event() -> None:
    from tau_agent_core.session_lock import LockHolder, WriteAccess

    holder = LockHolder(pid=42, host="box", head="tui", started="2026-10-24T09:00:00Z")

    class _Held:
        def __init__(self) -> None:
            self.read_only = False

        def access(self) -> WriteAccess:
            if not self.read_only:
                return WriteAccess(read_only=False, holder=None, reason=None)
            return WriteAccess(read_only=True, holder=holder, reason="held by pid 42")

        def release(self) -> None:
            pass

    session = _session()
    seen: list[AgentEvent] = []
    session.subscribe(lambda e: seen.append(e) if e.type == "lock_changed" else None)
    held = _Held()
    session._write_locks[id(session.cursor.log)] = (session.cursor.log, held)  # type: ignore[assignment]
    session._show_write_access()
    held.read_only = True
    session._show_write_access()
    session._show_write_access()
    await asyncio.sleep(0)
    assert [(e.read_only, e.lock_holder) for e in seen] == [(True, holder.to_dict())]
    session._write_locks.clear()
