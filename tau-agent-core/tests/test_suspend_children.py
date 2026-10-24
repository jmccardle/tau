"""Suspending a child agent's turn, and refusing a turn that cannot be stepped (M1e).

docs/STEP-DEBUGGER.md §3.9 and §3.12: a child's ``agent`` turn borrows its root
turn's responder for ``api.suspend`` only; the spawn result says who aborted it;
an ``input`` hook refuses an unsteppable turn with ``can_suspend``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.extension_types import HeadlessDialogError
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_agent_core.suspension import TurnSuspended
from tau_agent_core.suspensions import NoResponderError
from tau_agent_core.tools.base import AgentTool
from tau_llm.tools import ToolDefinition

from test_suspend import SPEC, _calls, _model, _script, _text

_results: list[Any] = []
_branches: list[Any] = []


def _child_stepper(api: Any) -> None:
    async def on_written(event: Any, ctx: Any) -> None:
        if event["kind"] == "user" and ctx.cursor.owner is not None:
            try:
                _results.append(await api.suspend(SPEC))
            except Exception as err:
                _results.append(err)

    api.on("entry_written", on_written)


def _delegate() -> AgentTool:
    async def execute(**kw: Any) -> str:
        session = _sessions[-1]
        result = await session.spawn(None, "child task", tools=[], hooks=True)
        _branches.append(result)
        return f"child: {result.ok}"

    return AgentTool(
        definition=ToolDefinition(
            name="delegate",
            label="delegate",
            description="delegate",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
    )


_sessions: list[AgentSession] = []


def _session(**kwargs: Any) -> AgentSession:
    _results.clear()
    _branches.clear()
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[_child_stepper],
        tools=[_delegate()],
        **kwargs,
    )
    session.add_suspension_responder("test")
    _sessions.append(session)
    return session


def _answer_with(session: AgentSession, action: str, seen: list[AgentEvent]) -> None:
    def on_event(event: AgentEvent) -> None:
        seen.append(event)
        if event.type == "suspension_open" and event.suspension:
            sid = event.suspension["id"]
            asyncio.get_running_loop().call_soon(
                lambda: session.answer_suspension(sid, action, {"note": ""}, {"client": "t"})
            )

    session.subscribe(on_event)


SCRIPT = (_calls("delegate"), _text("child ok"), _text("parent done"))


async def test_a_child_of_a_person_submitted_turn_suspends_through_the_root_responder() -> None:
    session = _session()
    seen: list[AgentEvent] = []
    _answer_with(session, "step", seen)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert _results[0]["action"] == "step"
    [opened] = [e for e in seen if e.type == "suspension_open"]
    state = opened.suspension or {}
    assert state["parent_cursor_id"] == session.cursor.id
    assert state["depth"] == 1 and state["spawned_from"] is None
    assert state["session_id"] == session.session_log.id
    [branch] = _branches
    assert branch.ok and branch.end_reason == "done" and branch.aborted_by is None
    child_entries = [e for e in session.session_log.entries() if e.get("source") == "agent"]
    assert child_entries and child_entries[0]["parentId"] is None, "a fresh root, same session"


async def test_a_child_of_a_timer_submitted_root_has_no_responder() -> None:
    session = _session()
    sub = Submission(text="go", source="timer", submitter="cron", submission_id="s1")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.submit(sub), timeout=2)
    assert isinstance(_results[0], NoResponderError)
    assert "root turn" in str(_results[0])


async def test_a_child_aborted_at_its_suspension_says_a_person_stopped_it() -> None:
    session = _session()
    seen: list[AgentEvent] = []
    _answer_with(session, "abort", seen)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    [branch] = _branches
    assert branch.end_reason == "aborted"
    assert branch.aborted_by == "suspension"
    assert branch.answered_by == {"client": "t"}


async def test_aborting_the_parent_resolves_the_childs_suspension_and_blames_the_parent() -> None:
    session = _session()
    seen: list[AgentEvent] = []

    def on_event(event: AgentEvent) -> None:
        seen.append(event)
        if event.type == "suspension_open":
            asyncio.get_running_loop().call_soon(lambda: session.abort(session.cursor))

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert _results[0]["action"] == "abort" and _results[0]["answered_by"] is None
    [closed] = [e for e in seen if e.type == "suspension_closed"]
    assert closed.reason == "aborted"
    [branch] = _branches
    assert (branch.end_reason, branch.aborted_by) == ("aborted", "parent")


async def test_the_tree_scope_lists_a_childs_suspension_from_the_root() -> None:
    session = _session()
    listed: list[list[dict[str, Any]]] = []

    def on_event(event: AgentEvent) -> None:
        if event.type == "suspension_open" and event.suspension:
            listed.append(session.pending_suspensions(session.cursor, "tree"))
            listed.append(session.pending_suspensions(session.cursor, "cursor"))
            sid = event.suspension["id"]
            asyncio.get_running_loop().call_soon(
                lambda: session.answer_suspension(sid, "step", {"note": ""})
            )

    session.subscribe(on_event)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    tree, own = listed
    assert len(tree) == 1 and tree[0]["depth"] == 1
    assert own == []


async def test_a_childs_form_keeps_its_own_rule() -> None:
    """Root inheritance is api.suspend's only: a sub-agent cannot ask for command arguments."""
    raised: list[BaseException] = []

    class _Delegate:
        async def form(self, spec: Any) -> Any:
            return {"n": "from a person"}

    def form_in_child(api: Any) -> None:
        async def on_written(event: Any, ctx: Any) -> None:
            if event["kind"] == "user" and ctx.cursor.owner is not None:
                try:
                    await ctx.ui.form({"title": "t", "fields": [{"name": "n", "kind": "text"}]})
                except HeadlessDialogError as err:
                    raised.append(err)

        api.on("entry_written", on_written)

    _results.clear()
    _branches.clear()
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[form_in_child],
        tools=[_delegate()],
    )
    _sessions.append(session)
    session.set_ui_delegate(_Delegate())
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(*SCRIPT)):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert len(raised) == 1 and "allow_user_input=False" in str(raised[0])


# ── §3.9: refusing a turn that cannot be stepped ─────────────────────────────


def _armed(api: Any) -> None:
    def on_input(event: Any, ctx: Any) -> dict[str, Any] | None:
        if not api.can_suspend():
            return {"refuse": "stepping is armed and nothing can answer a suspension here"}
        return None

    api.on("input", on_input)


@pytest.mark.parametrize(
    ("allow", "store"),
    [(False, True), (True, False)],
    ids=["no-allow-user-input", "store-history-off"],
)
async def test_an_armed_input_hook_refuses_an_unsteppable_turn(allow: bool, store: bool) -> None:
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[_armed])
    session.add_suspension_responder("test")
    sub = Submission(
        text="go",
        source="interactive",
        submitter="human",
        submission_id="s1",
        allow_user_input=allow,
        store_history=store,
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        result = await asyncio.wait_for(session.submit(sub), timeout=2)
    assert result.accepted is False
    assert result.rejection_reason == "stepping is armed and nothing can answer a suspension here"
    assert session.session_log.entries() == []


async def test_a_steppable_turn_passes_the_armed_input_hook() -> None:
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[_armed])
    session.add_suspension_responder("test")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await asyncio.wait_for(session.prompt("go"), timeout=2)
    assert any(e.get("type") == "message" for e in session.session_log.entries())


# ── done-callbacks (M1d review) ──────────────────────────────────────────────


async def test_fork_and_delivery_done_callbacks_do_not_report_a_suspended_turn() -> None:
    session = AgentSession(session_log=InMemorySessionLog(), model=_model())
    errors: list[Any] = []
    session._extension_runner.on_error(errors.append)

    async def suspended() -> None:
        raise TurnSuspended()

    task = asyncio.ensure_future(suspended())
    await asyncio.gather(task, return_exceptions=True)
    session._on_fork_task_done("s1", task)
    session._on_delivery_done("c1", task)
    assert errors == []
