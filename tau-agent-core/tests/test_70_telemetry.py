"""Smoke test for ``examples/70_telemetry.py`` — the W8/G4 readout.

The W7 demo shipped broken because it had never once been executed: its machinery
was validated by a standalone script, but the extension itself died on the first
line of dispatch. So every example that claims to prove a feature works now gets
driven through the REAL path before it is believed.

Here that means: the real extension registry, the real ``api.on("message_end", …)``
notify routing, the real ``ctx.get_usage()`` accessor, and the real ``api.ui``
delegate — with only the network boundary (``stream_simple``) faked. The usage the
fake completion carries is exactly what ``_usage_from_openai`` /
``_build_final_message`` now produce, so the extension is read against the shape
the provider actually emits, not a shape invented for the test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "70_telemetry.py"
_spec = importlib.util.spec_from_file_location("telemetry_70_example", _PATH)
assert _spec is not None and _spec.loader is not None
telemetry = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = telemetry
_spec.loader.exec_module(telemetry)


# Stock llama.cpp: a real `timings` block, and NO `n_ff_total` (fork-only).
STOCK_TIMINGS = {
    "prompt_n": 12,
    "prompt_ms": 40.5,
    "predicted_n": 20,
    "predicted_ms": 250.0,
    "predicted_per_second": 80.0,
}


def _model() -> Model:
    return Model(
        id="local-llm",
        name="local-llm",
        api="openai-completions",
        provider="openai",
        base_url="http://x/v1",
        context_window=8192,
        max_tokens=300,
    )


class _CannedStream:
    def __init__(self, usage: Usage):
        assistant = AssistantMessage(
            content=[TextContent(text="ok")],
            api="openai-completions",
            provider="openai",
            model="local-llm",
            stop_reason="stop",
            timestamp=0,
            usage=usage,
        )
        self._events = [
            TextDeltaEvent(delta="ok", partial=assistant),
            DoneEvent(final=assistant, usage=usage),
        ]

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def result(self):
        return self._events[-1].final

    def abort(self) -> None:
        pass


class _Delegate:
    """Captures ``api.ui.set_status`` exactly as the TUI's delegate would."""

    def __init__(self) -> None:
        self.status: dict[str, str | None] = {}

    def set_status(self, key: str, text: str | None) -> None:
        self.status[key] = text


async def _run_turn_with_usage(monkeypatch, usage: Usage) -> _Delegate:
    """Load the real extension into a real session and drive one real turn."""
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        api_key="k",
        compaction_settings=CompactionSettings(enabled=False),
    )
    delegate = _Delegate()
    session.set_ui_delegate(delegate)
    await session.load_extensions([str(_PATH)])

    async def fake_stream_simple(model, context, options=None):
        return _CannedStream(usage)

    monkeypatch.setattr("tau_agent_core.agent_loop.stream_simple", fake_stream_simple)
    await session.prompt("hi")
    return delegate


async def test_stock_server_reports_speed_and_omits_the_forced_share(monkeypatch):
    """A stock llama.cpp completion: tok/s shows, `forced=` must NOT be invented."""
    usage = Usage(
        input_tokens=12, output_tokens=20, total_tokens=32, extra={"timings": STOCK_TIMINGS}
    )
    delegate = await _run_turn_with_usage(monkeypatch, usage)

    line = delegate.status["telemetry"]
    assert line is not None
    assert "80.0 t/s" in line
    # n_ff_total is absent on a stock build. A `forced=0%` here would be a
    # fabricated claim that the grammar forced nothing.
    assert "forced" not in line


async def test_forced_share_appears_only_when_the_server_reports_it(monkeypatch):
    """The jump-forward fork DOES send `n_ff_total` — then the share is real."""
    usage = Usage(
        input_tokens=12,
        output_tokens=20,
        total_tokens=32,
        extra={"timings": {**STOCK_TIMINGS, "n_ff_total": 15}},
    )
    delegate = await _run_turn_with_usage(monkeypatch, usage)

    line = delegate.status["telemetry"]
    assert line is not None
    assert "forced=75%" in line


async def test_repair_count_is_surfaced(monkeypatch):
    """`repairs` rides the same dict; a constrained tool call should read 0."""
    usage = Usage(
        input_tokens=12,
        output_tokens=20,
        total_tokens=32,
        extra={"timings": STOCK_TIMINGS, "repairs": 0},
    )
    delegate = await _run_turn_with_usage(monkeypatch, usage)

    line = delegate.status["telemetry"]
    assert line is not None
    assert "repairs=0" in line


async def test_a_server_with_no_telemetry_clears_the_slot(monkeypatch):
    """No timings, no repairs → nothing known. Clear, never show a stale reading."""
    usage = Usage(input_tokens=12, output_tokens=20, total_tokens=32)
    delegate = await _run_turn_with_usage(monkeypatch, usage)

    assert delegate.status["telemetry"] is None


async def test_get_usage_does_not_hand_out_the_sessions_own_extra_dict(monkeypatch):
    """``get_usage()`` must deep-copy: ``extra`` is nested, a shallow copy shares it.

    Same bug class as the ``entries()`` shallow copy: an extension that mutates
    what it was handed would silently corrupt the session's own record of the
    completion, and every later reader would see the forgery.
    """
    usage = Usage(
        input_tokens=12, output_tokens=20, total_tokens=32, extra={"timings": STOCK_TIMINGS}
    )
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        api_key="k",
        compaction_settings=CompactionSettings(enabled=False),
    )

    async def fake_stream_simple(model, context, options=None):
        return _CannedStream(usage)

    monkeypatch.setattr("tau_agent_core.agent_loop.stream_simple", fake_stream_simple)
    await session.prompt("hi")

    handed_out = session.get_usage()
    assert handed_out is not None
    handed_out["extra"]["timings"]["predicted_per_second"] = 999999.0

    again = session.get_usage()
    assert again is not None
    assert again["extra"]["timings"]["predicted_per_second"] == 80.0
