"""The session through the public Python API: create, error types, reload.

The job loop (`next_job` / `answer`) is the driver's: its rules are tested in
Rust (`crates/tau-core/tests`) and through the `Harness` (`tests/behavior`).
"""

import zlib
from pathlib import Path

import pytest

import tau_next
from tau_next import (
    Author,
    Body,
    Config,
    Draft,
    Ending,
    ItemData,
    Next,
    Outcome,
    Session,
)


def new(directory: Path | None = None) -> tuple[Session, int]:
    session = Session.create(Config("{}"), "sys", 0, directory)
    return session, session.cursors()[0]


def state(session: Session) -> dict[str, object]:
    cursors = session.cursors()
    return {
        "cursors": cursors,
        "at": {c: session.cursor(c).at for c in cursors},
        "walks": {c: session.tree.walk(session.cursor(c).at) for c in cursors},
        "items": {c: session.cursor(c).items for c in cursors},
        "labels": {c: session.cursor(c).labels for c in cursors},
    }


# Create


def test_create_in_memory_has_a_config_root_a_system_node_and_one_cursor() -> None:
    session, cursor = new()
    assert session.cursors() == [0]
    root, system = session.tree.walk(session.cursor(cursor).at)
    assert root.parent is None
    assert root.body == Body.Config(Config("{}"))
    assert system.parent == root.id
    assert system.body == Body.System("sys")
    assert session.tree.config_at(system.id) == Config("{}")
    assert session.cursor(cursor).next == Next.Wait
    assert not session.cursor(cursor).auto
    assert not session.cursor(cursor).busy


def test_create_in_a_directory_is_loadable_at_once(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    before = state(session)
    session.close()
    assert state(Session.load(tmp_path / "s")) == before


def test_create_refuses_a_directory_that_holds_a_session(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    with pytest.raises(tau_next.Exists):
        new(tmp_path / "s")


def test_a_cursor_snapshot_does_not_see_a_move_made_after_it() -> None:
    session, cursor = new()
    before = session.cursor(cursor)
    moves = before.moves
    session.go_to(cursor, 0, "term", 5)
    after = session.cursor(cursor)
    assert after.at == 0
    assert before.moves == moves, "the snapshot ends where it was taken"
    assert before.at != 0
    assert len(after.moves) == len(moves) + 1
    session.close()
    with pytest.raises(tau_next.Closed):
        _ = before.moves


# Exceptions


def test_unknown_ids_raise_lookup_errors() -> None:
    session, cursor = new()
    with pytest.raises(tau_next.NoSuchNode):
        session.tree.node(99)
    with pytest.raises(tau_next.NoSuchNode):
        session.go_to(cursor, 99, "term", 0)
    with pytest.raises(tau_next.NoSuchCursor):
        session.cursor(7)
    with pytest.raises(tau_next.NoSuchCursor):
        session.input(7, "term", "hi")
    with pytest.raises(tau_next.NoSuchCursor):
        session.abort(7)
    with pytest.raises(tau_next.NoSuchLabel):
        session.label("nope")
    with pytest.raises(tau_next.NoSuchLabel):
        session.remove_label("nope")
    assert issubclass(tau_next.NoSuchNode, LookupError)


def test_a_step_on_a_busy_cursor_is_busy() -> None:
    session, cursor = new()
    session.input(cursor, "term", "hi")
    session.set_auto(cursor, True)
    with pytest.raises(tau_next.Busy):
        session.step(cursor)


def test_a_cursor_that_is_not_open_is_refused_and_leaves_no_record() -> None:
    session, cursor = new()
    before = state(session)
    with pytest.raises(tau_next.NoSuchCursor):
        session.abort(999)
    assert session.cursors() == [cursor]
    assert state(session) == before


def test_a_closed_session_raises_closed() -> None:
    session, cursor = new()
    session.close()
    with pytest.raises(tau_next.Closed):
        session.cursor(cursor)
    with pytest.raises(tau_next.Closed):
        session.input(cursor, "term", "hi")
    with pytest.raises(tau_next.Closed):
        session.close()


# Store errors


def test_a_second_open_of_a_session_is_leased(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    with pytest.raises(tau_next.Leased):
        Session.load(tmp_path / "s")
    session.close()
    Session.load(tmp_path / "s")


def test_a_missing_session_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Session.load(tmp_path / "none")


def test_files_that_are_not_a_session_are_refused(tmp_path: Path) -> None:
    (tmp_path / "log").write_bytes(b"not a tau log at all")
    with pytest.raises(tau_next.NotASession):
        Session.load(tmp_path)


def test_a_future_file_version_is_refused(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    log = tmp_path / "log"
    data = bytearray(log.read_bytes())
    data[8:12] = (99).to_bytes(4, "little")
    log.write_bytes(data)
    with pytest.raises(tau_next.Version):
        Session.load(tmp_path)


def test_a_damaged_scratchpad_file_is_corrupt(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    (tmp_path / "scratchpad").write_bytes(b"tau-pad\0" + (1).to_bytes(4, "little") + b"junk")
    with pytest.raises(tau_next.Corrupt):
        Session.load(tmp_path)


def test_an_intact_log_record_that_does_not_decode_is_an_error_not_a_cut(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    payload = b"not json"
    length = len(payload).to_bytes(4, "little")
    crc = zlib.crc32(length + payload).to_bytes(4, "little")
    with (tmp_path / "log").open("ab") as log:
        log.write(length + crc + payload)
    with pytest.raises(tau_next.Undecodable, match=r"record \d+ .*log"):
        Session.load(tmp_path)


def test_a_failed_save_is_a_crash_the_session_closes_and_reloads_as_last_synced(
    tmp_path: Path,
) -> None:
    session, cursor = new(tmp_path / "s")
    session.set_label("main", cursor)
    synced = state(session)
    block = tmp_path / "s" / "scratchpad.new"
    block.mkdir()  # the store cannot create its temporary scratchpad file here
    with pytest.raises(IsADirectoryError):
        session.input(cursor, "term", "hi")
    # Closed, as `close` leaves it: every call raises `Closed`, reads included.
    for call_ in (
        lambda: session.cursor(cursor).at,
        lambda: session.cursor(cursor).items,
        lambda: session.abort(cursor),
        lambda: session.next_job(cursor),
        session.close,
    ):
        with pytest.raises(tau_next.Closed):
            call_()
    block.rmdir()
    loaded = Session.load(tmp_path / "s")  # the lease was released
    assert state(loaded) == synced
    assert not loaded.cursor(cursor).auto


# Reload


def test_reopening_a_session_gives_the_state_before(tmp_path: Path) -> None:
    session, first = new(tmp_path / "s")
    session.input(first, "term", "hi")
    other = session.open(session.cursor(first).at, Author.Extension("ext"), 3)
    session.add_item(other, ItemData.Extension("timer", "tick"))
    session.go_to(first, 0, "term", 4)
    session.set_label("main", first)
    session.set_label("side", other)
    session.set_label("gone", other)
    session.remove_label("gone")
    before = state(session)
    assert len(before["cursors"]) == 2  # pyright: ignore[reportArgumentType]
    session.close()
    after = Session.load(tmp_path / "s")
    assert state(after) == before


def test_a_send_is_durable_on_return_and_a_loaded_cursor_is_woken_with_auto_off(
    tmp_path: Path,
) -> None:
    session, cursor = new(tmp_path / "s")
    session.input(cursor, "term", "hi")
    session.set_auto(cursor, True)  # a send
    session.close()  # before the step ran
    loaded = Session.load(tmp_path / "s")
    assert [i.data for i in loaded.cursor(cursor).items] == [ItemData.Input("term", "hi")]
    assert not loaded.cursor(cursor).auto
    assert loaded.cursor(cursor).busy, "load wakes every cursor"


def test_open_and_add_item_outside_a_step_are_durable_on_return(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    other = session.open(session.cursor(cursor).at, Author.Extension("ext"), 1)
    item = session.add_item(cursor, ItemData.Extension("timer", "tick"))
    session.close()
    loaded = Session.load(tmp_path / "s")
    assert loaded.cursors() == [cursor, other]
    assert [(i.id, i.data) for i in loaded.cursor(cursor).items] == [
        (item, ItemData.Extension("timer", "tick"))
    ]


# Values


def test_a_draft_must_state_what_it_delivers() -> None:
    with pytest.raises(TypeError):
        Draft(Body.User("x"), Author.Core())  # pyright: ignore[reportCallIssue]


def test_values_have_a_readable_repr() -> None:
    session, cursor = new()
    session.input(cursor, "term", "hi")
    assert repr(Body.User("hello")) == 'User { text: "hello" }'
    assert "hi" in repr(session.cursor(cursor).items[0])
    assert "System" in repr(session.tree.walk(session.cursor(cursor).at)[-1])
    assert "Ok" in repr(Outcome.Ok("fine"))
    assert "Frontend" in repr(Draft(Body.User("x"), Author.Frontend("term"), [1]))
    assert "Halted" in repr(Ending.Halted("why"))
