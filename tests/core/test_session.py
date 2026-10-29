"""The session through the public Python API: create, drive by hand, reload."""

from pathlib import Path

import pytest
from drive import call, run, say

import tau_next
from tau_next import (
    Author,
    Body,
    Config,
    Draft,
    Ending,
    Fed,
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
    return [n.body for n in session.walk(session.at(cursor))]


def state(session: Session) -> dict[str, object]:
    cursors = session.cursors()
    return {
        "cursors": cursors,
        "at": {c: session.at(c) for c in cursors},
        "walks": {c: session.walk(session.at(c)) for c in cursors},
        "items": {c: session.items(c) for c in cursors},
        "labels": {c: session.labels_of(c) for c in cursors},
        "scratchpad": session.scratchpad(),
    }


# Create


def test_create_in_memory_has_a_config_root_a_system_node_and_one_cursor() -> None:
    session, cursor = new()
    assert session.cursors() == [0]
    root, system = session.walk(session.at(cursor))
    assert root.parent is None
    assert root.body == Body.Config(Config("{}"))
    assert system.parent == root.id
    assert system.body == Body.System("sys")
    assert session.config_at(system.id) == Config("{}")
    assert session.next(cursor) == Next.Wait
    assert not session.auto(cursor)
    assert not session.busy(cursor)
    assert session.dropped() == 0


def test_create_in_a_directory_is_loadable_at_once(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    before = state(session)
    session.close()
    assert state(Session.load(tmp_path / "s", 0)) == before


def test_create_refuses_a_directory_that_holds_a_session(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    session.close()
    with pytest.raises(tau_next.Exists):
        new(tmp_path / "s")


# A plain turn


def test_a_plain_turn_reaches_rest_with_a_reply() -> None:
    session, cursor = new()
    item = session.send(cursor, "term", "hi", 5)
    assert session.busy(cursor)
    assert session.auto(cursor)
    assert session.items(cursor)[0].data == ItemData.Input("term", "hi")
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
    assert not session.busy(cursor)
    _, _, user, reply = session.walk(session.at(cursor))
    assert user.body == Body.User("hi")
    assert user.by == Author.Frontend("term")
    assert user.delivers == [item]
    assert user.time == 6
    assert reply.body == Body.Assistant([Part.Text("hello")], Stop.End)
    assert reply.by == Author.Provider()
    assert session.items(cursor) == []
    assert session.next(cursor) == Next.Wait


def test_the_input_draft_reaches_pre_input_and_an_edit_is_committed() -> None:
    session, cursor = new()
    session.send(cursor, "term", "hi", 0)

    def edit(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            assert job.draft is not None
            assert job.draft.body == Body.User("hi")
            edited = Draft(Body.User("HI"), job.draft.by, job.draft.delivers)
            return [Fed.Hooks(draft=edited)]
        return None

    run(session, cursor, iter([say("ok")]), on_job=edit)
    assert bodies(session, cursor)[2] == Body.User("HI")


def test_a_halting_pre_input_hook_removes_the_item_and_notifies_the_client() -> None:
    session, cursor = new()
    session.send(cursor, "term", "/nope", 0)

    def halt(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            return [Fed.Hooks(stop="no commands")]
        return None

    jobs = run(session, cursor, on_job=halt)
    last = [j for j in jobs if isinstance(j, Job.Hooks)][-1]
    assert last.point == Hook.PostStep
    assert last.ending == Ending.Halted("no commands")
    assert session.items(cursor) == []
    assert session.take_notices() == [("term", "no commands")]
    assert session.take_notices() == []
    assert not session.auto(cursor)


def test_a_failed_sample_ends_the_step_failed() -> None:
    session, cursor = new()
    session.send(cursor, "term", "hi", 0)
    failed = iter([Fed.Sampled(None, "boom")])
    jobs = run(session, cursor, failed)
    last = [j for j in jobs if isinstance(j, Job.Hooks)][-1]
    assert last.ending == Ending.Failed("boom")
    # A side node: written under the user node, the cursor stays where it was.
    side = session.node(session.at(cursor) + 1)
    assert side.parent == session.at(cursor)
    assert side.body == Body.Custom("sample_error", "boom")


# A tool batch


def test_a_tool_batch_is_answered_by_one_results_node_in_call_order() -> None:
    session, cursor = new()
    session.send(cursor, "term", "use tools", 0)
    samples = iter([call(("a", "read"), ("b", "write")), say("done")])
    jobs = run(session, cursor, samples)
    batches = [j for j in jobs if isinstance(j, Job.RunCalls)]
    assert [[c.id for c in b.calls] for b in batches] == [["a", "b"]]
    assert batches[0].calls[0].tool == "read"
    assert batches[0].calls[0].args_raw == "{}"
    _, _, _, assistant, results, final = session.walk(session.at(cursor))
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
    session.send(cursor, "term", "go", 0)
    samples = iter([call(("a", "t"), ("b", "t")), say("done")])

    def backwards(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.RunCalls):
            done: list[Fed] = [
                Fed.CallDone(c.id, Outcome.Err(c.id), Author.Extension("x")) for c in job.calls
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
    session.send(cursor, "term", "go", 0)
    samples = iter([call(("a", "t"))])
    cancelled: list[bool] = []

    def abort(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.RunCalls):
            cancelled.append(session.abort(cursor))
            return [Fed.CallDone("a", Outcome.Interrupted(), Author.Tools())]
        return None

    run(session, cursor, samples, on_job=abort)
    assert cancelled == [True]
    results = bodies(session, cursor)[-1]
    assert isinstance(results, Body.Results)
    assert results.outcomes[0][1] == Outcome.Interrupted()
    assert not session.auto(cursor)


# Cursors, items, labels, moves


def test_open_wakes_a_cursor_and_go_to_moves_it() -> None:
    session, first = new()
    system = session.at(first)
    other = session.open(system, Author.Extension("ext"), 3)
    assert session.cursors() == [first, other]
    assert session.busy(other)
    assert session.moves_of(other)[0].from_ is None
    run(session, other)
    session.send(first, "term", "hi", 0)
    run(session, first, iter([say("hey")]))
    assert session.go_to(other, session.at(first), "term", 4) is False
    run(session, other)
    assert session.at(other) == session.at(first)
    assert not session.auto(other)
    assert session.moves_of(other)[-1].by == Author.Frontend("term")
    assert session.lca(session.at(first), system) == system


def test_an_extension_item_wakes_its_cursor_and_stays_in_the_scratchpad() -> None:
    session, cursor = new()
    item = session.add_item(cursor, ItemData.Extension("timer", "tick"), 0)
    assert session.busy(cursor)
    run(session, cursor)
    assert [i.id for i in session.items(cursor)] == [item]
    assert session.scratchpad().items == session.items(cursor)
    assert session.scratchpad().next_item == item + 1


def test_labels_set_move_and_remove() -> None:
    session, first = new()
    other = session.open(session.at(first), Author.Core(), 0)
    session.set_label("main", first)
    session.set_label("side", first)
    assert session.labels_of(first) == ["main", "side"]
    session.set_label("main", other)
    assert session.label("main") == other
    assert session.labels_of(first) == ["side"]
    session.remove_label("side")
    assert session.labels_of(first) == []


def test_moves_since_step_lists_the_moves_the_step_made() -> None:
    session, cursor = new()
    session.send(cursor, "term", "hi", 0)
    seen: list[int] = []

    def watch(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PostReply:
            seen.extend(m.to for m in session.moves_since_step(cursor))
        return None

    run(session, cursor, iter([say("x")]), on_job=watch)
    user, reply = session.at(cursor) - 1, session.at(cursor)
    assert seen == [user, reply]


# Exceptions


def test_unknown_ids_raise_lookup_errors() -> None:
    session, cursor = new()
    with pytest.raises(tau_next.NoSuchNode):
        session.node(99)
    with pytest.raises(tau_next.NoSuchNode):
        session.go_to(cursor, 99, "term", 0)
    with pytest.raises(tau_next.NoSuchCursor):
        session.at(7)
    with pytest.raises(tau_next.NoSuchCursor):
        session.input(7, "term", "hi", 0)
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
    session.send(cursor, "term", "hi", 0)
    with pytest.raises(tau_next.Busy):
        session.step(cursor, "term", 0)


def test_a_go_to_while_a_cursor_hook_runs_aborts_and_moves_once_the_step_rests() -> None:
    session, cursor = new()
    system = session.at(cursor)
    session.send(cursor, "term", "hi", 0)
    assert isinstance(session.next_job(cursor), Job.Hooks)
    assert not session.go_to(cursor, 0, "term", 0), "nothing to cancel: the hook finishes"
    assert session.at(cursor) == system
    jobs = run(session, cursor)
    endings = [j.ending for j in jobs if isinstance(j, Job.Hooks) and j.ending is not None]
    assert endings[0] == Ending.Aborted()
    assert not any(isinstance(j, Job.Sample) for j in jobs)
    assert session.at(cursor) == 0


def test_a_results_draft_naming_a_call_twice_fails_the_step_and_changes_nothing() -> None:
    session, cursor = new()
    session.send(cursor, "term", "hi", 0)
    twice = [("a", Outcome.Ok("1"), Author.Tools()), ("a", Outcome.Ok("2"), Author.Tools())]
    bad = Draft(Body.Results(twice), Author.Core(), [])
    before = bodies(session, cursor)

    def corrupt(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PreInput:
            return [Fed.Hooks(draft=bad)]
        return None

    jobs = run(session, cursor, on_job=corrupt)
    endings = [j.ending for j in jobs if isinstance(j, Job.Hooks) and j.ending is not None]
    assert endings == [Ending.Failed("Results name a call twice")]
    assert bodies(session, cursor) == before, "the refused commit wrote nothing"
    assert [i.data for i in session.items(cursor)] == [ItemData.Input("term", "hi")]
    assert not session.auto(cursor)


def test_a_feed_that_answers_no_job_is_refused_and_the_job_stays_out() -> None:
    session, cursor = new()
    with pytest.raises(tau_next.WrongFeed):
        session.feed(cursor, Fed.Waited(), 0)
    session.send(cursor, "term", "hi", 0)
    assert isinstance(session.next_job(cursor), Job.Hooks)
    with pytest.raises(tau_next.WrongFeed):
        session.feed(cursor, say("x"), 0)
    session.feed(cursor, Fed.Hooks(), 0)  # the hook job was still out
    run(session, cursor, iter([say("x")]))


def test_abort_in_the_throttle_tells_the_driver_to_cancel_its_sleep() -> None:
    session, cursor = new()
    session.add_item(cursor, ItemData.Extension("timer", "tick"), 0)
    cancelled: list[bool] = []
    stray: list[bool] = []
    ticked = False

    def script(job: Job) -> list[Fed] | None:
        nonlocal ticked
        if isinstance(job, Job.Hooks) and job.point == Hook.PreStep and not ticked:
            ticked = True
            session.add_item(cursor, ItemData.Extension("timer", "tock"), 0)
        if isinstance(job, Job.Wait):
            cancelled.append(session.abort(cursor))
            try:
                session.feed(cursor, Fed.Waited(), 0)
            except tau_next.WrongFeed:
                stray.append(True)
            return []
        return None

    jobs = run(session, cursor, on_job=script)
    assert cancelled == [True]
    assert stray == [True]
    assert isinstance(jobs[-1], Job.Rest)


def test_a_closed_session_raises_closed() -> None:
    session, cursor = new()
    session.close()
    with pytest.raises(tau_next.Closed):
        session.at(cursor)
    with pytest.raises(tau_next.Closed):
        session.send(cursor, "term", "hi", 0)
    with pytest.raises(tau_next.Closed):
        session.close()


# Store errors


def test_a_second_open_of_a_session_is_leased(tmp_path: Path) -> None:
    session, _ = new(tmp_path / "s")
    with pytest.raises(tau_next.Leased):
        Session.load(tmp_path / "s", 0)
    session.close()
    Session.load(tmp_path / "s", 0)


def test_a_missing_session_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Session.load(tmp_path / "none", 0)


def test_files_that_are_not_a_session_are_refused(tmp_path: Path) -> None:
    (tmp_path / "log").write_bytes(b"not a tau log at all")
    with pytest.raises(tau_next.NotASession):
        Session.load(tmp_path, 0)


def test_a_future_file_version_is_refused(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    log = tmp_path / "log"
    data = bytearray(log.read_bytes())
    data[8:12] = (99).to_bytes(4, "little")
    log.write_bytes(data)
    with pytest.raises(tau_next.Version):
        Session.load(tmp_path, 0)


def test_a_damaged_scratchpad_file_is_corrupt(tmp_path: Path) -> None:
    session, _ = new(tmp_path)
    session.close()
    (tmp_path / "scratchpad").write_bytes(b"tau-pad\0" + (1).to_bytes(4, "little") + b"junk")
    with pytest.raises(tau_next.Corrupt):
        Session.load(tmp_path, 0)


# Persistence


def test_reopening_a_session_gives_the_state_before(tmp_path: Path) -> None:
    session, first = new(tmp_path / "s")
    session.send(first, "term", "hi", 1)
    run(session, first, iter([call(("a", "t")), say("done")]), 2)
    other = session.open(session.at(first) - 2, Author.Extension("ext"), 3)
    run(session, other)
    session.add_item(other, ItemData.Extension("timer", "tick"), 4)
    run(session, other)
    session.set_label("main", first)
    session.set_label("side", other)
    session.set_label("gone", other)
    session.remove_label("gone")
    before = state(session)
    assert len(before["cursors"]) == 2  # pyright: ignore[reportArgumentType]
    session.close()
    after = Session.load(tmp_path / "s", 5)
    assert state(after) == before
    assert after.dropped() == 0


def test_a_send_is_durable_on_return_and_a_loaded_cursor_has_auto_off(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    session.send(cursor, "term", "hi", 0)
    session.close()
    loaded = Session.load(tmp_path / "s", 1)
    assert [i.data for i in loaded.items(cursor)] == [ItemData.Input("term", "hi")]
    assert not loaded.auto(cursor)


def test_load_wakes_a_cursor_with_input_pending_for_pre_step_and_post_step_only(
    tmp_path: Path,
) -> None:
    session, cursor = new(tmp_path / "s")
    session.send(cursor, "term", "hi", 0)  # auto-step on, durable on return
    session.close()  # before the step ran
    loaded = Session.load(tmp_path / "s", 1)
    assert loaded.busy(cursor)
    assert not loaded.auto(cursor)
    jobs = run(loaded, cursor)
    assert [j.point for j in jobs if isinstance(j, Job.Hooks)] == [Hook.PreStep, Hook.PostStep]
    assert not any(isinstance(j, Job.Sample) for j in jobs)
    assert [i.data for i in loaded.items(cursor)] == [ItemData.Input("term", "hi")]
    assert len(loaded.walk(loaded.at(cursor))) == 2  # no User node, no reply


def test_close_drops_what_was_not_synced_as_a_crash_would(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    session.send(cursor, "term", "hi", 0)
    before = state(session)
    committed: list[Job] = []

    def crash(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Sample):
            committed.append(job)
            session.close()
        return None

    with pytest.raises(tau_next.Closed):
        run(session, cursor, iter([say("x")]), on_job=crash)
    # The input was committed in memory, but not synced: it is still an item.
    loaded = Session.load(tmp_path / "s", 0)
    assert state(loaded) == before


def test_post_hooks_see_synced_data(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    session.send(cursor, "term", "hi", 0)
    on_disk: list[int] = []

    def peek(job: Job) -> list[Fed] | None:
        if isinstance(job, Job.Hooks) and job.point == Hook.PostReply:
            copy = tmp_path / "copy"
            copy.mkdir(exist_ok=True)
            for name in ("log", "scratchpad"):
                if (tmp_path / "s" / name).exists():
                    (copy / name).write_bytes((tmp_path / "s" / name).read_bytes())
            on_disk.append(len(Session.load(copy, 0).walk(session.at(cursor))))
        return None

    run(session, cursor, iter([say("x")]), on_job=peek)
    assert on_disk == [4]


# Syncs, store failure, hook edits, cursor checks, repr


def test_open_and_add_item_outside_a_step_are_durable_on_return(tmp_path: Path) -> None:
    session, cursor = new(tmp_path / "s")
    other = session.open(session.at(cursor), Author.Extension("ext"), 1)
    item = session.add_item(cursor, ItemData.Extension("timer", "tick"), 2)
    session.close()
    loaded = Session.load(tmp_path / "s", 3)
    assert loaded.cursors() == [cursor, other]
    assert [(i.id, i.data) for i in loaded.items(cursor)] == [
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
        session.send(cursor, "term", "hi", 1)
    # Closed, as `close` leaves it: every call raises `Closed`, reads included.
    for call_ in (
        lambda: session.at(cursor),
        lambda: session.items(cursor),
        lambda: session.abort(cursor),
        lambda: session.next_job(cursor),
        session.close,
    ):
        with pytest.raises(tau_next.Closed):
            call_()
    block.rmdir()
    loaded = Session.load(tmp_path / "s", 2)  # the lease was released
    assert state(loaded) == synced
    assert not loaded.auto(cursor)


def edit_input_without_delivering(job: Job) -> list[Fed] | None:
    if isinstance(job, Job.Hooks) and job.point == Hook.PreInput and job.draft:
        edited = Draft(Body.User("edited"), job.draft.by, [])
        return [Fed.Hooks(None, edited)]
    return None


def test_a_draft_must_state_what_it_delivers() -> None:
    with pytest.raises(TypeError):
        Draft(Body.User("x"), Author.Core())  # pyright: ignore[reportCallIssue]


def test_a_pre_input_draft_that_delivers_nothing_leaves_the_input_pending() -> None:
    session = Session.create(Config("{}", 3), "sys", 0)
    cursor = session.cursors()[0]
    session.send(cursor, "term", "hi", 0)
    jobs = run(
        session, cursor, iter(say("ok") for _ in range(10)), on_job=edit_input_without_delivering
    )
    assert sum(isinstance(j, Job.Sample) for j in jobs) == 3  # one per step until max_steps
    users = [b for b in bodies(session, cursor) if isinstance(b, Body.User)]
    assert users == [Body.User("edited")] * 3
    assert not session.auto(cursor)
    assert [i.data for i in session.items(cursor)] == [ItemData.Input("term", "hi")]


def test_a_cursor_that_is_not_open_is_refused_and_leaves_no_record() -> None:
    session, cursor = new()
    before = state(session)
    for method in (session.abort, session.next_job):
        with pytest.raises(tau_next.NoSuchCursor):
            method(999)
    with pytest.raises(tau_next.NoSuchCursor):
        session.feed(999, Fed.Hooks(), 0)
    assert session.cursors() == [cursor]
    assert state(session) == before


def test_values_have_a_readable_repr() -> None:
    session, cursor = new()
    session.send(cursor, "term", "hi", 0)
    job = session.next_job(cursor)
    assert repr(Body.User("hello")) == 'User { text: "hello" }'
    assert "PreStep" in repr(job)
    assert "hi" in repr(session.items(cursor)[0])
    assert "System" in repr(session.walk(session.at(cursor))[-1])
    assert "Ok" in repr(Outcome.Ok("fine"))
    assert "Frontend" in repr(Draft(Body.User("x"), Author.Frontend("term"), [1]))
    assert "Halted" in repr(Ending.Halted("why"))
    assert "HookFailed" in repr(Fed.HookFailed("oops"))
