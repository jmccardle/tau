"""The calibration store and functions through the public Python API (D26 section 6): what
`tau.Tokens` is built on. The fit's own properties are Rust's (`crates/tau-core/tests/
calibration.rs`); here: the bindings say what the fit says, errors are typed, and two processes
updating one record lose nothing."""

import subprocess
import sys
from pathlib import Path

import pytest

from tau import (
    Author,
    Body,
    Calibration,
    CalibrationError,
    CalibrationNewer,
    CalibrationStore,
    Config,
    Outcome,
    Part,
    Stop,
    calibration_features,
)

NAME = "openai/0123456789abcdef.json"
KEY = ("openai", "http://host/v1", "m")


def result(text: str) -> list[float]:
    return calibration_features(Body.Results([("c", Outcome.Ok(text), Author.Tools())]))


def learn(store: CalibrationStore, spans: int, rate: float = 0.25) -> Calibration:
    record = None
    for i in range(spans):
        text = "r" * (400 + 100 * i)
        cost = round((len(text) + 1) * rate)
        record, events = store.observe(NAME, KEY, {"model": "m-1"}, (result(text), cost), "t")
        assert events == []
    assert record is not None
    return record


def test_the_features_of_a_body_count_its_text_by_role_and_class() -> None:
    user = calibration_features(Body.User("Hello world 42"))
    assert len(user) == 48
    assert user[7:14] == [9.0, 1.0, 2.0, 0.0, 2.0, 0.0, 3.0], "the user role's seven classes"
    assert user[42:] == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0], "one user node"
    reply = Body.Assistant([Part.Thinking("hm"), Part.Call("c1", "read", "{}")], Stop.Tools)
    roles = calibration_features(reply)[42:]
    assert roles == [0.0, 0.0, 0.0, 1.0, 0.0, 1.0], "calls, and thinking apart"
    assert not any(calibration_features(Body.Config(Config("{}"))))


def test_a_store_touches_nothing_until_it_observes(tmp_path: Path) -> None:
    directory = tmp_path / "calibration"
    store = CalibrationStore(directory)
    assert store.read(NAME) is None
    assert store.names() == []
    assert store.remove(NAME) is False
    assert not directory.exists()
    learn(store, 1)
    assert (directory / NAME).is_file()
    assert store.names() == [NAME]


def test_a_record_is_ready_after_ten_spans_and_then_predicts_with_a_bound(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    record = learn(store, 9)
    assert (record.observations, record.spans, record.since_reset, record.needed) == (9, 9, 9, 10)
    assert not record.ready
    assert record.error is None
    assert record.predict(result("r" * 4000)) is None
    record = learn(store, 1)
    assert record.ready
    assert record.error is not None
    estimate = record.predict(result("r" * 4000))
    assert estimate is not None
    tokens, bound = estimate
    assert abs(tokens - 1000) <= bound
    assert bound >= 2
    assert record.predict([0.0] * 48) is None, "no node, no count"
    assert record.key == KEY
    assert record.markers == {"model": "m-1"}
    assert len(record.weights) == 42
    assert len(record.per_message) == 6
    again = store.read(NAME)
    assert again is not None
    assert again.predict(result("r" * 4000)) == estimate, "the file holds what was learned"


def test_a_changed_marker_is_an_event_and_resets_the_record(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    assert learn(store, 12).ready
    record, events = store.observe(NAME, KEY, {"model": "m-2"}, None, "later")
    assert events == [("marker", "model", "m-1", "m-2", 0.0)]
    assert not record.ready
    assert (record.observations, record.spans, record.since_reset) == (13, 12, 0)
    assert record.updated == "later"


def test_a_jump_in_what_tokens_cost_is_an_event(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    learn(store, 40)
    found: list[tuple[str, str, str, str, float]] = []
    for i in range(20):  # the same text now costs 60% more
        text = "r" * (400 + 100 * i)
        cost = round((len(text) + 1) * 0.4)
        _, events = store.observe(NAME, KEY, {"model": "m-1"}, (result(text), cost), "t")
        found += events
    assert [e[0] for e in found] == ["jump"]
    assert found[0][4] > 0.3, "by about how much more"


def test_a_file_that_is_not_a_record_raises_and_is_left_alone(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path)
    learn(store, 1)
    path = tmp_path / NAME
    good = path.read_text()
    path.write_text("{}")
    with pytest.raises(CalibrationError, match="not a calibration record"):
        store.read(NAME)
    with pytest.raises(CalibrationError):
        store.observe(NAME, KEY, {}, None, "t")
    assert path.read_text() == "{}"
    newer = good.replace('"version":1', '"version":7', 1)
    path.write_text(newer)
    with pytest.raises(CalibrationNewer, match="version 7, from a newer tau"):
        store.read(NAME)
    assert issubclass(CalibrationNewer, CalibrationError)
    assert path.read_text() == newer
    with pytest.raises(CalibrationError, match="not the name of a calibration record"):
        store.read("../outside.json")
    assert store.remove(NAME) is True
    assert store.read(NAME) is None


WORKER = """
import sys
from tau import Author, Body, CalibrationStore, Outcome, calibration_features

directory, name, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
store = CalibrationStore(directory)
for i in range(count):
    text = "r" * (300 + 50 * i + worker)
    body = Body.Results([("c", Outcome.Ok(text), Author.Tools())])
    span = (calibration_features(body), (len(text) + 1) // 4)
    store.observe(name, ("openai", "http://host/v1", "m"), {"model": "m-1"}, span, f"w{worker}")
"""


def test_two_processes_updating_one_record_lose_nothing(tmp_path: Path) -> None:
    """D26 T3. Each observation is folded into the record on disk under the file's lock, so
    whichever order the processes take it in, every one is counted."""
    each = 60
    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), NAME, str(w), str(each)])
        for w in range(3)
    ]
    for worker in workers:
        assert worker.wait(timeout=120) == 0
    record = CalibrationStore(tmp_path).read(NAME)
    assert record is not None
    assert (record.observations, record.spans) == (3 * each, 3 * each)
    assert record.ready
    left = [p.name for p in (tmp_path / "openai").iterdir() if ".new-" in p.name]
    assert left == [], "no half-written file is left"
    estimate = record.predict(result("r" * 4000))
    assert estimate is not None
    assert abs(estimate[0] - 1000) <= estimate[1] + 5
