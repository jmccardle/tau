"""A trace: one Chrome trace file with the native spans and the driver's together."""

import json
from pathlib import Path

import pytest
from fakes import FakeProvider, Recorder, Reply
from harness import Harness, scenario

from tau import Part, TraceError, trace


def names(path: Path) -> set[str]:
    events = json.loads(path.read_text())
    return {e["name"] for e in events if e["ph"] in "BX"}


@scenario
async def test_a_trace_holds_native_and_driver_spans_of_one_turn(tmp_path: Path) -> None:
    provider = FakeProvider([Reply([Part.Text("hi")])])
    h = await Harness.create(tmp_path, provider=provider, extensions=[Recorder()])
    out = tmp_path / "turn.json"
    with trace.capture(out):
        await h.send("hello")
        await h.settle()
    await h.close()
    seen = names(out)
    assert {"core.write", "core.answer", "py.answer", "store.fsync_log"} <= seen
    assert {"provider", "pre_step recorder"} <= seen
    events = json.loads(out.read_text())
    cats = {e["cat"] for e in events if e["ph"] == "X"}
    assert {"sample", "hook", "driver"} <= cats


@scenario
async def test_spans_record_nothing_when_no_trace_runs(tmp_path: Path) -> None:
    h = await Harness.create(tmp_path, provider=FakeProvider([Reply([Part.Text("hi")])]))
    first = tmp_path / "first.json"
    with trace.capture(first):
        pass
    await h.send("hello")
    await h.settle()
    await h.close()
    assert "core.write" not in names(first)


def test_a_second_trace_while_one_runs_is_refused(tmp_path: Path) -> None:
    with trace.capture(tmp_path / "a.json"), pytest.raises(TraceError, match="already tracing"):
        trace.start(tmp_path / "b.json")


def test_stopping_with_no_trace_is_refused() -> None:
    with pytest.raises(TraceError, match="no trace is running"):
        trace.stop()
