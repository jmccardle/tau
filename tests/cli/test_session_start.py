"""The shared start (D18 section 5): `start_session` resolves the session flags, changes into the
session's working directory, loads the config and extensions, and creates, opens or forks the
session. Through the public API on real files (the model is the mock provider)."""

import argparse
import asyncio
import json
import os
import re
import stat
from collections.abc import Callable, Coroutine, Iterator, Sequence
from pathlib import Path

import pytest
from world import World, tau, trust

from tau import (
    Author,
    Body,
    Client,
    CommandFailed,
    Config,
    ConfigError,
    CursorView,
    ExtensionLoadError,
    Labels,
    Leased,
    NoSuchNode,
    NotASession,
    Opened,
    Part,
    PickSession,
    RetryProvider,
    Session,
    SessionInfo,
    Started,
    Stop,
    UnknownSession,
    first_cursor_node,
    list_sessions,
    model_config,
    open_client,
    start_session,
)
from tau.cli import extension_flags, session_flags

OLD = "20261030-091502-1c2d3e"
NEW = "20261031-142233-7f3a9c"
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    """The shared start changes the process's working directory; give it back."""
    before = os.getcwd()
    yield
    os.chdir(before)


MINE = '''
name = "mine"

async def ping() -> str:
    """Pong."""
    return "pong"

commands = (ping,)
'''
SPY = '''
from tau import Cursor

name = "spy"

async def which_tools(cursor: Cursor) -> str:
    """Say the names of the driver's tools."""
    return ", ".join(cursor.tools())

commands = (which_tools,)
'''
BASE_TOOLS = "read, write, edit, grep, find, bash"
MOCK = {"default_model": "m", "models": {"m": {"model": "m", "provider": "mock"}}}


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    return parser.parse_args(list(argv))


def system(cwd: Path, loaded: object) -> str:
    return f"You work in {cwd}."


LOOP = asyncio.new_event_loop()
"""One loop for the module: a session is started in one `run` and stopped in another."""


@pytest.fixture(scope="module", autouse=True)
def close_loop() -> Iterator[None]:
    yield
    LOOP.close()


def run[T](coro: Coroutine[object, object, T]) -> T:
    return LOOP.run_until_complete(coro)


def start(
    world: World, *argv: str, cwd: Path | None = None, pick: PickSession | None = None
) -> Started:
    return run(
        start_session(
            parse(*argv),
            environ=world.environ,
            cwd=cwd or world.home,
            system=system,
            pick_session=pick,
        )
    )


def stop(*started: Started) -> None:
    for each in started:
        run(each.stop())


def sessions_of(world: World) -> list[str]:
    path = world.tau / "sessions"
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def rows(world: World) -> list[SessionInfo]:
    return list_sessions(world.tau / "sessions")


def used(world: World, id: str, *, cwd: Path, age: float = 0) -> Path:
    return world.session(id, cwd=cwd, says=["hello"], age=age)


# -- a new session


def test_a_new_session_is_a_directory_named_by_its_id_with_info_json(configured: World) -> None:
    started = start(configured)
    try:
        path = started.directory
        assert path is not None
        assert path.parent == configured.tau / "sessions"
        assert ID.fullmatch(path.name)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        info = json.loads((path / "info.json").read_text(encoding="utf-8"))
        assert info == {"cwd": str(configured.home.resolve())}
        assert sorted(p.name for p in path.iterdir()) == ["info.json", "log"]
    finally:
        stop(started)


def test_a_new_sessions_system_comes_from_the_seam_called_with_its_working_directory(
    configured: World,
) -> None:
    work = configured.home / "work"
    work.mkdir()
    started = start(configured, cwd=work)
    try:
        root, node = started.session.tree.walk(started.session.cursor(0).at)
        assert node.body == Body.System(f"You work in {work.resolve()}.")
        assert root.body == Body.Config(model_config("m"))
    finally:
        stop(started)


def test_the_seam_is_not_called_for_an_existing_session_or_a_fork(configured: World) -> None:
    used(configured, OLD, cwd=configured.home)
    seen: list[Path] = []

    def watching(cwd: Path, loaded: object) -> str:
        seen.append(cwd)
        return "not used"

    for flags in (["--session", OLD], ["--fork", OLD]):
        started = run(
            start_session(
                parse(*flags), environ=configured.environ, cwd=configured.home, system=watching
            )
        )
        stop(started)
    assert seen == []


def test_the_process_changes_into_the_sessions_working_directory(configured: World) -> None:
    work = configured.home / "work"
    work.mkdir()
    started = start(configured, cwd=work)
    try:
        assert Path.cwd() == work.resolve()
        assert started.cwd == work.resolve()
    finally:
        stop(started)


def test_a_new_session_is_its_directorys_resolved_path(configured: World) -> None:
    real = configured.home / "real"
    real.mkdir()
    link = configured.home / "link"
    link.symlink_to(real)
    started = start(configured, cwd=link)
    try:
        info = json.loads((started.directory / "info.json").read_text(encoding="utf-8"))  # type: ignore[operator]
        assert info["cwd"] == str(real.resolve())
    finally:
        stop(started)


def test_no_session_keeps_the_session_in_memory(configured: World) -> None:
    started = start(configured, "--no-session")
    try:
        assert started.directory is None
        assert sessions_of(configured) == []
        assert started.session.cursors() == [0]
    finally:
        stop(started)
    assert sessions_of(configured) == []


@pytest.mark.parametrize(
    "flags",
    [
        ["-c", "--fork", "x"],
        ["-r", "--session", "x"],
        ["--session", "x", "--fork", "y"],
        ["--no-session", "-c"],
        ["--no-session", "--session-dir", "x"],
        ["--no-session", "-n", "name"],
    ],
)
def test_flags_that_each_choose_the_session_cannot_be_combined(
    configured: World, flags: list[str]
) -> None:
    with pytest.raises(ConfigError, match=r"cannot be combined|keeps nothing"):
        start(configured, *flags)
    assert sessions_of(configured) == []


def test_n_names_a_new_session(configured: World) -> None:
    started = start(configured, "-n", "the store rewrite")
    try:
        assert [r.name for r in rows(configured)] == ["the store rewrite"]
    finally:
        stop(started)


def test_session_dir_moves_where_sessions_live(configured: World) -> None:
    here = configured.home / "here"
    here.mkdir()
    started = start(configured, "--session-dir", "kept", cwd=here)
    try:
        assert started.directory is not None
        assert started.directory.parent == here / "kept"
        assert sessions_of(configured) == []
    finally:
        stop(started)


# -- a session that was never used


def test_a_session_nobody_used_is_deleted_at_exit(configured: World) -> None:
    started = start(configured)
    assert sessions_of(configured) != []
    stop(started)
    assert sessions_of(configured) == []


def test_a_session_with_a_message_stays(configured: World) -> None:
    started = start(configured)
    client = started.driver.client("t")

    async def say() -> None:
        await client.input(0, "hello")
        await client.close()

    run(say())
    stop(started)
    assert len(sessions_of(configured)) == 1


def test_an_unused_fork_goes_but_an_opened_session_never_does(configured: World) -> None:
    used(configured, OLD, cwd=configured.home)
    opened = start(configured, "--session", OLD)
    stop(opened)
    assert sessions_of(configured) == [OLD]
    forked = start(configured, "--fork", OLD)
    stop(forked)
    assert sessions_of(configured) == [OLD]


# -- continuing, resuming, opening


def test_continue_opens_the_newest_session_of_this_directory(configured: World) -> None:
    here, there = configured.home / "here", configured.home / "there"
    here.mkdir()
    there.mkdir()
    used(configured, OLD, cwd=here, age=500)
    used(configured, NEW, cwd=here, age=5)
    used(configured, "20261101-000000-bbbbbb", cwd=there, age=1)
    started = start(configured, "-c", cwd=here)
    try:
        assert started.directory == configured.tau / "sessions" / NEW
        assert not started.created
        assert Path.cwd() == here.resolve()
    finally:
        stop(started)


def test_continue_with_none_names_the_directory_and_the_sessions_directory(
    configured: World,
) -> None:
    used(configured, OLD, cwd=configured.home / "elsewhere")
    with pytest.raises(UnknownSession) as stopped:
        start(configured, "-c")
    assert str(configured.home.resolve()) in str(stopped.value)
    assert str(configured.tau / "sessions") in str(stopped.value)


def test_continue_does_not_pass_a_leased_session_to_an_older_one(configured: World) -> None:
    here = configured.home
    used(configured, OLD, cwd=here, age=500)
    path = used(configured, NEW, cwd=here, age=5)
    holder = Session.load(path)
    try:
        with pytest.raises(Leased) as stopped:
            start(configured, "-c")
        message = str(stopped.value)
        assert NEW in message
        assert f"tau --fork {NEW}" in message
    finally:
        holder.close()


def test_a_leased_session_is_an_error_for_session_too(configured: World) -> None:
    holder = Session.load(used(configured, OLD, cwd=configured.home))
    try:
        with pytest.raises(Leased, match=OLD):
            start(configured, "--session", OLD)
    finally:
        holder.close()


def test_session_takes_a_path_an_id_a_name_or_a_prefix(configured: World) -> None:
    path = configured.session(OLD, cwd=configured.home, name="rewrite", says=["hello"])
    for ref in (str(path), OLD, "rewrite", "20261030"):
        started = start(configured, "--session", ref)
        try:
            assert started.directory == path
        finally:
            stop(started)


def test_session_takes_no_node_and_says_to_fork(configured: World) -> None:
    used(configured, OLD, cwd=configured.home)
    with pytest.raises(ConfigError, match=f"--fork {OLD}:2"):
        start(configured, "--session", f"{OLD}:2")


def test_an_unknown_session_is_unknown_session(configured: World) -> None:
    with pytest.raises(UnknownSession, match="no session 'nothing'"):
        start(configured, "--session", "nothing")
    assert sessions_of(configured) == []


def test_n_renames_an_opened_session(configured: World) -> None:
    used(configured, OLD, cwd=configured.home)
    started = start(configured, "--session", OLD, "-n", "renamed")
    try:
        assert [r.name for r in rows(configured)] == ["renamed"]
    finally:
        stop(started)


def test_resume_asks_the_picker_with_this_directorys_rows_and_all_of_them(
    configured: World,
) -> None:
    here, there = configured.home / "here", configured.home / "there"
    here.mkdir()
    there.mkdir()
    used(configured, OLD, cwd=here, age=10)
    used(configured, NEW, cwd=there, age=5)
    asked: list[tuple[list[str], list[str]]] = []

    def pick(nearby: Sequence[SessionInfo], everywhere: Sequence[SessionInfo]) -> SessionInfo:
        asked.append(([r.id for r in nearby], [r.id for r in everywhere]))
        return everywhere[0]

    started = start(configured, "-r", cwd=here, pick=pick)
    try:
        assert asked == [([OLD], [NEW, OLD])]
        assert started.directory == configured.tau / "sessions" / NEW
        assert Path.cwd() == there.resolve(), "the chosen session's directory, not the typed one"
    finally:
        stop(started)


def test_resume_without_a_picker_is_an_error(configured: World) -> None:
    used(configured, OLD, cwd=configured.home)
    with pytest.raises(ConfigError, match="-r needs a terminal"):
        start(configured, "-r")


def test_a_session_whose_working_directory_is_gone_cannot_open_but_names_it(
    configured: World,
) -> None:
    gone = configured.home / "gone"
    used(configured, OLD, cwd=gone)
    with pytest.raises(FileNotFoundError, match=f"{gone} no longer exists"):
        start(configured, "--session", OLD)
    assert [r.error for r in rows(configured)] == [None], "it is still a session"


def test_a_session_without_info_json_cannot_open(configured: World) -> None:
    path = used(configured, OLD, cwd=configured.home)
    (path / "info.json").unlink()
    with pytest.raises(NotASession, match=r"info\.json is missing"):
        start(configured, "--session", str(path))


# -- the order of the start: chdir first, then the config and the extensions


def test_the_config_comes_from_the_sessions_directory_not_the_one_typed_in(
    world: World,
) -> None:
    project = world.project("proj", config=MOCK)
    trust(world, project)
    made = world.session(OLD, cwd=project, says=["hello"])
    elsewhere = world.home / "elsewhere"
    elsewhere.mkdir()
    assert not (world.tau / "config.json").exists(), "only the project knows the model"
    started = start(world, "--session", str(made), cwd=elsewhere)
    try:
        assert started.model == "m"
        assert started.loaded.project is not None
        assert started.loaded.project.root == project.resolve()
    finally:
        stop(started)


def test_relative_paths_in_flags_mean_the_directory_they_were_typed_in(configured: World) -> None:
    session_cwd = configured.home / "session"
    session_cwd.mkdir()
    typed = configured.home / "typed"
    typed.mkdir()
    (typed / "mine.py").write_text(MINE, encoding="utf-8")
    made = configured.session(OLD, cwd=session_cwd, says=["hello"], sessions=typed / "kept")
    started = start(
        configured, "--session-dir", "kept", "--session", f"kept/{made.name}", "-e", "./mine.py",
        cwd=typed,
    )  # fmt: skip
    try:
        assert started.directory == made
        assert Path.cwd() == session_cwd.resolve()
        commands = run(started.driver.client("t").commands())
        assert "ping" in {c.name for c in commands}
    finally:
        stop(started)


def test_an_extension_that_fails_to_load_stops_the_start_before_anything_is_written(
    configured: World,
) -> None:
    with pytest.raises(ExtensionLoadError, match="no such file"):
        start(configured, "-e", "missing.py")
    assert sessions_of(configured) == []


def test_a_model_that_cannot_be_chosen_stops_the_start_before_anything_is_written(
    world: World,
) -> None:
    world.user_config({"models": MOCK["models"]})  # no default_model
    with pytest.raises(ConfigError, match="no model chosen"):
        start(world)
    assert sessions_of(world) == []


def test_no_extensions_does_not_remove_the_session_name_command(configured: World) -> None:
    started = start(configured, "--no-extensions")
    client = started.driver.client("t")

    async def name() -> str | None:
        try:
            return await client.run(0, "name", {"name": "still here"})
        finally:
            await client.close()

    try:
        assert run(name()) == "named 'still here'"
    finally:
        stop(started)


# -- /name


def test_name_command_shows_sets_and_removes_the_name_while_the_session_runs(
    configured: World,
) -> None:
    started = start(configured)
    client = started.driver.client("t")

    async def commands() -> list[str | None]:
        out = [await client.run(0, "name", {})]
        out.append(await client.run(0, "name", {"name": "the store rewrite"}))
        out.append(await client.run(0, "name", {}))
        listed = [r.name for r in rows(configured)]
        out.append(await client.run(0, "name", {"name": ""}))
        out.append(await client.run(0, "name", {}))
        await client.close()
        return [*out, str(listed)]

    try:
        assert run(commands()) == [
            "(no name)",
            "named 'the store rewrite'",
            "the store rewrite",
            "name removed",
            "(no name)",
            "['the store rewrite']",
        ]
    finally:
        stop(started)


def test_a_session_in_memory_has_no_name_command(configured: World) -> None:
    started = start(configured, "--no-session")
    names = {c.name for c in run(started.driver.client("t").commands())}
    stop(started)
    assert "name" not in names
    assert "model" in names


# -- fork


def source_with_branches(world: World, cwd: Path) -> Path:
    """OLD: System (1), "one" (2), a reply (3), "other" (4, a branch from 2) and "two" (5, after
    the reply), cursor 0 on "two"."""

    def build(session: Session, tip: int) -> int:
        one = session.append(tip, Body.User("one"), Author.Frontend("t"), 1)
        reply = session.append(
            one, Body.Assistant([Part.Text("ok")], Stop.End, "stop"), Author.Provider(), 2
        )
        session.append(one, Body.User("other"), Author.Frontend("t"), 3)
        return session.append(reply, Body.User("two"), Author.Frontend("t"), 4)

    return world.session(OLD, cwd=cwd, build=build)


def bodies(session: Session, node: int) -> list[Body]:
    return [n.body for n in session.tree.walk(node)]


def test_fork_copies_the_first_cursors_walk_into_a_new_session_that_records_its_source(
    configured: World,
) -> None:
    source = source_with_branches(configured, configured.home)
    wanted = bodies(Session.read(source), 5)
    started = start(configured, "--fork", OLD, "-n", "a copy")
    try:
        assert started.created
        assert started.directory not in (None, source)
        assert bodies(started.session, 4) == wanted
        assert len(started.session.tree) == 5, "the other branch is not copied"
        assert started.session.cursors() == [0]
        assert started.session.cursor(0).at == 4
        assert started.directory is not None
        info = json.loads((started.directory / "info.json").read_text(encoding="utf-8"))
        assert info == {
            "cwd": str(configured.home),
            "name": "a copy",
            "forked_from": {"session": OLD, "node": 5},
        }
        assert len(Session.read(source).tree) == 6, "the source is as it was"
    finally:
        stop(started)


def test_a_session_whose_newest_config_only_sets_options_reopens_on_the_model_before_it(
    configured: World,
) -> None:
    def build(session: Session, tip: int) -> int:
        options = Body.Config(model_config(extensions={"step_limit": {"limit": 3}}))
        return session.append(tip, options, Author.Frontend("t"), 2)

    configured.session(OLD, cwd=configured.home, says=["hello"], build=build)
    for argv in (("-c",), ("--fork", OLD)):
        started = start(configured, *argv)
        try:
            assert started.model == "m"
        finally:
            stop(started)


def test_a_ref_node_forks_that_walk(configured: World) -> None:
    source = source_with_branches(configured, configured.home)
    wanted = bodies(Session.read(source), 4)
    started = start(configured, "--fork", f"{OLD}:4", "-n", "x")
    try:
        assert bodies(started.session, 3) == wanted
        assert started.directory is not None
        info = json.loads((started.directory / "info.json").read_text(encoding="utf-8"))
        assert info["forked_from"] == {"session": OLD, "node": 4}
    finally:
        stop(started)


def test_a_fork_inherits_the_sources_working_directory(configured: World) -> None:
    there = configured.home / "there"
    there.mkdir()
    source_with_branches(configured, there)
    started = start(configured, "--fork", OLD)
    try:
        assert Path.cwd() == there.resolve()
        assert started.cwd == there.resolve()
    finally:
        stop(started)


def test_a_fork_of_a_session_another_process_holds_works_and_leaves_it_alone(
    configured: World,
) -> None:
    path = source_with_branches(configured, configured.home)
    holder = Session.load(path)
    try:
        started = start(configured, "--fork", OLD)
        assert started.directory != path
        stop(started)
        assert len(holder.tree) == 6
    finally:
        holder.close()


def test_a_fork_at_a_node_that_is_not_there_creates_nothing(configured: World) -> None:
    source_with_branches(configured, configured.home)
    with pytest.raises(NoSuchNode):
        start(configured, "--fork", f"{OLD}:99")
    assert sessions_of(configured) == [OLD]


def test_a_fork_never_modified_leaves_no_trace_but_a_modified_one_is_listed(
    configured: World,
) -> None:
    source_with_branches(configured, configured.home)
    started = start(configured, "--fork", OLD)
    client = started.driver.client("t")

    async def say() -> None:
        await client.input(0, "and one more")
        await client.close()

    run(say())
    stop(started)
    assert len(sessions_of(configured)) == 2
    (copy,) = [r for r in rows(configured) if r.id != OLD]
    assert copy.forked_from is not None
    assert copy.forked_from.session == OLD


# -- the base tools: a new session offers its model the tools of the built-in `base_tools`
# extension; the config's `load` and `--no-extensions` turn them off


def offered(world: World, *argv: str) -> str:
    spy = world.home / "spy.py"
    spy.write_text(SPY, encoding="utf-8")
    started = start(world, "--no-session", "-e", str(spy), *argv)
    try:
        return run(started.driver.client("t").run(0, "which_tools", {})) or ""
    finally:
        stop(started)


def test_a_started_session_offers_the_base_tools(configured: World) -> None:
    assert offered(configured) == BASE_TOOLS


def test_load_false_in_the_config_starts_a_session_without_them(configured: World) -> None:
    configured.user_config({**MOCK, "load": {"base_tools": False}})
    assert offered(configured) == ""


def test_no_extensions_leaves_them_offered(configured: World) -> None:
    assert offered(configured, "--no-extensions") == BASE_TOOLS


def test_load_false_beats_no_extensions_for_them(configured: World) -> None:
    configured.user_config({**MOCK, "load": {"base_tools": False}})
    assert offered(configured, "--no-extensions") == ""


# -- the first cursor


def prepared(world: World, work: Callable[[Client], Coroutine[object, object, None]]) -> None:
    """The session `OLD`, with what `work` does to it through a client of its running driver."""
    world.session(OLD, cwd=world.home)
    started = start(world, "--session", OLD)
    client = started.driver.client("t")
    try:
        run(work(client))
    finally:
        run(client.close())
        stop(started)


def opened_session(world: World, *argv: str, session: str = OLD) -> tuple[Opened, Session]:
    """`open_client` on the session `session`, and the session it started."""
    found = run(
        open_client(
            parse("--session", session, *argv),
            client="t",
            environ=world.environ,
            cwd=world.home,
            system=system,
        )
    )
    assert found.started is not None
    return found, found.started.session


def test_the_first_cursor_is_the_lowest_open_one(configured: World) -> None:
    async def branch(client: Client) -> None:
        await client.open(0)
        await client.open(0)
        await client.close_cursor(0)

    prepared(configured, branch)
    found, session = opened_session(configured)
    try:
        assert session.cursors() == [1, 2]
        assert found.cursor == 1
        assert first_cursor_node(session) == found.started.session.cursor(1).at  # type: ignore[union-attr]
        assert isinstance(found.snapshot[0], CursorView)
        assert isinstance(found.snapshot[-1], Labels), "every event up to the first Labels"
        assert found.welcome is not None
        assert (found.welcome.session, found.welcome.pid) == (OLD, os.getpid())
        assert found.welcome.cwd == str(configured.home.resolve())
    finally:
        run(found.close())


def test_with_every_cursor_closed_the_lowest_is_reopened_with_its_labels(
    configured: World,
) -> None:
    async def close(client: Client) -> None:
        await client.set_label(0, "main")
        await client.close_cursor(0)

    prepared(configured, close)
    found, session = opened_session(configured)
    try:
        assert found.cursor == 1, "a new cursor with the closed one's last node and labels"
        assert session.cursor(found.cursor).labels == ["main"]
        assert session.cursor(found.cursor).at == first_cursor_node(session)
        views = [e for e in found.snapshot if isinstance(e, CursorView)]
        assert [v.cursor for v in views] == [1], "the reopened cursor's view is in the snapshot"
    finally:
        run(found.close())


def test_a_no_session_open_has_no_welcome(configured: World) -> None:
    found = run(
        open_client(
            parse("--no-session"), client="t", environ=configured.environ, cwd=configured.home
        )
    )
    try:
        assert found.welcome is None
        assert found.cursor == 0
    finally:
        run(found.close())
    assert sessions_of(configured) == []


def test_close_stops_the_start_and_removes_a_session_never_used(configured: World) -> None:
    found = run(open_client(parse(), client="t", environ=configured.environ, cwd=configured.home))
    assert len(sessions_of(configured)) == 1
    run(found.close())
    assert sessions_of(configured) == []


def test_the_start_report_inputs_are_kept(configured: World) -> None:
    started = start(configured, "--no-session")
    try:
        keys = [s.key for s in started.sources if s.state in ("on", "always")]
        assert {"base_tools", "step_limit", "session_name"} <= set(keys)
        assert started.prompt is None, "this program supplied its own system text"
    finally:
        stop(started)


def test_a_fork_whose_source_has_no_open_cursor_copies_the_closed_ones_last_node(
    configured: World,
) -> None:
    source_with_branches(configured, configured.home)
    opened = start(configured, "--session", OLD)
    client = opened.driver.client("t")
    run(client.close_cursor(0))
    run(client.close())
    stop(opened)
    started = start(configured, "--fork", OLD)
    try:
        assert started.session.cursor(0).at == 4, "walk(5) copied: five nodes, ids 0-4"
        info = json.loads((started.directory / "info.json").read_text(encoding="utf-8"))  # type: ignore[union-attr]
        assert info["forked_from"]["node"] == 5
    finally:
        stop(started)


def test_model_config_names_the_model_of_a_new_sessions_root(configured: World) -> None:
    started = start(configured, "--no-session")
    try:
        assert started.session.tree.node(0).body == Body.Config(model_config("m"))
    finally:
        stop(started)


# -- the model of a session that already has one


def test_continuing_and_forking_use_the_recorded_model_with_no_default_model(
    world: World,
) -> None:
    world.user_config({"models": MOCK["models"]})  # no default_model, and no --model
    world.session(OLD, cwd=world.home, says=["hello"])  # its root Config names model "m"
    for flags in (["-c"], ["--session", OLD], ["--fork", OLD]):
        started = start(world, *flags)
        try:
            assert started.model == "m"
        finally:
            stop(started)


def test_model_is_refused_for_a_continued_or_forked_session_naming_slash_model(
    configured: World,
) -> None:
    used(configured, OLD, cwd=configured.home)
    for flags in (["-c"], ["--session", OLD], ["--fork", OLD]):
        with pytest.raises(ConfigError, match="/model"):
            run(
                start_session(
                    parse(*flags),
                    environ=configured.environ,
                    cwd=configured.home,
                    system=system,
                    model="m",
                )
            )
    assert sessions_of(configured) == [OLD], "nothing was created"


# -- a failure after the session exists leaves nothing: no directory, no lease


def predating_d13(world: World) -> Path:
    """OLD, whose `Config` node names no model (a session from before D13)."""
    path = world.tau / "sessions" / OLD
    path.mkdir(parents=True, mode=0o700)
    (path / "info.json").write_text(json.dumps({"cwd": str(world.home)}), encoding="utf-8")
    session = Session.create(Config('{"note": "pre-D13"}'), "sys", 0, path)
    session.append(session.cursor(0).at, Body.User("hello"), Author.Frontend("t"), 0)
    session.go_to(0, 2, "t", 0)
    session.close()
    return path


def test_a_fork_whose_source_records_no_model_is_refused_before_a_directory_is_made(
    configured: World,
) -> None:
    predating_d13(configured)
    with pytest.raises(ConfigError):
        start(configured, "--fork", OLD)
    assert sessions_of(configured) == [OLD]


def test_an_opened_session_whose_config_cannot_be_read_gives_its_lease_back(
    configured: World,
) -> None:
    path = predating_d13(configured)
    with pytest.raises(ConfigError):
        start(configured, "--session", OLD)
    Session.load(path).close()  # Leased if the failed start kept it


def test_a_session_is_leased_before_any_extension_is_imported(configured: World) -> None:
    marker = configured.home / "imported"
    drop_ins = configured.tau / "extensions"
    drop_ins.mkdir(mode=0o700)
    (drop_ins / "side.py").write_text(
        f"open({str(marker)!r}, 'a').write('x')\nname = 'side'\n", encoding="utf-8"
    )
    (drop_ins / "side.py").chmod(0o600)
    holder = Session.load(used(configured, OLD, cwd=configured.home))
    try:
        with pytest.raises(Leased):
            start(configured, "-c")
    finally:
        holder.close()
    assert not marker.exists(), "the drop-in's import side effects ran before Leased was said"


def test_a_start_that_fails_after_the_lease_gives_it_back_and_does_not_rename(
    configured: World,
) -> None:
    path = used(configured, OLD, cwd=configured.home)
    with pytest.raises(ExtensionLoadError):
        start(configured, "--session", OLD, "-n", "never", "-e", "missing.py")
    Session.load(path).close()
    assert [r.name for r in rows(configured)] == [None]


# -- a program's own system text and the prompt flags


@pytest.mark.parametrize(
    "flags",
    [["--system-prompt", "x"], ["--append-system-prompt", "x"], ["-nc"]],
)
def test_a_program_with_its_own_system_text_refuses_the_flags_that_shape_the_assembly(
    configured: World, flags: list[str]
) -> None:
    with pytest.raises(ConfigError, match="its own system"):
        start(configured, *flags)
    assert sessions_of(configured) == []


# -- names


def test_a_new_sessions_and_a_forks_name_is_stored_stripped(configured: World) -> None:
    source_with_branches(configured, configured.home)
    fresh = start(configured, "-n", "  foo  ")
    forked = start(configured, "--fork", OLD, "-n", "  bar  ")
    try:
        assert sorted(r.name or "" for r in rows(configured)) == ["", "bar", "foo"]
    finally:
        stop(fresh, forked)


@pytest.mark.parametrize("flags", [["-n", "  "], ["--fork", OLD, "-n", ""]])
def test_an_empty_name_is_refused_for_a_new_session_and_a_fork(
    configured: World, flags: list[str]
) -> None:
    source_with_branches(configured, configured.home)
    with pytest.raises(ConfigError, match="empty"):
        start(configured, *flags)
    assert sessions_of(configured) == [OLD]


@pytest.mark.parametrize("name", ["a/b", "v:2"])
def test_a_name_that_cannot_be_a_ref_is_refused_everywhere_a_name_is_set(
    configured: World, name: str
) -> None:
    used(configured, OLD, cwd=configured.home)
    with pytest.raises(ConfigError, match=r"path|:NODE"):
        start(configured, "-n", name)
    with pytest.raises(ConfigError, match=r"path|:NODE"):
        start(configured, "--session", OLD, "-n", name)
    assert sessions_of(configured) == [OLD]
    started = start(configured)
    client = started.driver.client("t")

    async def rename() -> None:
        try:
            await client.run(0, "name", {"name": name})
        finally:
            await client.close()

    try:
        with pytest.raises(CommandFailed, match=r"path|:NODE"):
            run(rename())
    finally:
        stop(started)
    result = tau(configured, "sessions", "name", OLD, name)
    assert result.code == 1
    assert "path" in result.err
    assert [r.name for r in rows(configured)] == [None]


def test_a_session_named_while_it_runs_is_not_deleted_as_unused(configured: World) -> None:
    started = start(configured)
    client = started.driver.client("t")

    async def name() -> None:
        await client.run(0, "name", {"name": "keep me"})
        await client.close()

    run(name())
    stop(started)
    assert [r.name for r in rows(configured)] == ["keep me"]


# -- retry (D24 section 4): a switch in `load`, not an extension


def test_every_provider_a_session_starts_with_retries(configured: World) -> None:
    started = start(configured)
    try:
        assert started.providers
        assert all(isinstance(p, RetryProvider) for p in started.providers.values())
        assert sorted(started.providers) == ["anthropic", "gemini", "mock", "openai"]
    finally:
        stop(started)


def test_no_extensions_does_not_turn_retry_off(configured: World) -> None:
    started = start(configured, "--no-extensions")
    try:
        assert all(isinstance(p, RetryProvider) for p in started.providers.values())
    finally:
        stop(started)


def test_load_retry_false_leaves_the_providers_as_they_are(configured: World) -> None:
    configured.user_config({**MOCK, "load": {"retry": False}})
    started = start(configured)
    try:
        assert not any(isinstance(p, RetryProvider) for p in started.providers.values())
    finally:
        stop(started)


def test_tau_ext_disable_retry_is_that_switch(configured: World) -> None:
    listed = tau(configured, "ext", "list", "--json").out
    row = next(r for r in map(json.loads, listed.splitlines()) if r["key"] == "retry")
    assert (row["state"], row["kind"], row["extensions"]) == ("on", "built-in", [])
    assert tau(configured, "ext", "disable", "retry").code == 0
    started = start(configured)
    try:
        assert not any(isinstance(p, RetryProvider) for p in started.providers.values())
    finally:
        stop(started)


# -- attachments and skills (D24 sections 5 and 6) in a session the shared start opens


def users_of(started: Started) -> list[str]:
    """The text of every `User` node of the session, oldest first."""
    tree = started.session.tree
    nodes = (tree.node(i) for i in range(len(tree)))
    return [n.body.text for n in nodes if isinstance(n.body, Body.User)]


def said(started: Started, line: str) -> str:
    """The `User` node `line` became after the session answered it."""
    client = started.driver.client("t")

    async def go() -> None:
        await client.send(0, line)
        await started.driver.rest(0)
        await client.close()

    run(go())
    return users_of(started)[-1]


def test_at_file_means_the_sessions_working_directory(configured: World) -> None:
    work = configured.home / "work"
    work.mkdir()
    (work / "notes.txt").write_text("remember", encoding="utf-8")
    started = start(configured, "--no-session", cwd=work)
    try:
        assert said(started, "read @notes.txt") == (
            'read @notes.txt\n\n<attachment path="notes.txt">\nremember\n</attachment>'
        )
    finally:
        stop(started)


def test_load_false_turns_attachments_off(configured: World) -> None:
    work = configured.home / "work"
    work.mkdir()
    (work / "notes.txt").write_text("remember", encoding="utf-8")
    configured.user_config({**MOCK, "load": {"attachments": False}})
    started = start(configured, "--no-session", cwd=work)
    try:
        assert said(started, "read @notes.txt") == "read @notes.txt"
    finally:
        stop(started)


def test_a_template_in_tau_home_is_a_command_of_the_started_session(configured: World) -> None:
    prompts = configured.tau / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "shout.md").write_text("Say $1 loudly", encoding="utf-8")
    started = start(configured, "--no-session")
    client = started.driver.client("t")
    try:

        async def go() -> None:
            await client.run(0, "shout", {"args": "hello"})
            await started.driver.rest(0)
            await client.close()

        run(go())
        users = users_of(started)
        assert users == ["Say hello loudly"]
    finally:
        stop(started)


def test_a_file_that_cannot_be_used_stops_the_start_and_creates_nothing(
    configured: World,
) -> None:
    prompts = configured.tau / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "my notes.md").write_text("x", encoding="utf-8")
    with pytest.raises(ExtensionLoadError, match="the name 'my notes' is not ASCII letters"):
        start(configured)
    assert sessions_of(configured) == []


# -- options the layers set are checked at the start, once


@pytest.mark.parametrize(
    ("extension", "options", "problem"),
    [
        ("attachments", {"limit": 0}, "an attachment limit must be at least 1 byte"),
        ("step_limit", {"limit": 0}, "a step limit must be at least 1"),
        ("retry", {"x": 1}, "extension 'retry' has no options"),
        ("base_tools", {"x": 1}, "extension 'base_tools' has no options"),
    ],
)
def test_options_an_extension_refuses_stop_the_start_before_a_session_exists(
    configured: World, extension: str, options: dict[str, object], problem: str
) -> None:
    configured.user_config({**MOCK, "extensions": {extension: options}})
    with pytest.raises(ConfigError, match=f"extensions.{extension}: .*{problem}"):
        start(configured)
    assert sessions_of(configured) == []
    assert problem in tau(configured, "config", "check").out
