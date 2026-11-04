"""`copied_from` and `derived_from` (D24 M1): optional, exact through the log, old logs load."""

import zlib
from pathlib import Path

import pytest

import tau
from tau import Author, Body, Config, Part, Session, Stop


def new(directory: Path | None = None) -> tuple[Session, int]:
    session = Session.create(Config("{}"), "sys", 0, directory)
    return session, session.cursors()[0]


def append_record(directory: Path, payload: bytes) -> None:
    length = len(payload).to_bytes(4, "little")
    crc = zlib.crc32(length + payload).to_bytes(4, "little")
    with (directory / "log").open("ab") as log:
        log.write(length + crc + payload)


def test_a_node_has_neither_field_unless_given_them() -> None:
    session, cursor = new()
    node = session.append(session.cursor(cursor).at, Body.User("a"), Author.Core(), 1)
    assert session.tree.node(node).copied_from is None
    assert session.tree.node(node).derived_from is None


def test_append_records_both_and_they_survive_a_reload(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    first = session.append(1, Body.User("a"), Author.Core(), 1)
    copy = session.append(1, Body.User("a"), Author.Core(), 2, copied_from=first)
    summary = session.append(1, Body.User("s"), Author.Core(), 3, derived_from=first)
    session.close()
    loaded = Session.load(tmp_path / "s")
    assert (loaded.tree.node(copy).copied_from, loaded.tree.node(copy).derived_from) == (
        first,
        None,
    )
    assert (loaded.tree.node(summary).copied_from, loaded.tree.node(summary).derived_from) == (
        None,
        first,
    )
    assert cursor == 0


def test_a_source_that_is_not_a_node_is_refused_and_nothing_is_written() -> None:
    session, _ = new()
    before = len(session.tree)
    with pytest.raises(tau.NoSuchNode):
        session.append(1, Body.User("a"), Author.Core(), 1, copied_from=99)
    assert len(session.tree) == before


def test_a_log_written_before_the_fields_existed_loads(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    old = (
        b'{"Write":{"node":{"id":2,"parent":1,"time":1,"by":"Core","body":{"User":"hi"},'
        b'"delivers":[]},"mv":null}}'
    )
    append_record(tmp_path / "s", old)
    append_record(tmp_path / "s", b'{"Sync":{"pad":null}}')
    node = Session.load(tmp_path / "s").tree.node(2)
    assert (node.copied_from, node.derived_from) == (None, None)


def test_a_log_whose_source_is_missing_does_not_load(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    bad = (
        b'{"Write":{"node":{"id":2,"parent":1,"time":1,"by":"Core","body":{"User":"hi"},'
        b'"delivers":[],"copied_from":77},"mv":null}}'
    )
    append_record(tmp_path / "s", bad)
    append_record(tmp_path / "s", b'{"Sync":{"pad":null}}')
    with pytest.raises(tau.BadRecord):
        Session.load(tmp_path / "s")


def test_a_fork_strips_usage_from_a_copy_whose_source_it_leaves_behind() -> None:
    session, cursor = new()
    usage = '{"model":"m","usage":{"input":9,"output":1}}'
    reply = Body.Assistant([Part.Text("r")], Stop.End, None, usage)
    side = session.append(1, reply, Author.Provider(), 1)
    user = session.append(1, Body.User("a"), Author.Core(), 2)
    kept = session.append(user, reply, Author.Core(), 3, copied_from=side)
    own = session.append(kept, reply, Author.Provider(), 4)
    forked = session.fork(own, 9)
    walk = forked.tree.walk(forked.cursor(0).at)
    copy, real = walk[-2], walk[-1]
    assert copy.copied_from is None
    assert copy.body == Body.Assistant([Part.Text("r")], Stop.End, None, '{"model":"m"}')
    assert real.body == reply, "a reply of its own keeps its usage"
    assert cursor == 0
