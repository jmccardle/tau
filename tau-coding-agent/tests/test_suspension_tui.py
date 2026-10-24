"""The TUI as a live suspension responder (docs/STEP-DEBUGGER.md §3.5, §3.8; M1f-a).

Driven through the real ``TauApp`` and a real ``TauBackend``, with the LLM
faked at ``agent_loop.stream_simple``. The broker is core's
(``tau-agent-core/tests/test_suspend.py``); what is pinned here is that the TUI
registers, renders, answers through ``AgentSession.answer_suspension``, and
follows ``suspension_closed``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest
from textual.widgets import Button, Static

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, TextContent, ToolCall, Usage

from tau_agent_core.suspensions import validate_suspension_spec
from tau_coding_agent import suspension_ui

_TS = 1_700_000_000_000
_REPO = Path(__file__).resolve().parents[2]
_STEP_MODE = str(_REPO / "examples" / "80_step_mode.py")

SPEC: dict[str, Any] = {
    "title": "Break",
    "text": "look at this",
    "evidence": [{"label": "why", "text": "because"}],
    "fields": [{"name": "note", "kind": "text"}],
    "actions": [{"label": "step"}],
}


def _assistant(content: list[Any], stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, final: AssistantMessage) -> None:
        self._events = [DoneEvent(final=final, usage=Usage())]

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for event in self._events:
            yield event

    async def result(self) -> AssistantMessage:
        return self._events[0].final

    def abort(self) -> None:
        pass


def _script(*finals: AssistantMessage) -> Callable[..., Any]:
    queue = list(finals)

    async def fake(model: Any, context: Any, options: Any = None) -> _Stream:
        return _Stream(queue.pop(0))

    return fake


class _Submit:
    """Duck-typed ``Input.Submitted``: ``on_input_submitted`` reads only ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


@pytest.fixture
def listing(tmp_path: Path) -> Path:
    (tmp_path / "marker.txt").write_text("x")
    return tmp_path


def _app(make_app: Any, **kwargs: Any) -> Any:
    return make_app(
        config={
            "models": {
                "m": {
                    "backend": "openai",
                    "model": "m",
                    "base_url": "http://localhost/v1",
                    "api_key": "not-needed",
                    "tools": ["ls"],
                }
            },
        },
        **kwargs,
    )


async def _until(pilot: Any, predicate: Callable[[], Any], tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("the condition never held")


def _buttons_laid_out(app: Any) -> bool:
    """Whether the modal is on top with its button row mounted and given a height.

    A query, not ``query_one``: the row may not be mounted yet when the screen is.
    """
    screen = _screen(app)
    if screen is None:
        return False
    rows = screen.query("#suspension-buttons")
    return bool(rows) and rows.first().region.height > 0


async def _shown(pilot: Any, app: Any) -> suspension_ui.SuspensionScreen:
    """Wait until the modal is on top and laid out, so a click lands on its buttons."""
    await _until(pilot, lambda: _buttons_laid_out(app))
    screen = _screen(app)
    assert screen is not None
    return screen


def _screen(app: Any) -> suspension_ui.SuspensionScreen | None:
    responder = app._suspension_responder
    screen = None if responder is None else responder.screen
    if screen is None or not screen.is_current:
        return None
    return screen


def _raise(app: Any, spec: dict[str, Any] = SPEC) -> asyncio.Task[dict[str, Any]]:
    """Raise a suspension on the head cursor through the session's own broker."""
    session = app.current_backend.agent_session
    cursor = session.cursor
    return asyncio.create_task(
        session._suspensions.raise_and_wait(
            cursor=cursor,
            signal=cursor.abort_signal,
            spec=validate_suspension_spec(spec),
            site="turn_end",
            extension="probe",
            call=None,
            entry_id=None,
            continuation={},
        )
    )


def _close_reason(entry: dict[str, Any]) -> str:
    """The one reader of a finalized suspension entry's close reason."""
    return str(entry["close_reason"])


def _side_entries(app: Any) -> list[dict[str, Any]]:
    return [e for e in app.current_session.entries() if e.get("type") == "suspension"]


# ── the responder ────────────────────────────────────────────────────────────


async def test_the_tui_registers_as_its_sessions_live_responder(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        session = app.current_backend.agent_session
        assert session.suspension_responders == (suspension_ui.RESPONDER_NAME,)
        previous = app._suspension_responder
        app._rebind_after_session_swap()
        assert session.suspension_responders == (suspension_ui.RESPONDER_NAME,)
        assert app._suspension_responder is not previous


# ── rendering, answering, closing ────────────────────────────────────────────


async def test_a_suspension_opens_the_modal_and_an_answer_reaches_the_broker(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        waiting = _raise(app)
        await _shown(pilot, app)
        screen = _screen(app)
        assert screen is not None
        assert screen.query_one(".tau-dialog-title", Static).render().plain == "Break"
        site = str(screen.query_one("#suspension-site", Static).render())
        assert "probe at turn_end" in site and "cursor" in site
        assert "because" in str(screen.query_one(".suspension-evidence", Static).render())
        assert not screen.query_one("#suspension-abort-turn", Button).disabled
        screen.query_one("#ext-form-field-0").value = "fine"  # type: ignore[attr-defined]
        await pilot.click("#suspension-action-0")
        resolution = await asyncio.wait_for(waiting, timeout=2)
        assert resolution["action"] == "step"
        assert resolution["values"] == {"note": "fine"}
        assert resolution["answered_by"] == suspension_ui.ANSWERED_BY
        await _until(pilot, lambda: _screen(app) is None and len(app.screen_stack) == 1)
        [entry] = _side_entries(app)
        assert _close_reason(entry) == "answered"


async def test_esc_does_not_close_the_modal(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        waiting = _raise(app)
        await _shown(pilot, app)
        await pilot.press("escape")
        await pilot.pause()
        assert _screen(app) is not None
        waiting.cancel()


async def test_a_refused_answer_keeps_the_modal_up(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        waiting = _raise(app)
        await _shown(pilot, app)
        screen = _screen(app)
        assert screen is not None
        assert "not one of its actions" in (
            screen._answer_with(screen.suspension_id, "jump", {}) or ""
        )
        assert screen.answerable and not waiting.done()
        waiting.cancel()


async def test_abort_turn_aborts_and_the_close_dismisses_the_modal(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        waiting = _raise(app)
        await _shown(pilot, app)
        await pilot.click("#suspension-abort-turn")
        resolution = await asyncio.wait_for(waiting, timeout=2)
        assert resolution["action"] == "abort" and resolution["answered_by"] is None
        await _until(pilot, lambda: len(app.screen_stack) == 1)
        [entry] = _side_entries(app)
        assert _close_reason(entry) == "aborted"


async def test_another_responders_answer_dismisses_the_modal(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        waiting = _raise(app)
        await _shown(pilot, app)
        screen = _screen(app)
        assert screen is not None
        session = app.current_backend.agent_session
        session.answer_suspension(screen.suspension_id, "step", {"note": ""}, {"client": "rpc"})
        assert (await asyncio.wait_for(waiting, timeout=2))["answered_by"] == {"client": "rpc"}
        await _until(pilot, lambda: len(app.screen_stack) == 1)
        assert app._suspension_responder.screen is None


# ── the queue ────────────────────────────────────────────────────────────────


async def test_a_queued_suspension_is_listed_and_unanswerable_until_the_front(make_app) -> None:
    app = _app(make_app)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        first = _raise(app)
        await _shown(pilot, app)
        second = _raise(app, {**SPEC, "title": "Second"})
        await _until(
            pilot,
            lambda: "Second" in str(_screen(app).query_one("#suspension-queue", Static).render()),  # type: ignore[union-attr]
        )
        screen = _screen(app)
        assert screen is not None and screen.state["spec"]["title"] == "Break"
        assert "queued: Second" in str(screen.query_one("#suspension-queue", Static).render())
        responder = app._suspension_responder
        [_, queued] = app.current_backend.agent_session.pending_suspensions(scope="session")
        assert queued["queued"] is True
        assert "queued" in (responder.answer(queued["id"], "step", {"note": ""}) or "")
        await pilot.click("#suspension-action-0")
        await asyncio.wait_for(first, timeout=2)
        await _until(
            pilot,
            lambda: (
                _screen(app) is not None
                and _screen(app).suspension_id == queued["id"]  # type: ignore[union-attr]
                and _screen(app).answerable
            ),  # type: ignore[union-attr]
        )
        await pilot.click("#suspension-action-0")
        assert (await asyncio.wait_for(second, timeout=2))["action"] == "step"
        await _until(pilot, lambda: len(app.screen_stack) == 1)


async def test_a_queued_state_disables_the_actions_until_it_reaches_the_front(make_app) -> None:
    app = _app(make_app)
    answers: list[tuple[str, str, dict[str, Any]]] = []
    state = {
        "id": "s1",
        "cursor_id": "c1",
        "depth": 0,
        "spec": validate_suspension_spec(SPEC),
        "site": "turn_end",
        "extension": "probe",
        "call": None,
        "entry_id": None,
        "queued": True,
    }

    def answer(sid: str, action: str, values: dict[str, Any]) -> None:
        answers.append((sid, action, values))

    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        screen = suspension_ui.SuspensionScreen(state, answer, lambda: None)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#suspension-action-0", Button).disabled
        assert screen.query_one("#suspension-queued", Static).display
        screen._send("step")
        assert answers == []
        screen.set_state({**state, "queued": False}, [])
        await pilot.pause()
        assert not screen.query_one("#suspension-action-0", Button).disabled
        assert not screen.query_one("#suspension-queued", Static).display
        await pilot.click("#suspension-action-0")
        assert answers == [("s1", "step", {"note": ""})]


# ── the example, end to end ──────────────────────────────────────────────────


def _ls_then_text(directory: Path) -> Callable[..., Any]:
    call = ToolCall(id="call-1", name="ls", arguments={"path": str(directory)})
    return _script(_assistant([call], "toolUse"), _assistant([TextContent(text="done")]))


def _tool_results(app: Any) -> list[str]:
    texts = []
    for entry in app.current_session.entries():
        message = entry.get("message") or {}
        if entry.get("type") == "message" and message.get("role") == "toolResult":
            texts.append(" ".join(c.get("text", "") for c in message.get("content", [])))
    return texts


async def test_step_mode_steps_a_scripted_tool_call(
    make_app, monkeypatch, listing, wait_for_workers_settled
) -> None:
    monkeypatch.setattr("tau_agent_core.agent_loop.stream_simple", _ls_then_text(listing))
    app = _app(make_app, extension_paths=[_STEP_MODE])
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await app.on_input_submitted(_Submit("list it"))
        await _shown(pilot, app)
        screen = _screen(app)
        assert screen is not None
        assert screen.state["spec"]["title"] == "Step: ls"
        assert screen.state["site"] == "tool_call"
        assert screen.state["call"]["id"] == "call-1"
        assert str(listing) in str(screen.query_one("#suspension-call", Static).render())
        assert not screen.query("#suspension-abort-turn")
        assert _tool_results(app) == []
        await pilot.click("#suspension-action-0")
        await wait_for_workers_settled(app)
        await _until(pilot, lambda: len(app.screen_stack) == 1)
        assert any("marker.txt" in text for text in _tool_results(app))
        [entry] = _side_entries(app)
        assert entry["resolution"]["action"] == "step"
        assert entry["resolution"]["answered_by"] == suspension_ui.ANSWERED_BY


async def test_step_mode_abort_runs_no_tool(
    make_app, monkeypatch, listing, wait_for_workers_settled
) -> None:
    monkeypatch.setattr("tau_agent_core.agent_loop.stream_simple", _ls_then_text(listing))
    app = _app(make_app, extension_paths=[_STEP_MODE])
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await app.on_input_submitted(_Submit("list it"))
        await _shown(pilot, app)
        await pilot.click("#suspension-action-2")
        await wait_for_workers_settled(app)
        await _until(pilot, lambda: len(app.screen_stack) == 1)
        assert not any("marker.txt" in text for text in _tool_results(app))
        [entry] = _side_entries(app)
        assert entry["resolution"]["action"] == "abort"
        assert entry["resolution"]["answered_by"] == suspension_ui.ANSWERED_BY
