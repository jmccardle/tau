"""Each head shows a provider retry, in the core's words (docs/PROVIDER-RETRY.md §5).

The TUI (a toast) and the REPL (a line) render :class:`TurnStream`'s
``provider_retry`` kind; ``tau -p``
writes it on stderr; ``--mode json`` and the RPC wire carry the event itself, which
``tau-agent-core/tests/test_provider_retry_events.py`` pins.
"""

from __future__ import annotations

from rich.console import Console

from tau_agent_core.events import AgentEvent
from tau_agent_core.provider_retry import retry_notice
from tau_coding_agent import chat_widgets, transcript
from tau_coding_agent.backends import TurnStream
from tau_coding_agent.headless import report_stream_notice
from tau_coding_agent.repl import ReplRenderer
from tau_coding_agent.repl_input import MemoryReader

_TS = 1_700_000_000_000

_RETRY = AgentEvent(
    type="provider_retry",
    timestamp=_TS,
    error="HTTP 503 from model 'm' at 'u': Loading model",
    attempt=1,
    max_retries=3,
    delay_seconds=1.25,
    submission_id="s1",
)


def test_turn_stream_renders_a_retry_as_the_core_notice():
    (structured,) = TurnStream("s1").feed(_RETRY)
    assert structured == {
        "kind": "provider_retry",
        "text": retry_notice(_RETRY.model_dump()),
        "delay_seconds": 1.25,
        "stream": "s1",
    }
    assert structured["text"].endswith("— retry 1 of 3 in 1.2s")


def test_print_mode_writes_the_notice_on_stderr_only(capsys):
    (structured,) = TurnStream("s1").feed(_RETRY)
    report_stream_notice(structured)
    report_stream_notice({"kind": "text_delta", "delta": "x"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"[τ] {structured['text']}\n"


def test_the_repl_prints_the_notice():
    console = Console(record=True, width=200, force_terminal=False)
    renderer = ReplRenderer(console, MemoryReader(), model_name="m")
    (structured,) = TurnStream("s1").feed(_RETRY)
    renderer(structured)
    assert "retry 1 of 3 in 1.2s" in console.export_text()


class _NoTurnBackend:
    """Render events are fed to ``_on_render_event`` directly; no turn runs."""

    async def submit_turn(self, submission: object) -> object:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


async def test_the_tui_raises_a_warning_toast_and_mounts_nothing(make_app):
    app = make_app(create_backend=lambda cfg: _NoTurnBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        boxes_before = len(app.query_one(transcript.ChatDisplay).query(chat_widgets.MessageBox))
        (structured,) = TurnStream("s1").feed(_RETRY)
        await app._on_render_event(structured)
        await pilot.pause()
        toasts = [n for n in app._notifications if "retry 1 of 3" in n.message]
        assert len(toasts) == 1 and toasts[0].severity == "warning"
        boxes_after = len(app.query_one(transcript.ChatDisplay).query(chat_widgets.MessageBox))
        assert boxes_after == boxes_before
