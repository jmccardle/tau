"""Tests for ``examples/ext_kit/stream.py`` — the S54 *event-stream* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S54.

Two layers, mirroring ``test_ext_kit_spawn.py`` (whose ``stream_tau`` this
supervises):

* **pure / synthetic** — the JSONL reader, the tool-call signature, the
  :class:`StreamCounters`, :class:`StuckDetector`, and (clock-injectable)
  :class:`ProgressWatchdog`, plus :func:`monitor_stream` driven over hand-built
  async event iterators (no subprocess): pass-through, stuck→stop→kill(aclose),
  and stall→stop.
* **real spawn** — :func:`monitor_stream` wrapping :func:`ext_kit.spawn.stream_tau`
  against a stdlib fake OpenAI-compatible server, proving the supervisor composes
  on the S53 substrate end to end.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import spawn, stream  # noqa: E402  (path insertion must precede the import)


# ── synthetic event builders ─────────────────────────────────────────────────


def _turn_start(index: int = 0) -> dict[str, Any]:
    return {"type": "turn_start", "timestamp": 0, "turn_index": index}


def _tool_start(
    name: str, args: dict[str, Any] | None = None, call_id: str = "c"
) -> dict[str, Any]:
    return {
        "type": "tool_execution_start",
        "timestamp": 0,
        "tool_call_id": call_id,
        "tool_name": name,
        "args": args if args is not None else {},
    }


def _assistant_end(text: str = "hi") -> dict[str, Any]:
    return {
        "type": "message_end",
        "timestamp": 0,
        "message": {
            "role": "assistant",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "content": [{"type": "text", "text": text}],
        },
    }


class _FakeStream:
    """An async event iterator over a fixed list, tracking whether it was closed.

    Stands in for ``spawn.stream_tau`` in the pure tests: its :meth:`aclose` is
    the *kill* hook :func:`monitor_stream` calls when it stops early, so a test can
    assert the child would have been reaped.
    """

    def __init__(self, events: list[dict[str, Any]], *, hang_after: int | None = None) -> None:
        self._events = events
        self._i = 0
        self._hang_after = hang_after
        self.closed = False

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._hang_after is not None and self._i >= self._hang_after:
            # Simulate a silent child: never produce the next event.
            await asyncio.sleep(3600)
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def aclose(self) -> None:
        self.closed = True


# ── JSONL reader ─────────────────────────────────────────────────────────────


def test_iter_jsonl_decodes_and_skips_blank_lines():
    lines = [
        json.dumps({"type": "agent_start", "timestamp": 0}),
        "",
        "   ",
        json.dumps(_assistant_end()),
    ]
    events = list(stream.iter_jsonl(lines))
    assert [e["type"] for e in events] == ["agent_start", "message_end"]


def test_iter_jsonl_is_strict_on_malformed():
    # Fail-Early: a corrupt non-blank line raises rather than being dropped.
    with pytest.raises(json.JSONDecodeError):
        list(stream.iter_jsonl(['{"type": "ok"}', "{not json}"]))


def test_read_jsonl_parses_a_whole_blob():
    blob = "\n".join(
        [json.dumps({"type": "agent_start", "timestamp": 0}), json.dumps(_assistant_end())]
    )
    events = stream.read_jsonl(blob)
    assert [e["type"] for e in events] == ["agent_start", "message_end"]


# ── tool-call signature ──────────────────────────────────────────────────────


def test_event_tool_signature_only_for_tool_starts():
    assert stream.event_tool_signature(_turn_start()) is None
    assert stream.event_tool_signature(_assistant_end()) is None
    assert stream.event_tool_signature(_tool_start("read", {"path": "a"})) is not None


def test_event_tool_signature_is_arg_order_invariant():
    a = stream.event_tool_signature(_tool_start("read", {"path": "a", "n": 1}))
    b = stream.event_tool_signature(_tool_start("read", {"n": 1, "path": "a"}))
    assert a == b
    # Different args → different signature.
    assert a != stream.event_tool_signature(_tool_start("read", {"path": "b", "n": 1}))
    # Different tool → different signature.
    assert a != stream.event_tool_signature(_tool_start("grep", {"path": "a", "n": 1}))


# ── counters ─────────────────────────────────────────────────────────────────


def test_stream_counters_tally_turns_tools_and_messages():
    counters = stream.StreamCounters()
    for event in [
        _turn_start(0),
        _tool_start("read", {"path": "a"}),
        _tool_start("read", {"path": "b"}),
        _tool_start("grep", {"q": "x"}),
        _assistant_end(),
        _turn_start(1),
        {"type": "agent_end", "timestamp": 0},  # ignored kind
    ]:
        counters.observe(event)
    assert counters.turns == 2
    assert counters.tool_calls == 3
    assert counters.assistant_messages == 1
    assert counters.by_tool == {"read": 2, "grep": 1}
    assert counters.as_dict()["by_tool"] == {"read": 2, "grep": 1}


# ── stuck detector ───────────────────────────────────────────────────────────


def test_stuck_detector_rejects_bad_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        stream.StuckDetector(0)


def test_stuck_detector_trips_on_identical_consecutive_calls():
    det = stream.StuckDetector(3)
    call = _tool_start("read", {"path": "loop"})
    assert det.observe(call) is False  # run 1
    assert det.run_length == 1
    assert det.observe(call) is False  # run 2
    assert det.observe(call) is True  # run 3 >= 3 → trip
    assert det.flagged is True
    # Latches: further observes keep returning True.
    assert det.observe(_turn_start()) is True


def test_stuck_detector_resets_on_different_signature():
    det = stream.StuckDetector(3)
    read = _tool_start("read", {"path": "a"})
    grep = _tool_start("grep", {"q": "x"})
    assert det.observe(read) is False
    assert det.observe(read) is False
    assert det.observe(grep) is False  # different sig → run resets to 1
    assert det.run_length == 1
    assert det.observe(grep) is False
    assert det.observe(grep) is True  # 3 identical greps → trip


def test_stuck_detector_ignores_non_tool_events_between_identical_calls():
    # A text/turn event between identical tool calls neither extends nor resets the
    # run: an assistant that says something between two identical calls is still a loop.
    det = stream.StuckDetector(2)
    call = _tool_start("bash", {"cmd": "ls"})
    assert det.observe(call) is False
    assert det.observe(_assistant_end("thinking...")) is False
    assert det.observe(_turn_start(1)) is False
    assert det.observe(call) is True  # 2nd identical call, despite intervening events


# ── progress watchdog ────────────────────────────────────────────────────────


def test_progress_watchdog_rejects_bad_timeout():
    with pytest.raises(ValueError, match="timeout must be > 0"):
        stream.ProgressWatchdog(0)


def test_progress_watchdog_remaining_and_stall_with_fake_clock():
    clock = {"t": 100.0}
    dog = stream.ProgressWatchdog(5.0, time_fn=lambda: clock["t"])
    assert dog.remaining() == pytest.approx(5.0)
    clock["t"] = 103.0
    assert dog.remaining() == pytest.approx(2.0)
    assert dog.stalled() is False  # 2s left
    clock["t"] = 106.0  # 6s since last event → past the 5s deadline
    assert dog.stalled() is True
    assert dog.flagged is True


def test_progress_watchdog_record_resets_the_gap():
    clock = {"t": 0.0}
    dog = stream.ProgressWatchdog(5.0, time_fn=lambda: clock["t"])
    clock["t"] = 4.0
    dog.record()  # an event arrived at t=4
    clock["t"] = 8.0  # only 4s since that event → not stalled
    assert dog.stalled() is False
    assert dog.remaining() == pytest.approx(1.0)


# ── StreamMonitor.observe (per-event fold) ───────────────────────────────────


def test_stream_monitor_observe_returns_stuck_and_folds_counters():
    mon = stream.StreamMonitor(stuck_limit=2)
    call = _tool_start("read", {"path": "x"})
    assert mon.observe(_turn_start()) is None
    assert mon.observe(call) is None
    assert mon.observe(call) == "stuck"
    assert mon.counters.turns == 1
    assert mon.counters.tool_calls == 2


def test_stream_monitor_without_watchdog_has_none_watchdog():
    mon = stream.StreamMonitor(stuck_limit=3)
    assert mon.watchdog is None
    mon2 = stream.StreamMonitor(stuck_limit=3, progress_timeout=1.0)
    assert isinstance(mon2.watchdog, stream.ProgressWatchdog)


# ── monitor_stream: async driver over a synthetic stream ─────────────────────


async def test_monitor_stream_passes_all_events_through_when_no_flag():
    events = [_turn_start(0), _tool_start("read", {"path": "a"}), _assistant_end()]
    src = _FakeStream(events)
    mon = stream.StreamMonitor(stuck_limit=5)
    seen = [e async for e in stream.monitor_stream(src, monitor=mon)]
    assert [e["type"] for e in seen] == ["turn_start", "tool_execution_start", "message_end"]
    assert mon.counters.tool_calls == 1
    assert mon.counters.assistant_messages == 1
    assert src.closed is True  # source always closed (kills a real child)


async def test_monitor_stream_stops_and_kills_on_stuck():
    call = _tool_start("read", {"path": "loop"})
    trailing = _assistant_end("should never be reached")
    src = _FakeStream([call, call, call, trailing])
    mon = stream.StreamMonitor(stuck_limit=3)
    flags: list[str] = []
    seen = [e async for e in stream.monitor_stream(src, monitor=mon, on_flag=flags.append)]
    # Yields exactly the three offending calls (the 3rd trips), then stops — the
    # trailing event is never delivered.
    assert len(seen) == 3
    assert all(e["type"] == "tool_execution_start" for e in seen)
    assert flags == ["stuck"]
    assert mon.stuck.flagged is True
    assert src.closed is True  # closing stream_tau reaps the child → the "kill"


async def test_monitor_stream_stalls_when_child_goes_silent():
    # One event, then the source hangs forever; a short watchdog fires.
    src = _FakeStream([_turn_start(0)], hang_after=1)
    mon = stream.StreamMonitor(stuck_limit=5, progress_timeout=0.05)
    flags: list[str] = []
    seen = [e async for e in stream.monitor_stream(src, monitor=mon, on_flag=flags.append)]
    assert [e["type"] for e in seen] == ["turn_start"]  # only the pre-stall event
    assert flags == ["stalled"]
    assert mon.watchdog is not None and mon.watchdog.flagged is True
    assert src.closed is True


async def test_monitor_stream_builds_its_own_monitor_when_none_passed():
    src = _FakeStream([_assistant_end()])
    seen = [e async for e in stream.monitor_stream(src, stuck_limit=2)]
    assert [e["type"] for e in seen] == ["message_end"]


# ── real spawn: monitor_stream over spawn.stream_tau (composes on S53) ────────

_SSE_BODY = (
    'data: {"id":"cmpl-1","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Monitored result: done."},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":5,"total_tokens":16}}\n\n'
    "data: [DONE]\n\n"
)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        body = _SSE_BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fake_home(tmp_path, fake_provider, monkeypatch):
    tau_dir = tmp_path / ".tau"
    tau_dir.mkdir()
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider,
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
        "system_prompt": "You are a helpful subagent.",
    }
    (tau_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


async def test_monitor_stream_over_real_stream_tau(fake_home):
    mon = stream.StreamMonitor(stuck_limit=5)
    events = [
        e
        async for e in stream.monitor_stream(
            spawn.stream_tau("summarize the plan", cwd=str(fake_home)), monitor=mon
        )
    ]
    assert events, "expected the child's E-json events to flow through the monitor"
    message_ends = [
        e
        for e in events
        if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"
    ]
    assert message_ends, "expected an assistant message_end in the monitored stream"
    assert mon.counters.assistant_messages >= 1
    assert mon.stuck.flagged is False  # a single well-behaved turn is not stuck
