"""Each head shows the followUps a user abort dropped, in the core's words.

docs/TURN-SETTINGS.md §5. The TUI (a toast) and the REPL (a line) render
:class:`TurnStream`'s ``follow_ups_dropped`` kind; ``tau -p`` writes it on stderr;
``--mode json`` and the RPC wire carry the event itself, which
``tau-agent-core/tests/test_abort_follow_ups.py`` pins for the wire.
"""

from __future__ import annotations

import json

from rich.console import Console

from tau_agent_core.events import AgentEvent
from tau_agent_core.follow_ups import follow_ups_dropped_notice
from tau_agent_core.event_projection import MessageDeltaProjector
from tau_coding_agent import chat_widgets, transcript
from tau_coding_agent.backends import TurnStream, json_mode_payloads
from tau_coding_agent.headless import report_stream_notice
from tau_coding_agent.repl import ReplRenderer
from tau_coding_agent.repl_input import MemoryReader

_TS = 1_700_000_000_000

_DROPPED = AgentEvent(
    type="follow_ups_dropped",
    timestamp=_TS,
    dropped=["continue on the cheap model"],
    submission_id="s1",
)


def test_turn_stream_renders_the_drop_as_the_core_notice():
    (structured,) = TurnStream("s1").feed(_DROPPED)
    assert structured == {
        "kind": "follow_ups_dropped",
        "text": follow_ups_dropped_notice(_DROPPED.model_dump()),
        "dropped": ["continue on the cheap model"],
        "stream": "s1",
    }
    assert structured["text"] == (
        "Aborted: dropped 1 queued follow-up: 'continue on the cheap model'"
    )


def test_print_mode_writes_the_notice_on_stderr_only(capsys):
    (structured,) = TurnStream("s1").feed(_DROPPED)
    report_stream_notice(structured)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"[τ] {structured['text']}\n"


def test_json_mode_carries_the_event_itself():
    (line,) = json_mode_payloads(MessageDeltaProjector(), _DROPPED)
    payload = json.loads(line) if isinstance(line, str) else line
    assert payload["type"] == "follow_ups_dropped"
    assert payload["dropped"] == ["continue on the cheap model"]


def test_the_repl_prints_the_notice():
    console = Console(record=True, width=200, force_terminal=False)
    renderer = ReplRenderer(console, MemoryReader(), model_name="m")
    (structured,) = TurnStream("s1").feed(_DROPPED)
    renderer(structured)
    assert "dropped 1 queued follow-up" in console.export_text()


class _NoTurnBackend:
    """Render events are fed to ``_on_render_event`` directly; no turn runs."""

    async def submit_turn(self, submission: object) -> object:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


async def test_the_tui_raises_a_warning_toast_and_mounts_nothing(make_app):
    app = make_app(create_backend=lambda cfg: _NoTurnBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        boxes_before = len(app.query_one(transcript.ChatDisplay).query(chat_widgets.MessageBox))
        (structured,) = TurnStream("s1").feed(_DROPPED)
        await app._on_render_event(structured)
        await pilot.pause()
        toasts = [n for n in app._notifications if "queued follow-up" in n.message]
        assert len(toasts) == 1 and toasts[0].severity == "warning"
        boxes_after = len(app.query_one(transcript.ChatDisplay).query(chat_widgets.MessageBox))
        assert boxes_after == boxes_before
