"""Replay data on parts (D15): checked on construction, exact through the log, optional."""

import zlib
from pathlib import Path

import pytest

import tau_next
from tau_next import Author, Body, Config, Part, Replay, Session, Stop

SIGNED = Replay("anthropic", '{ "signature" : "ab\\u00e9==\\n" }')  # spacing and escapes kept


def new(directory: Path) -> tuple[Session, int]:
    session = Session.create(Config("{}"), "sys", 0, directory)
    return session, session.cursors()[0]


def reply(session: Session, cursor: int, parts: list[Part]) -> int:
    return session.append(
        session.cursor(cursor).at, Body.Assistant(parts, Stop.End), Author.Provider(), 1
    )


def append_record(directory: Path, payload: bytes) -> None:
    length = len(payload).to_bytes(4, "little")
    crc = zlib.crc32(length + payload).to_bytes(4, "little")
    with (directory / "log").open("ab") as log:
        log.write(length + crc + payload)


def test_a_replay_holds_its_provider_and_its_data_exactly() -> None:
    assert SIGNED.provider == "anthropic"
    assert SIGNED.data == '{ "signature" : "ab\\u00e9==\\n" }'
    assert Replay("anthropic", SIGNED.data) == SIGNED
    assert Replay("gemini", SIGNED.data) != SIGNED
    assert len({SIGNED, Replay("anthropic", SIGNED.data)}) == 1


def test_a_part_carries_replay_and_defaults_to_none() -> None:
    assert Part.Text("t").replay is None
    assert Part.Thinking("t").replay is None
    assert Part.Call("c", "tool", "{}").replay is None
    assert Part.Text("t", SIGNED).replay == SIGNED
    assert Part.Thinking("", replay=SIGNED).replay == SIGNED
    assert Part.Call("c", "tool", "{}", replay=SIGNED).replay == SIGNED
    assert Part.Text("t", SIGNED) != Part.Text("t")


BAD_DATA = {
    "not json": "not json",
    "empty": "",
    "an array": "[]",
    "a string": '"s"',
    "a number": "1",
    "NaN": '{"a": NaN}',
    "Infinity": '{"a": Infinity}',
    "-Infinity": '{"a": -Infinity}',
    "out of range": '{"a": 1e999}',
    "trailing text": '{"a": 1} x',
}


@pytest.mark.parametrize("data", BAD_DATA.values(), ids=BAD_DATA.keys())
def test_bad_replay_data_is_refused_at_construction(data: str) -> None:
    with pytest.raises(tau_next.BadReplay, match="JSON object"):
        Replay("anthropic", data)


def test_a_replay_with_no_provider_is_refused() -> None:
    with pytest.raises(tau_next.BadReplay, match="provider"):
        Replay("", "{}")


def test_bad_replay_is_a_value_error() -> None:
    assert issubclass(tau_next.BadReplay, ValueError)


def test_parts_with_replay_round_trip_through_the_log(tmp_path: Path) -> None:
    parts = [
        Part.Thinking("hm", SIGNED),
        Part.Thinking("", Replay("anthropic", '{"redacted":"zz=="}')),
        Part.Text("hello", Replay("gemini", '{"thoughtSignature":"s"}')),
        Part.Text("plain"),
        Part.Call("c1", "tool", '{"a": 1}', Replay("gemini", '{"thoughtSignature":"t"}')),
        Part.Call("c2", "tool", "{}"),
    ]
    session, cursor = new(tmp_path / "s")
    node = reply(session, cursor, parts)
    session.close()
    body = Session.load(tmp_path / "s").tree.node(node).body
    assert isinstance(body, Body.Assistant)
    assert body.parts == parts


def test_a_part_without_replay_is_logged_as_it_always_was(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    reply(session, cursor, [Part.Text("hi"), Part.Thinking("t"), Part.Call("c", "t", "{}")])
    session.close()
    log = (tmp_path / "s" / "log").read_bytes()
    unsigned = b'[{"Text":"hi"},{"Thinking":"t"},{"Call":{"id":"c","tool":"t","args_raw":"{}"}}]'
    assert b'"parts":' + unsigned in log


def test_a_log_written_before_replay_existed_loads(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    old = (
        b'{"Write":{"node":{"id":2,"parent":1,"time":1,"by":"Provider","body":{"Assistant":'
        b'{"parts":[{"Text":"hi"},{"Thinking":"hm"},{"Call":{"id":"c","tool":"t","args_raw":"{}"}}],'
        b'"stop":"Tools","finish_reason":null,"meta":null}},"delivers":[]},"mv":null}}'
    )
    append_record(tmp_path / "s", old)
    append_record(tmp_path / "s", b'{"Sync":{"pad":null}}')
    body = Session.load(tmp_path / "s").tree.node(2).body
    assert isinstance(body, Body.Assistant)
    assert body.parts == [Part.Text("hi"), Part.Thinking("hm"), Part.Call("c", "t", "{}")]


def test_a_log_holding_bad_replay_data_does_not_load(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    bad = (
        b'{"Write":{"node":{"id":2,"parent":1,"time":1,"by":"Provider","body":{"Assistant":'
        b'{"parts":[{"Text":{"text":"hi","replay":{"provider":"p","data":"[1]"}}}],'
        b'"stop":"End","finish_reason":null,"meta":null}},"delivers":[]},"mv":null}}'
    )
    append_record(tmp_path / "s", bad)
    with pytest.raises(tau_next.Undecodable):
        Session.load(tmp_path / "s")
