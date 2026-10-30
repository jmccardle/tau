"""The session through the public Python API: create, drive by hand, reload."""

import zlib
from pathlib import Path

import pytest
from drive import call, run, say, send

import tau_next
from tau_next import (
    Answer,
    Author,
    Body,
    Config,
    Draft,
    Ending,
    Hook,
    ItemData,
    Job,
    Next,
    Outcome,
    Part,
    Session,
    Stop,
)


def new(directory: Path | None = None) -> tuple[Session, int]:
    session = Session.create(Config("{}"), "sys", 0, directory)
    return session, session.cursors()[0]


def bodies(session: Session, cursor: int) -> list[Body]:
    return [n.body for n in session.tree.walk(session.cursor(cursor).at)]


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


# A plain turn


def test_a_plain_turn_reaches_rest_with_a_reply() -> None:
    session, cursor = new()
    item = send(session, cursor, "term", "hi")
    assert session.cursor(cursor).busy
    assert session.cursor(cursor).auto
    assert session.cursor(cursor).items[0].data == ItemData.Input("term", "hi")
    jobs = run(session, cursor, iter([say("hello")]), 6)
    points = [j.point for j in jobs if isinstance(j, Job.Hooks)]
    assert points == [
        Hook.PreStep,
        Hook.PreInput,
        Hook.PreSample,
        Hook.PreReply,
        Hook.PostReply,
        Hook.PostStep,
    ]
    assert Job.Sample() in jobs
    assert isinstance(jobs[-1], Job.Rest)
    assert not session.cursor(cursor).busy
    _, _, user, reply = session.tree.walk(session.cursor(cursor).at)
    assert user.body == Body.User("hi")
    assert user.by == Author.Frontend("term")
    assert user.delivers == [item]
    assert user.time == 6
    assert reply.body == Body.Assistant([Part.Text("hello")], Stop.End)
    assert reply.by == Author.Provider()
    assert session.cursor(cursor).items == []
    assert session.cursor(cursor).next == Next.Wait


def test_the_input_draft_reaches_pre_input_and_an_edit_is_committed() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")

    def edit(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            assert job.draft is not None
            assert job.draft.body == Body.User("hi")
            edited = Draft(Body.User("HI"), job.draft.by, job.draft.delivers)
            return [Answer.Hooks(draft=edited)]
        return None

    run(session, cursor, iter([say("ok")]), on_job=edit)
    assert bodies(session, cursor)[2] == Body.User("HI")


def test_a_halting_pre_input_hook_removes_the_item_and_notifies_the_client() -> None:
    session, cursor = new()
    send(session, cursor, "term", "/nope")

    def halt(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            return [Answer.Hooks(stop="no commands")]
        return None

    jobs = run(session, cursor, on_job=halt)
    last = [j for j in jobs if isinstance(j, Job.Hooks)][-1]
    assert last.point == Hook.PostStep
    assert last.ending == Ending.Halted("no commands")
    assert session.cursor(cursor).items == []
    assert session.take_notices() == [("term", "no commands")]
    assert session.take_notices() == []
    assert not session.cursor(cursor).auto


def test_a_failed_sample_ends_the_step_failed() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")
    failed = iter([Answer.Sampled(None, "boom")])
    jobs = run(session, cursor, failed)
    last = [j for j in jobs if isinstance(j, Job.Hooks)][-1]
    assert last.ending == Ending.Failed("boom")
    # A side node: written under the user node, the cursor stays where it was.
    side = session.tree.node(session.cursor(cursor).at + 1)
    assert side.parent == session.cursor(cursor).at
    assert side.body == Body.Custom("sample_error", "boom")


# A tool batch


def test_a_tool_batch_is_answered_by_one_results_node_in_call_order() -> None:
    session, cursor = new()
    send(session, cursor, "term", "use tools")
    samples = iter([call(("a", "read"), ("b", "write")), say("done")])
    jobs = run(session, cursor, samples)
    batches = [j for j in jobs if isinstance(j, Job.RunCalls)]
    assert [[c.id for c in b.calls] for b in batches] == [["a", "b"]]
    assert batches[0].calls[0].tool == "read"
    assert batches[0].calls[0].args_raw == "{}"
    _, _, _, assistant, results, final = session.tree.walk(session.cursor(cursor).at)
    assert isinstance(assistant.body, Body.Assistant)
    assert assistant.body.stop == Stop.Tools
    assert results.by == Author.Tools()
    assert results.body == Body.Results(
        [
            ("a", Outcome.Ok("ran read"), Author.Tools()),
            ("b", Outcome.Ok("ran write"), Author.Tools()),
        ]
    )
    assert final.body == Body.Assistant([Part.Text("done")], Stop.End)


def test_calls_may_finish_in_any_order() -> None:
    session, cursor = new()
    send(session, cursor, "term", "go")
    samples = iter([call(("a", "t"), ("b", "t")), say("done")])

    def backwards(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.RunCalls):
            done: list[Answer] = [
                Answer.CallDone(c.id, Outcome.Err(c.id), Author.Extension("x")) for c in job.calls
            ]
            return done[::-1]
        return None

    run(session, cursor, samples, on_job=backwards)
    results = bodies(session, cursor)[-2]
    assert isinstance(results, Body.Results)
    assert [o[0] for o in results.outcomes] == ["a", "b"]
    assert results.outcomes[0][1:] == (Outcome.Err("a"), Author.Extension("x"))


def test_abort_during_calls_asks_the_driver_to_cancel_them() -> None:
    session, cursor = new()
    send(session, cursor, "term", "go")
    samples = iter([call(("a", "t"))])
    cancelled: list[bool] = []

    def abort(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.RunCalls):
            cancelled.append(session.abort(cursor))
            return [Answer.CallDone("a", Outcome.Interrupted(), Author.Tools())]
        return None

    run(session, cursor, samples, on_job=abort)
    assert cancelled == [True]
    results = bodies(session, cursor)[-1]
    assert isinstance(results, Body.Results)
    assert results.outcomes[0][1] == Outcome.Interrupted()
    assert not session.cursor(cursor).auto


# Cursors, items, labels, moves


def test_open_wakes_a_cursor_and_go_to_moves_it() -> None:
    session, first = new()
    system = session.cursor(first).at
    other = session.open(system, Author.Extension("ext"), 3)
    assert session.cursors() == [first, other]
    assert session.cursor(other).busy
    assert session.cursor(other).moves[0].from_ is None
    run(session, other)
    send(session, first, "term", "hi")
    run(session, first, iter([say("hey")]))
    assert session.go_to(other, session.cursor(first).at, "term", 4) is False
    run(session, other)
    assert session.cursor(other).at == session.cursor(first).at
    assert not session.cursor(other).auto
    assert session.cursor(other).moves[-1].by == Author.Frontend("term")
    assert session.tree.lca(session.cursor(first).at, system) == system


def test_a_cursor_snapshot_does_not_see_a_move_made_after_it() -> None:
    session, cursor = new()
    run(session, cursor)
    send(session, cursor, "term", "hi")
    run(session, cursor, iter([say("hey")]))
    before = session.cursor(cursor)
    moves = before.moves
    assert len(moves) >= 2
    session.go_to(cursor, 0, "term", 5)
    run(session, cursor)
    after = session.cursor(cursor)
    assert after.at == 0
    assert before.moves == moves, "the snapshot ends where it was taken"
    assert before.at != 0
    assert len(after.moves) == len(moves) + 1
    session.close()
    with pytest.raises(tau_next.Closed):
        _ = before.moves


def test_an_extension_item_wakes_its_cursor_and_stays_in_the_scratchpad() -> None:
    session, cursor = new()
    item = session.add_item(cursor, ItemData.Extension("timer", "tick"))
    assert session.cursor(cursor).busy
    run(session, cursor)
    assert [i.id for i in session.cursor(cursor).items] == [item]
    assert session.add_item(cursor, ItemData.Extension("timer", "tock")) == item + 1


def test_labels_set_move_and_remove() -> None:
    session, first = new()
    other = session.open(session.cursor(first).at, Author.Core(), 0)
    session.set_label("main", first)
    session.set_label("side", first)
    assert session.cursor(first).labels == ["main", "side"]
    session.set_label("main", other)
    assert session.label("main") == other
    assert session.cursor(first).labels == ["side"]
    session.remove_label("side")
    assert session.cursor(first).labels == []


def test_moves_since_step_lists_the_moves_the_step_made() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")
    seen: list[int] = []

    def watch(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PostReply:
            seen.extend(m.to for m in session.cursor(cursor).moves_since_step)
        return None

    run(session, cursor, iter([say("x")]), on_job=watch)
    user, reply = session.cursor(cursor).at - 1, session.cursor(cursor).at
    assert seen == [user, reply]


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
    with pytest.raises(tau_next.NoSuchCursor):
        session.next_job(7)
    with pytest.raises(tau_next.NoSuchLabel):
        session.label("nope")
    with pytest.raises(tau_next.NoSuchLabel):
        session.remove_label("nope")
    assert issubclass(tau_next.NoSuchNode, LookupError)


def test_a_step_on_a_busy_cursor_is_busy() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")
    with pytest.raises(tau_next.Busy):
        session.step(cursor)


def test_a_go_to_while_a_cursor_hook_runs_aborts_and_moves_once_the_step_rests() -> None:
    session, cursor = new()
    system = session.cursor(cursor).at
    send(session, cursor, "term", "hi")
    assert isinstance(session.next_job(cursor), Job.Hooks)
    assert not session.go_to(cursor, 0, "term", 0), "nothing to cancel: the hook finishes"
    assert session.cursor(cursor).at == system
    jobs = run(session, cursor)
    endings = [j.ending for j in jobs if isinstance(j, Job.Hooks) and j.ending is not None]
    assert endings[0] == Ending.Aborted()
    assert not any(isinstance(j, Job.Sample) for j in jobs)
    assert session.cursor(cursor).at == 0


def test_a_results_draft_naming_a_call_twice_fails_the_step_and_changes_nothing() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")
    twice = [("a", Outcome.Ok("1"), Author.Tools()), ("a", Outcome.Ok("2"), Author.Tools())]
    bad = Draft(Body.Results(twice), Author.Core(), [])
    before = bodies(session, cursor)

    def corrupt(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            return [Answer.Hooks(draft=bad)]
        return None

    jobs = run(session, cursor, on_job=corrupt)
    endings = [j.ending for j in jobs if isinstance(j, Job.Hooks) and j.ending is not None]
    assert endings == [Ending.Failed("a node names a call id twice")]
    assert bodies(session, cursor) == before, "the refused commit wrote nothing"
    assert [i.data for i in session.cursor(cursor).items] == [ItemData.Input("term", "hi")]
    assert not session.cursor(cursor).auto


def test_a_feed_that_answers_no_job_is_refused_and_the_job_stays_out() -> None:
    session, cursor = new()
    with pytest.raises(tau_next.WrongAnswer):
        session.answer(cursor, say("x"), 0)
    send(session, cursor, "term", "hi")
    assert isinstance(session.next_job(cursor), Job.Hooks)
    with pytest.raises(tau_next.WrongAnswer):
        session.answer(cursor, say("x"), 0)
    session.answer(cursor, Answer.Hooks(), 0)  # the hook job was still out
    run(session, cursor, iter([say("x")]))


def test_a_closed_session_raises_closed() -> None:
    session, cursor = new()
    session.close()
    with pytest.raises(tau_next.Closed):
        session.cursor(cursor)
    with pytest.raises(tau_next.Closed):
        send(session, cursor, "term", "hi")
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


# Persistence


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


def test_reopening_a_session_gives_the_state_before(tmp_path: Path) -> None:
    session, first = new(tmp_path / "s")
    send(session, first, "term", "hi")
    run(session, first, iter([call(("a", "t")), say("done")]), 2)
    other = session.open(session.cursor(first).at - 2, Author.Extension("ext"), 3)
    run(session, other)
    session.add_item(other, ItemData.Extension("timer", "tick"))
    run(session, other)
    session.set_label("main", first)
    session.set_label("side", other)
    session.set_label("gone", other)
    session.remove_label("gone")
    before = state(session)
    assert len(before["cursors"]) == 2  # pyright: ignore[reportArgumentType]
    session.close()
    after = Session.load(tmp_path / "s")
    assert state(after) == before


def test_a_send_is_durable_on_return_and_a_loaded_cursor_has_auto_off(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    send(session, cursor, "term", "hi")
    session.close()
    loaded = Session.load(tmp_path / "s")
    assert [i.data for i in loaded.cursor(cursor).items] == [ItemData.Input("term", "hi")]
    assert not loaded.cursor(cursor).auto


def test_load_wakes_a_cursor_with_input_pending_for_pre_step_and_post_step_only(
    tmp_path: Path,
) -> None:
    session, cursor = new(tmp_path / "s")
    send(session, cursor, "term", "hi")  # auto-step on, durable on return
    session.close()  # before the step ran
    loaded = Session.load(tmp_path / "s")
    assert loaded.cursor(cursor).busy
    assert not loaded.cursor(cursor).auto
    jobs = run(loaded, cursor)
    assert [j.point for j in jobs if isinstance(j, Job.Hooks)] == [Hook.PreStep, Hook.PostStep]
    assert not any(isinstance(j, Job.Sample) for j in jobs)
    assert [i.data for i in loaded.cursor(cursor).items] == [ItemData.Input("term", "hi")]
    assert len(loaded.tree.walk(loaded.cursor(cursor).at)) == 2  # no User node, no reply


def test_close_drops_what_was_not_synced_as_a_crash_would(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    send(session, cursor, "term", "hi")
    before = state(session)
    committed: list[Job] = []

    def crash(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Sample):
            committed.append(job)
            session.close()
        return None

    with pytest.raises(tau_next.Closed):
        run(session, cursor, iter([say("x")]), on_job=crash)
    # The input was committed in memory, but not synced: it is still an item.
    loaded = Session.load(tmp_path / "s")
    assert state(loaded) == before


def test_post_hooks_see_synced_data(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    send(session, cursor, "term", "hi")
    on_disk: list[int] = []

    def peek(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PostReply:
            copy = tmp_path / "copy"
            copy.mkdir(exist_ok=True)
            for name in ("log", "scratchpad"):
                if (tmp_path / "s" / name).exists():
                    (copy / name).write_bytes((tmp_path / "s" / name).read_bytes())
            on_disk.append(len(Session.load(copy).tree.walk(session.cursor(cursor).at)))
        return None

    run(session, cursor, iter([say("x")]), on_job=peek)
    assert on_disk == [4]


# Syncs, store failure, hook edits, cursor checks, repr


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


def test_a_failed_save_is_a_crash_the_session_closes_and_reloads_as_last_synced(
    tmp_path: Path,
) -> None:
    session, cursor = new(tmp_path / "s")
    session.set_label("main", cursor)
    synced = state(session)
    block = tmp_path / "s" / "scratchpad.new"
    block.mkdir()  # the store cannot create its temporary scratchpad file here
    with pytest.raises(IsADirectoryError):
        send(session, cursor, "term", "hi")
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


def edit_input_without_delivering(job: Job) -> list[Answer] | None:
    if isinstance(job, Job.Hooks) and job.point == Hook.PreInput and job.draft:
        edited = Draft(Body.User("edited"), job.draft.by, [])
        return [Answer.Hooks(None, edited)]
    return None


def test_a_draft_must_state_what_it_delivers() -> None:
    with pytest.raises(TypeError):
        Draft(Body.User("x"), Author.Core())  # pyright: ignore[reportCallIssue]


def test_a_pre_input_draft_that_delivers_nothing_leaves_the_input_pending() -> None:
    session, cursor = new()
    session.input(cursor, "term", "hi")
    run(session, cursor)  # auto-step is off: the woken step offers no input
    session.step(cursor)
    jobs = run(
        session, cursor, iter(say("ok") for _ in range(10)), on_job=edit_input_without_delivering
    )
    assert sum(isinstance(j, Job.Sample) for j in jobs) == 1
    users = [b for b in bodies(session, cursor) if isinstance(b, Body.User)]
    assert users == [Body.User("edited")]
    assert [i.data for i in session.cursor(cursor).items] == [ItemData.Input("term", "hi")]


def test_a_pre_input_hook_that_leaves_no_draft_commits_nothing() -> None:
    session, cursor = new()
    session.input(cursor, "term", "hi")
    run(session, cursor)  # auto-step is off: the woken step offers no input
    session.step(cursor)

    def take_the_draft(job: Job) -> list[Answer] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            return [Answer.Hooks(None, None)]
        return None

    jobs = run(session, cursor, iter(say("ok") for _ in range(10)), on_job=take_the_draft)
    assert not any(isinstance(b, Body.User) for b in bodies(session, cursor))
    assert [i.data for i in session.cursor(cursor).items] == [ItemData.Input("term", "hi")]
    endings = [j.ending for j in jobs if isinstance(j, Job.Hooks) and j.ending is not None]
    assert endings == [Ending.Done()], "leaving no draft is not a failure"


def test_a_cursor_that_is_not_open_is_refused_and_leaves_no_record() -> None:
    session, cursor = new()
    before = state(session)
    for method in (session.abort, session.next_job):
        with pytest.raises(tau_next.NoSuchCursor):
            method(999)
    with pytest.raises(tau_next.NoSuchCursor):
        session.answer(999, Answer.Hooks(), 0)
    assert session.cursors() == [cursor]
    assert state(session) == before


def test_values_have_a_readable_repr() -> None:
    session, cursor = new()
    send(session, cursor, "term", "hi")
    job = session.next_job(cursor)
    assert repr(Body.User("hello")) == 'User { text: "hello" }'
    assert "PreStep" in repr(job)
    assert "hi" in repr(session.cursor(cursor).items[0])
    assert "System" in repr(session.tree.walk(session.cursor(cursor).at)[-1])
    assert "Ok" in repr(Outcome.Ok("fine"))
    assert "Frontend" in repr(Draft(Body.User("x"), Author.Frontend("term"), [1]))
    assert "Halted" in repr(Ending.Halted("why"))
    assert "HookFailed" in repr(Answer.HookFailed("oops"))
