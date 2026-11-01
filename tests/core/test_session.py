"""The session through the public Python API: create, error types, reload.

The job loop (`next_job` / `answer`) is the driver's: its rules are tested in
Rust (`crates/tau-core/tests`) and through the `Harness` (`tests/behavior`).
"""

import zlib
from pathlib import Path

import pytest

import tau
from tau import (
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
    with pytest.raises(tau.Exists):
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
    with pytest.raises(tau.Closed):
        _ = before.moves


# Exceptions


def test_unknown_ids_raise_lookup_errors() -> None:
    session, cursor = new()
    with pytest.raises(tau.NoSuchNode):
        session.tree.node(99)
    with pytest.raises(tau.NoSuchNode):
        session.go_to(cursor, 99, "term", 0)
    with pytest.raises(tau.NoSuchCursor):
        session.cursor(7)
    with pytest.raises(tau.NoSuchCursor):
        session.input(7, Author.Frontend("term"), "hi")
    with pytest.raises(tau.NoSuchCursor):
        session.abort(7)
    with pytest.raises(tau.NoSuchLabel):
        session.label("nope")
    with pytest.raises(tau.NoSuchLabel):
        session.remove_label("nope")
    assert issubclass(tau.NoSuchNode, LookupError)


def test_a_step_on_a_busy_cursor_is_busy() -> None:
    session, cursor = new()
    session.input(cursor, Author.Frontend("term"), "hi")
    session.set_auto(cursor, True)
    with pytest.raises(tau.Busy):
        session.step(cursor)


def test_a_cursor_that_is_not_open_is_refused_and_leaves_no_record() -> None:
    session, cursor = new()
    before = state(session)
    with pytest.raises(tau.NoSuchCursor):
        session.abort(999)
    assert session.cursors() == [cursor]
    assert state(session) == before


def test_a_closed_session_raises_closed() -> None:
    session, cursor = new()
    session.close()
    with pytest.raises(tau.Closed):
        session.cursor(cursor)
    with pytest.raises(tau.Closed):
        session.input(cursor, Author.Frontend("term"), "hi")
    with pytest.raises(tau.Closed):
        session.close()


# Store errors


def test_a_second_open_of_a_session_is_leased(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    with pytest.raises(tau.Leased):
        Session.load(tmp_path / "s")
    session.close()
    Session.load(tmp_path / "s")


def test_a_missing_session_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Session.load(tmp_path / "none")


def test_files_that_are_not_a_session_are_refused(tmp_path: Path) -> None:
    (tmp_path / "log").write_bytes(b"not a tau log at all")
    with pytest.raises(tau.NotASession):
        Session.load(tmp_path)


def test_a_future_file_version_is_refused(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    log = tmp_path / "log"
    data = bytearray(log.read_bytes())
    data[8:12] = (99).to_bytes(4, "little")
    log.write_bytes(data)
    with pytest.raises(tau.Version):
        Session.load(tmp_path)


def test_a_damaged_scratchpad_file_is_corrupt(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    (tmp_path / "scratchpad").write_bytes(b"tau-pad\0" + (1).to_bytes(4, "little") + b"junk")
    with pytest.raises(tau.Corrupt):
        Session.load(tmp_path)


def test_an_intact_log_record_that_does_not_decode_is_an_error_not_a_cut(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    payload = b"not json"
    length = len(payload).to_bytes(4, "little")
    crc = zlib.crc32(length + payload).to_bytes(4, "little")
    with (tmp_path / "log").open("ab") as log:
        log.write(length + crc + payload)
    with pytest.raises(tau.Undecodable, match=r"record \d+ .*log"):
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
        session.input(cursor, Author.Frontend("term"), "hi")
    # Closed, as `close` leaves it: every call raises `Closed`, reads included.
    for call_ in (
        lambda: session.cursor(cursor).at,
        lambda: session.cursor(cursor).items,
        lambda: session.abort(cursor),
        lambda: session.next_job(cursor),
        session.close,
    ):
        with pytest.raises(tau.Closed):
            call_()
    block.rmdir()
    loaded = Session.load(tmp_path / "s")  # the lease was released
    assert state(loaded) == synced
    assert not loaded.cursor(cursor).auto


# Reload


def test_reopening_a_session_gives_the_state_before(tmp_path: Path) -> None:
    session, first = new(tmp_path / "s")
    session.input(first, Author.Frontend("term"), "hi")
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
    session.input(cursor, Author.Frontend("term"), "hi")
    session.set_auto(cursor, True)  # a send
    session.close()  # before the step ran
    loaded = Session.load(tmp_path / "s")
    assert [i.data for i in loaded.cursor(cursor).items] == [
        ItemData.Input(Author.Frontend("term"), "hi")
    ]
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
        Draft(Body.User("x"), [])  # pyright: ignore[reportCallIssue]


def test_values_have_a_readable_repr() -> None:
    session, cursor = new()
    session.input(cursor, Author.Frontend("term"), "hi")
    assert repr(Body.User("hello")) == 'User { text: "hello" }'
    assert "hi" in repr(session.cursor(cursor).items[0])
    assert "System" in repr(session.tree.walk(session.cursor(cursor).at)[-1])
    assert "Ok" in repr(Outcome.Ok("fine"))
    assert "Frontend" in repr(Draft(Body.User("x"), [1], Author.Frontend("term")))
    assert "Halted" in repr(Ending.Halted("why"))


def test_a_cursor_state_repr_names_the_snapshot_not_the_session() -> None:
    session, cursor = new()
    session.input(cursor, Author.Frontend("term"), "a secret scratchpad line")
    state = session.cursor(cursor)
    shown = repr(state)
    assert f"cursor: {cursor}" in shown
    assert "secret" not in shown
    session.append(state.at, Body.User("a later node"), Author.Core(), 1)
    session.set_label("later", cursor)
    assert repr(state) == shown
    assert "later" not in shown


def test_enums_are_not_integers() -> None:
    assert tau.Stop.Tools != 1
    assert tau.Stop.Tools not in {1}
    assert tau.Next.Sample != 1
    assert tau.Hook.PreStep != 0
    assert tau.Hook.PreStep.name == "pre_step"


def test_a_config_states_its_data_and_a_draft_follows_its_fields() -> None:
    with pytest.raises(TypeError):
        Config()  # pyright: ignore[reportCallIssue]
    draft = Draft(Body.User("x"), [3], Author.Core())
    assert (draft.body, draft.delivers, draft.by) == (Body.User("x"), [3], Author.Core())


# Errors


def test_an_os_error_keeps_its_errno_and_the_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError) as caught:
        Session.load(missing)
    assert caught.value.errno == 2
    assert caught.value.strerror == "No such file or directory"
    assert caught.value.filename == str(missing)


def test_a_directory_that_cannot_be_made_names_itself(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    with pytest.raises(NotADirectoryError) as caught:
        Session.create(Config("{}"), "sys", 0, tmp_path / "file" / "s")
    assert caught.value.errno == 20
    assert caught.value.filename == str(tmp_path / "file" / "s")


def test_the_job_loop_is_public_and_refuses_an_answer_to_no_job() -> None:
    session, cursor = new()
    assert session.next_job(cursor) == tau.Job.Rest()
    before = state(session)
    with pytest.raises(tau.WrongAnswer):
        session.answer(cursor, tau.Answer.HookFailed("nothing asked"), 1)
    session.input(cursor, Author.Frontend("term"), "go")
    job = session.next_job(cursor)
    assert isinstance(job, tau.Job.Hooks)
    assert job.point == tau.Hook.PreStep
    with pytest.raises(tau.WrongAnswer):
        session.answer(cursor, tau.Answer.CallDone("c", Outcome.Ok("x"), Author.Core()), 2)
    assert before["at"] == state(session)["at"], "a refused answer changed nothing"
    session.answer(cursor, tau.Answer.Hooks(None, None), 3)
    ended = session.next_job(cursor)
    assert isinstance(ended, tau.Job.Hooks)
    assert (ended.point, ended.ending) == (tau.Hook.PostStep, Ending.Done())


def test_a_log_record_that_cannot_replay_is_a_bad_record(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    records = (
        b'{"Label":{"label":"ghost","cursor":99}}',  # a label on a cursor that never was
        b'{"Sync":{"pad":null}}',  # then a sync, or load cuts the unsynced tail
    )
    with (tmp_path / "log").open("ab") as log:
        for payload in records:
            length = len(payload).to_bytes(4, "little")
            crc = zlib.crc32(length + payload).to_bytes(4, "little")
            log.write(length + crc + payload)
    with pytest.raises(tau.BadRecord):
        Session.load(tmp_path)


def test_the_hook_argument_is_not_a_frontend_affordance() -> None:
    session, cursor = new()
    with pytest.raises(TypeError):
        session.add_item(cursor, ItemData.Extension("x", "y"), hook=cursor)  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        session.set_label("main", cursor, hook=cursor)  # pyright: ignore[reportCallIssue]


# Copies: fork, read without the lease, the first message (D18)


def with_messages(directory: Path) -> tuple[Session, int, int]:
    """A stored session: System, a user message (node 2), a reply (3), on a second branch from
    node 2 another message (4), cursor 1 on it, and a label and a pending item."""
    session, cursor = new(directory)
    first = session.append(session.cursor(cursor).at, Body.User("hello"), Author.Core(), 5)
    reply = session.append(
        first, Body.Assistant([tau.Part.Text("hi")], tau.Stop.End, "stop"), Author.Provider(), 6
    )
    other = session.append(first, Body.User("elsewhere"), Author.Extension("gm"), 7)
    session.open(other, Author.Core(), 8)
    session.set_label("main", cursor)
    session.add_item(cursor, ItemData.Input(Author.Core(), "pending"))
    return session, reply, other


def test_a_fork_is_the_walk_copied_node_for_node_with_one_cursor_on_its_tip(
    tmp_path: Path,
) -> None:
    session, reply, _ = with_messages(tmp_path / "s")
    copy = session.fork(reply, 99)
    walk = session.tree.walk(reply)
    assert [(n.id, n.parent, n.body, n.by, n.time) for n in copy.tree.walk(reply)] == [
        (i, i - 1 if i else None, n.body, n.by, n.time) for i, n in enumerate(walk)
    ]
    assert len(copy.tree) == len(walk), "the other branch is not copied"
    assert copy.cursors() == [0]
    assert copy.cursor(0).at == reply
    assert not copy.cursor(0).auto
    assert copy.labels() == []
    assert copy.cursor(0).items == []
    assert copy.tree.walk(reply)[-1].body == walk[-1].body


def test_a_fork_into_a_directory_reloads_as_it_was_and_the_source_is_untouched(
    tmp_path: Path,
) -> None:
    session, reply, _ = with_messages(tmp_path / "s")
    before = state(session)
    copy = session.fork(reply, 99, tmp_path / "copy")
    saved = state(copy)
    copy.close()
    assert state(session) == before
    assert state(Session.load(tmp_path / "copy")) == saved
    with pytest.raises(tau.Exists):
        session.fork(reply, 99, tmp_path / "copy")


def test_a_fork_of_a_node_that_is_not_there_is_no_such_node(tmp_path: Path) -> None:
    session, _, _ = with_messages(tmp_path / "s")
    with pytest.raises(tau.NoSuchNode):
        session.fork(77, 0, tmp_path / "copy")
    assert not (tmp_path / "copy").exists(), "nothing was written"


def test_read_is_a_copy_of_a_session_another_holds_and_writes_nothing(tmp_path: Path) -> None:
    held, _, _ = with_messages(tmp_path / "s")
    with pytest.raises(tau.Leased):
        Session.load(tmp_path / "s")
    log = (tmp_path / "s" / "log").read_bytes()
    copy = Session.read(tmp_path / "s")
    assert state(copy) == state(held)
    copy.append(0, Body.User("in the copy only"), Author.Core(), 1)  # accepted, never saved
    copy.close()
    assert (tmp_path / "s" / "log").read_bytes() == log
    assert sorted(p.name for p in (tmp_path / "s").iterdir()) == ["log", "scratchpad"]


def test_read_cuts_nothing_from_a_torn_tail(tmp_path: Path) -> None:
    session, _, _ = with_messages(tmp_path / "s")
    session.close()
    log = tmp_path / "s" / "log"
    log.write_bytes(log.read_bytes() + b"half a record")
    copy = Session.read(tmp_path / "s")
    assert len(copy.tree) == 5
    assert log.read_bytes().endswith(b"half a record")
    assert not list((tmp_path / "s").glob("dropped-*"))


def test_read_refuses_a_directory_that_is_not_a_session(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Session.read(tmp_path / "nothing")
    (tmp_path / "log").write_bytes(b"not a log, but long enough")
    with pytest.raises(tau.NotASession):
        Session.read(tmp_path)


def test_first_message_is_the_first_user_nodes_text_without_the_lease(tmp_path: Path) -> None:
    session, _, _ = with_messages(tmp_path / "s")
    assert tau.first_message(tmp_path / "s") == "hello"
    empty, _ = new(tmp_path / "empty")
    assert tau.first_message(tmp_path / "empty") is None
    empty.close()
    session.close()
