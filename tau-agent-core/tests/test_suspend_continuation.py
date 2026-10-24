"""The continuation a suspension records (docs/STEP-DEBUGGER.md §3.7.3, M1e).

Milestone 1 records it; milestone 2 resumes from it. These pin each row of
§3.7.3's "``then``, by case" table and the fields beside it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog

from test_suspend import SPEC, _calls, _model, _script, _text, _tool

Predicate = Callable[[str, dict[str, Any]], bool]


def _stepper(when: Predicate) -> Callable[[Any], None]:
    def factory(api: Any) -> None:
        def on(hook: str) -> Callable[..., Any]:
            async def handler(event: Any, ctx: Any) -> None:
                if when(hook, event):
                    await api.suspend(SPEC)

            return handler

        for hook in ("tool_call", "entry_written", "turn_end", "user_turn_end"):
            api.on(hook, on(hook))

    return factory


def _returner(hook: str) -> Callable[[Any], None]:
    def factory(api: Any) -> None:
        api.on(hook, lambda event, ctx: {"message": {"customType": "n", "content": hook}})

    return factory


async def _continuations(
    when: Predicate,
    *finals: Any,
    parallel: bool = False,
    before: tuple[Callable[[Any], None], ...] = (),
    after: tuple[Callable[[Any], None], ...] = (),
) -> list[dict[str, Any]]:
    ran: list[str] = []
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[*before, _stepper(when), *after],
        tools=[_tool("one", ran), _tool("two", ran)],
        tool_execution_mode="parallel" if parallel else "sequential",
    )
    session.add_suspension_responder("test")

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]
            asyncio.get_running_loop().call_soon(
                lambda: session.answer_suspension(sid, "step", {"note": ""})
            )

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*finals)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    entries = session.session_log.entries()
    return [{**e["continuation"], "_entry": e} for e in entries if e.get("type") == "suspension"]


def _kind(kind: str, role: str | None = None) -> Predicate:
    def when(hook: str, event: dict[str, Any]) -> bool:
        if hook != kind:
            return False
        if role is None:
            return True
        return bool(event.get("kind") == role)

    return when


def _pending(continuation: dict[str, Any]) -> list[tuple[str, bool, Any, Any]]:
    return [
        (c["id"], c["prepared"], c["effective_args"], c["blocked"])
        for c in continuation["pending_calls"]
    ]


async def test_a_user_entry_samples_next() -> None:
    [c] = await _continuations(_kind("entry_written", "user"), _text("ok"))
    assert (c["phase"], c["then"], c["loop"], c["turn_index"]) == (
        "dispatch:entry_written",
        "sample",
        "run",
        0,
    )
    assert c["group"] == [c["_entry"]["entryId"]] and c["index"] == 0
    assert c["pending_calls"] == [] and c["pending_abort"] is None
    assert c["repeat"] == {"prev_batch": None, "count": 1}


async def test_an_assistant_entry_with_calls_runs_its_tools_next() -> None:
    c, text_only = await _continuations(
        _kind("entry_written", "assistant"), _calls("one", "two"), _text("ok")
    )
    assert c["then"] == "tools"
    assert _pending(c) == [("c-one", False, None, None), ("c-two", False, None, None)]
    assert (text_only["then"], text_only["turn_index"]) == ("break", 1)


async def test_a_text_only_assistant_entry_breaks_next() -> None:
    [c] = await _continuations(_kind("entry_written", "assistant"), _text("ok"))
    assert c["then"] == "break"


async def test_a_sequential_result_runs_the_rest_then_reaches_turn_end() -> None:
    first, second = await _continuations(
        _kind("entry_written", "toolResult"), _calls("one", "two"), _text("ok")
    )
    assert (first["then"], _pending(first)) == ("tools", [("c-two", False, None, None)])
    assert (second["then"], second["pending_calls"]) == ("turn_end", [])


async def test_parallel_results_are_one_group_with_nothing_pending() -> None:
    first, second = await _continuations(
        _kind("entry_written", "toolResult"), _calls("one", "two"), _text("ok"), parallel=True
    )
    assert first["then"] == second["then"] == "turn_end"
    assert first["pending_calls"] == []
    assert (first["index"], second["index"]) == (0, 1)
    assert first["group"] == second["group"] and len(first["group"]) == 2


async def test_a_sequential_tool_call_records_the_unprepared_rest() -> None:
    def second_call(hook: str, event: dict[str, Any]) -> bool:
        return hook == "tool_call" and event["tool_call_id"] == "c-two"

    [c] = await _continuations(second_call, _calls("one", "two"), _text("ok"))
    assert (c["phase"], c["then"], c["handler"]) == ("dispatch:tool_call", "tools", 0)
    assert _pending(c) == [("c-two", False, None, None)]


async def test_a_parallel_tool_call_records_the_prepared_calls_before_it() -> None:
    def second_call(hook: str, event: dict[str, Any]) -> bool:
        return hook == "tool_call" and event["tool_call_id"] == "c-two"

    def blocker(api: Any) -> None:
        def on_call(event: Any, ctx: Any) -> dict[str, Any] | None:
            if event["tool_call_id"] == "c-one":
                event["input"]["seen"] = True
            return None

        api.on("tool_call", on_call)

    [c] = await _continuations(
        second_call, _calls("one", "two"), _text("ok"), parallel=True, before=(blocker,)
    )
    assert c["handler"] == 1
    assert _pending(c) == [("c-one", True, {"seen": True}, None), ("c-two", False, None, None)]
    assert [g[0] for g in c["gates"]] == [
        "test_suspend_continuation.test_a_parallel_tool_call_records_the_prepared_calls_before_it.<locals>.blocker",
        "test_suspend_continuation._stepper.<locals>.factory",
    ]


async def test_turn_end_on_a_text_only_turn_ends_the_prompt_and_keeps_earlier_returns() -> None:
    [c] = await _continuations(_kind("turn_end"), _text("ok"), before=(_returner("turn_end"),))
    assert (c["phase"], c["then"]) == ("dispatch:turn_end", "end_prompt")
    assert c["returned"] == [{"customType": "n", "content": "turn_end"}]


async def test_turn_end_after_a_tool_turn_samples() -> None:
    first, _second = await _continuations(_kind("turn_end"), _calls("one"), _text("ok"))
    assert (first["then"], first["turn_index"]) == ("sample", 0)
    assert _second["then"] == "end_prompt" and _second["turn_index"] == 1


async def test_a_turn_end_node_on_a_text_only_turn_breaks() -> None:
    [c] = await _continuations(
        _kind("entry_written", "custom"), _text("ok"), after=(_returner("turn_end"),)
    )
    assert c["then"] == "break"


async def test_user_turn_end_ends_the_prompt_and_its_node_is_followed_by_nothing() -> None:
    def when(hook: str, event: dict[str, Any]) -> bool:
        return hook == "user_turn_end" or (hook == "entry_written" and event["kind"] == "custom")

    dispatch, node = await _continuations(when, _text("ok"), after=(_returner("user_turn_end"),))
    assert (dispatch["phase"], dispatch["then"], dispatch["returned"]) == (
        "dispatch:user_turn_end",
        "end_prompt",
        [],
    )
    assert (node["phase"], node["then"]) == ("dispatch:entry_written", "nothing")


async def test_the_pending_abort_reaches_the_side_entry() -> None:
    def failing(api: Any) -> None:
        def annotate(event: Any, ctx: Any) -> Any:
            if event["kind"] == "assistant":
                raise RuntimeError("bad note")
            return None

        api.on("annotate", annotate)

    ran: list[str] = []
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[failing, _stepper(_kind("entry_written", "assistant"))],
        tools=[_tool("one", ran)],
    )
    session.add_suspension_responder("test")

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]
            asyncio.get_running_loop().call_soon(
                lambda: session.answer_suspension(sid, "step", {"note": ""})
            )

    session.subscribe(on_event)
    script = _script(_calls("one"), _text("ok"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=script):
        with pytest.raises(Exception, match="bad note"):
            await asyncio.wait_for(session.prompt("go"), timeout=2)
    [entry] = [e for e in session.session_log.entries() if e.get("type") == "suspension"]
    assert "bad note" in entry["continuation"]["pending_abort"]
    assert ran == []
