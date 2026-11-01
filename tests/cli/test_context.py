"""The system prompt (D20): assembled once, when a session is created, from the base text, the
appended sections and the context files, and recorded as the session's `System` node. Through
`tau.system_prompt`, the shared start and `tau context` on real files.

Discovery walks to `/`, so a test reads only what is inside its own temporary directory: the
assertions look at the sources under `tmp_path`, and a stray `/AGENTS.md` on the host does not
matter.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Coroutine, Iterator
from pathlib import Path

import pytest
from world import World, tau, trust

from tau import (
    Author,
    Body,
    ConfigError,
    ContextFileError,
    LoadedConfig,
    Session,
    Started,
    SystemPrompt,
    load_config,
    start_session,
    system_prompt,
)
from tau.cli import extension_flags, session_flags

MOCK = {"default_model": "m", "models": {"m": {"model": "m", "provider": "mock"}}}
BASE = "You are Tau, a coding agent."


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    """The shared start changes the process's working directory; give it back."""
    before = os.getcwd()
    yield
    os.chdir(before)


@pytest.fixture
def configured(world: World) -> World:
    world.user_config(MOCK)
    return world


def write(path: Path, text: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def loaded(world: World, cwd: Path, cli: dict[str, object] | None = None) -> LoadedConfig:
    return load_config(cwd, world.environ, ask=None, cli=cli)


def assemble(
    world: World,
    cwd: Path | None = None,
    *,
    cli: dict[str, object] | None = None,
    append: tuple[str, ...] = (),
    context_files: bool = True,
) -> SystemPrompt:
    here = cwd or world.home
    return system_prompt(here, loaded(world, here, cli), append=append, context_files=context_files)


def files_of(world: World, prompt: SystemPrompt) -> list[str]:
    """The context files the prompt took from inside the test's directory, relative to it, in
    order."""
    root = world.home.parent.resolve()
    return [
        str(Path(s.origin).relative_to(root))
        for s in prompt.sources
        if s.origin.startswith(str(root) + "/")
    ]


def in_order(text: str, *pieces: str) -> None:
    """Each piece is in `text`, one after the other."""
    at = -1
    for piece in pieces:
        found = text.find(piece, at + 1)
        assert found > at, f"{piece!r} is not after the one before it in {text!r}"
        at = found


# -- discovery


def test_the_ancestors_are_read_root_most_first_so_the_nearest_reads_last(world: World) -> None:
    root = world.home.parent
    write(root / "AGENTS.md", "top")
    write(world.home / "a" / "AGENTS.md", "middle")
    cwd = world.home / "a" / "b"
    write(cwd / "AGENTS.md", "nearest")
    prompt = assemble(world, cwd)
    assert files_of(world, prompt) == ["AGENTS.md", "home/a/AGENTS.md", "home/a/b/AGENTS.md"]
    in_order(prompt.text, "top", "middle", "nearest")


def test_each_file_is_wrapped_with_its_path_in_one_project_context_block(world: World) -> None:
    cwd = world.home / "a"
    write(cwd / "AGENTS.md", "first\n\n")
    write(world.home / "AGENTS.md", "second")
    prompt = assemble(world, cwd, context_files=True)
    assert prompt.text.count("<project_context>") == 1
    assert (
        f'<project_instructions path="{cwd.resolve()}/AGENTS.md">\nfirst\n</project' in prompt.text
    )
    in_order(
        prompt.text,
        "<project_context>",
        f'path="{world.home.resolve()}/AGENTS.md"',
        f'path="{cwd.resolve()}/AGENTS.md"',
        "</project_context>",
    )


def test_a_directory_gives_one_file_and_agents_wins_over_claude(world: World) -> None:
    write(world.home / "AGENTS.md", "agents")
    write(world.home / "CLAUDE.md", "claude")
    prompt = assemble(world)
    assert files_of(world, prompt) == ["home/AGENTS.md"]
    assert "claude" not in prompt.text


def test_claude_md_is_found_when_there_is_no_agents_md(world: World) -> None:
    write(world.home / "CLAUDE.md", "claude")
    assert files_of(world, assemble(world)) == ["home/CLAUDE.md"]


def test_other_spellings_and_the_override_file_are_not_context_files(world: World) -> None:
    for name in ("AGENTS.override.md", "AGENTS.MD", "CLAUDE.MD", "agents.md"):
        write(world.home / name, "no")
    assert files_of(world, assemble(world)) == []


def test_a_directory_with_a_context_files_name_is_skipped(world: World) -> None:
    (world.home / "AGENTS.md").mkdir()
    write(world.home / "CLAUDE.md", "claude")
    assert files_of(world, assemble(world)) == ["home/CLAUDE.md"]


def test_a_fifo_named_like_a_context_file_is_not_read(world: World) -> None:
    os.mkfifo(world.home / "AGENTS.md")
    write(world.home / "CLAUDE.md", "claude")
    assert files_of(world, assemble(world)) == ["home/CLAUDE.md"]


def test_the_tau_home_file_comes_first(world: World) -> None:
    write(world.tau / "AGENTS.md", "global")
    write(world.home / "AGENTS.md", "project")
    prompt = assemble(world)
    assert files_of(world, prompt) == ["home/.tau/AGENTS.md", "home/AGENTS.md"]
    in_order(prompt.text, "global", "project")


def test_a_file_reached_twice_is_read_once_at_its_first_slot(world: World) -> None:
    shared = write(world.home / "AGENTS.md", "shared")
    nested = world.home / "a"
    nested.mkdir()
    (nested / "AGENTS.md").symlink_to(shared)
    prompt = assemble(world, nested)
    assert files_of(world, prompt) == ["home/AGENTS.md"]
    assert prompt.text.count("shared") == 1


def test_the_tau_home_file_is_read_once_when_tau_home_is_an_ancestor(world: World) -> None:
    write(world.tau / "AGENTS.md", "global")
    prompt = assemble(world, world.tau)
    assert files_of(world, prompt) == ["home/.tau/AGENTS.md"]


def test_a_symlinked_working_directory_is_resolved_and_its_files_named_by_real_path(
    world: World,
) -> None:
    real = world.home / "real"
    write(real / "AGENTS.md", "real")
    (world.home / "link").symlink_to(real)
    prompt = assemble(world, world.home / "link")
    assert files_of(world, prompt) == ["home/real/AGENTS.md"]
    assert f"`{real.resolve()}`" in prompt.text


def test_a_symlinked_context_file_is_read_and_named_by_its_target(world: World) -> None:
    target = write(world.home / "notes" / "team.md", "team rules")
    (world.home / "AGENTS.md").symlink_to(target)
    prompt = assemble(world)
    assert files_of(world, prompt) == ["home/notes/team.md"]


# -- the project's SYSTEM.md


def project(world: World, system: str | bytes | None = "project rules") -> Path:
    root = world.project("proj", {})
    if system is not None:
        write(root / ".tau" / "SYSTEM.md", system)
    trust(world, root)
    return root


def test_the_projects_system_md_reads_last_from_a_directory_below_the_project(
    world: World,
) -> None:
    root = project(world)
    write(root / "AGENTS.md", "agents")
    cwd = root / "src"
    cwd.mkdir()
    prompt = assemble(world, cwd)
    assert files_of(world, prompt) == ["home/proj/AGENTS.md", "home/proj/.tau/SYSTEM.md"]
    in_order(prompt.text, "agents", "project rules")


def test_system_md_is_the_bytes_trust_approved_not_the_file_now(world: World) -> None:
    root = project(world)
    config = loaded(world, root)
    (root / ".tau" / "SYSTEM.md").write_text("edited since", encoding="utf-8")
    prompt = system_prompt(root, config)
    assert "project rules" in prompt.text
    assert "edited since" not in prompt.text


def test_a_symlinked_system_md_is_refused_when_trusting_the_project(world: World) -> None:
    root = world.project("proj", {})
    (root / "elsewhere.md").write_text("x", encoding="utf-8")
    (root / ".tau" / "SYSTEM.md").symlink_to(root / "elsewhere.md")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "SYSTEM.md" in result.err
    assert "symlink" in result.err


def test_no_context_files_leaves_out_every_file_including_system_md(world: World) -> None:
    root = project(world)
    write(root / "AGENTS.md", "agents")
    write(world.tau / "AGENTS.md", "global")
    prompt = assemble(world, root, context_files=False)
    assert files_of(world, prompt) == []
    assert "<project_context>" not in prompt.text
    assert [s.origin for s in prompt.sources] == ["base"]


# -- worktrees


def worktree(world: World, at: Path, main: Path) -> Path:
    """A linked worktree at `at` of the repository at `main`, laid out as `git worktree add`
    does."""
    admin = main / ".git" / "worktrees" / at.name
    write(admin / "HEAD", "ref: refs/heads/feat\n")
    write(admin / "commondir", "../..\n")
    write(main / ".git" / "HEAD", "ref: refs/heads/main\n")
    write(at / ".git", f"gitdir: {admin}\n")
    return at


def test_a_worktree_inside_its_main_repository_hides_the_main_files_of_the_same_name(
    world: World,
) -> None:
    main = world.home / "repo"
    write(main / "CLAUDE.md", "main copy")
    wt = worktree(world, main / ".claude" / "worktrees" / "wt", main)
    write(wt / "CLAUDE.md", "worktree copy")
    prompt = assemble(world, wt)
    assert files_of(world, prompt) == ["home/repo/.claude/worktrees/wt/CLAUDE.md"]
    assert "main copy" not in prompt.text


def test_a_worktree_hides_only_the_file_with_its_own_files_name(world: World) -> None:
    main = world.home / "repo"
    write(main / "AGENTS.md", "main agents")
    wt = worktree(world, main / ".claude" / "worktrees" / "wt", main)
    write(wt / "CLAUDE.md", "worktree claude")
    prompt = assemble(world, wt)
    assert files_of(world, prompt) == [
        "home/repo/AGENTS.md",
        "home/repo/.claude/worktrees/wt/CLAUDE.md",
    ]


def test_a_worktree_with_no_context_file_hides_nothing(world: World) -> None:
    main = world.home / "repo"
    write(main / "CLAUDE.md", "main copy")
    wt = worktree(world, main / ".claude" / "worktrees" / "wt", main)
    assert files_of(world, assemble(world, wt)) == ["home/repo/CLAUDE.md"]


def test_the_hiding_applies_from_a_directory_inside_the_worktree(world: World) -> None:
    main = world.home / "repo"
    write(main / "CLAUDE.md", "main copy")
    wt = worktree(world, main / ".claude" / "worktrees" / "wt", main)
    write(wt / "CLAUDE.md", "worktree copy")
    deeper = wt / "src"
    deeper.mkdir()
    assert files_of(world, assemble(world, deeper)) == ["home/repo/.claude/worktrees/wt/CLAUDE.md"]


def test_a_sibling_worktree_hides_nothing(world: World) -> None:
    main = world.home / "repo"
    write(main / "CLAUDE.md", "main copy")
    wt = worktree(world, world.home / "feat", main)
    write(wt / "CLAUDE.md", "worktree copy")
    # The main repository is not an ancestor of the worktree, so nothing reaches it.
    assert files_of(world, assemble(world, wt)) == ["home/feat/CLAUDE.md"]


def test_an_ordinary_repository_hides_nothing(world: World) -> None:
    repo = world.home / "repo"
    write(repo / ".git" / "HEAD", "ref: refs/heads/main\n")
    write(repo / "CLAUDE.md", "only copy")
    assert files_of(world, assemble(world, repo)) == ["home/repo/CLAUDE.md"]


def git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", *who, *args], cwd=cwd, env=env, capture_output=True, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_worktree_made_by_git_hides_the_same_way(world: World) -> None:
    main = world.home / "repo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    write(main / "CLAUDE.md", "main copy")
    git(main, "add", ".")
    git(main, "commit", "-qm", "init")
    wt = main / ".claude" / "worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    write(wt / "CLAUDE.md", "worktree copy")
    assert files_of(world, assemble(world, wt)) == ["home/repo/.claude/worktrees/wt/CLAUDE.md"]


# -- a file that is found and cannot be used


def refused(world: World, cwd: Path | None = None) -> str:
    with pytest.raises(ContextFileError) as raised:
        assemble(world, cwd)
    return str(raised.value)


def test_a_file_that_is_not_utf8_stops_the_assembly_naming_the_path_and_the_way_out(
    world: World,
) -> None:
    path = write(world.home / "AGENTS.md", b"\xff\xfe not utf-8 \xff")
    message = refused(world)
    assert str(path.resolve()) in message
    assert "UTF-8" in message
    assert "--no-context-files" in message


def test_a_context_file_error_is_a_config_error() -> None:
    assert issubclass(ContextFileError, ConfigError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_stops_the_assembly(world: World) -> None:
    path = write(world.home / "AGENTS.md", "secret")
    path.chmod(0)
    try:
        message = refused(world)
    finally:
        path.chmod(0o600)
    assert str(path.resolve()) in message
    assert "Permission denied" in message


def test_a_symlink_to_nothing_stops_the_assembly(world: World) -> None:
    (world.home / "AGENTS.md").symlink_to(world.home / "gone.md")
    message = refused(world)
    assert str(world.home / "AGENTS.md") in message
    assert "symlink to nothing" in message


def test_a_non_utf8_system_md_stops_the_assembly_naming_it(world: World) -> None:
    root = project(world, b"\xff\xfe")
    assert str(root / ".tau" / "SYSTEM.md") in refused(world, root)


def test_no_context_files_does_not_read_what_would_have_failed(world: World) -> None:
    write(world.home / "AGENTS.md", b"\xff")
    assert "\xff" not in assemble(world, context_files=False).text


def test_a_byte_order_mark_is_dropped(world: World) -> None:
    write(world.home / "AGENTS.md", b"\xef\xbb\xbfhello")
    prompt = assemble(world)
    assert "﻿" not in prompt.text
    assert '">\nhello\n</project_instructions>' in prompt.text
    assert prompt.sources[-1].size == len(b"hello")


# -- the base text, the appended sections, the config key


def test_the_built_in_base_text_names_the_working_directory_and_nothing_else_of_0_15s_fields(
    world: World,
) -> None:
    cwd = world.home / "w"
    cwd.mkdir()
    prompt = assemble(world, cwd, context_files=False)
    assert prompt.text.startswith(BASE)
    assert f"`{cwd.resolve()}`" in prompt.text
    assert "{{" not in prompt.text
    assert "Available tools" not in prompt.text
    assert [s.origin for s in prompt.sources] == ["base"]
    assert prompt.sources[0].size == len(prompt.text.encode())


def test_a_user_system_prompt_replaces_the_base_and_is_literal(world: World) -> None:
    world.user_config({"system_prompt": "Be {{brief}} in {{cwd}}."})
    prompt = assemble(world, context_files=False)
    assert prompt.text == "Be {{brief}} in {{cwd}}."
    assert [(s.origin, s.size) for s in prompt.sources] == [("config", len(prompt.text))]


def test_the_projects_system_prompt_overrides_the_users(world: World) -> None:
    world.user_config({"system_prompt": "user"})
    root = world.project("proj", {"system_prompt": "project"})
    trust(world, root)
    assert assemble(world, root, context_files=False).text == "project"


def test_null_in_a_nearer_layer_drops_the_inherited_system_prompt(world: World) -> None:
    world.user_config({"system_prompt": "user"})
    root = world.project("proj", {"system_prompt": None})
    trust(world, root)
    assert assemble(world, root, context_files=False).text.startswith(BASE)


def test_an_empty_system_prompt_is_no_base_text_at_all(world: World) -> None:
    world.user_config({"system_prompt": ""})
    write(world.home / "AGENTS.md", "rules")
    prompt = assemble(world)
    assert prompt.text.startswith("<project_context>")
    assert "config" not in [s.origin for s in prompt.sources]
    nothing = assemble(world, context_files=False)
    assert nothing.text == ""
    assert nothing.sources == ()


def test_the_command_lines_system_prompt_is_the_nearest_layer(world: World) -> None:
    root = world.project("proj", {"system_prompt": "project"})
    trust(world, root)
    world.user_config({"system_prompt": "user"})
    prompt = assemble(world, root, cli={"system_prompt": "from the flag"}, context_files=False)
    assert prompt.text == "from the flag"
    assert [s.origin for s in prompt.sources] == ["flag"]


def test_appended_sections_follow_the_base_in_order_and_precede_the_context_files(
    world: World,
) -> None:
    world.user_config({"system_prompt": "base"})
    write(world.home / "AGENTS.md", "rules")
    prompt = assemble(world, append=("one", "", "two"))
    in_order(prompt.text, "base\n\none\n\ntwo\n\n<project_context>", "rules")
    assert [s.origin for s in prompt.sources if not s.origin.startswith("/")] == [
        "config",
        "flag",
        "flag",
    ]


def test_a_non_string_system_prompt_is_a_config_problem(world: World) -> None:
    for bad in (3, ["a"], {"a": 1}, True):
        world.user_config({"system_prompt": bad})
        result = tau(world, "config", "check")
        assert result.code == 1, bad
        assert "'system_prompt' must be a string" in result.out


def test_config_check_show_says_which_layer_the_base_text_comes_from(world: World) -> None:
    config = world.user_config({**MOCK, "system_prompt": "twelve bytes"})
    shown = tau(world, "config", "check", "--show", "m")
    assert shown.code == 0
    assert f"system_prompt  12 bytes  [{config}]" in shown.out
    config.write_text(json.dumps(MOCK), encoding="utf-8")
    assert "system_prompt  (built in)" in tau(world, "config", "check", "--show", "m").out


# -- tau context


def test_tau_context_lists_each_part_with_its_origin_and_size(configured: World) -> None:
    write(configured.home / "AGENTS.md", "rules")
    result = tau(configured, "context", "--append-system-prompt", "more")
    assert result.code == 0, result.err
    lines = [line.split(maxsplit=1) for line in result.out.splitlines()]
    rows = {origin: int(size) for size, origin in lines}
    assert rows["flag"] == 4
    assert rows[f"{configured.home.resolve()}/AGENTS.md"] == 5
    assert list(rows)[:2] == ["base", "flag"]


def test_tau_context_show_prints_the_text_a_new_session_would_be_given(
    configured: World,
) -> None:
    write(configured.home / "AGENTS.md", "rules")
    result = tau(configured, "context", "--show", "--system-prompt", "mine", "-nc")
    assert result.out == "mine\n"
    full = tau(configured, "context", "--show")
    assert full.out.startswith(BASE)
    assert "rules" in full.out


def test_tau_context_shows_no_control_character_from_a_file(configured: World) -> None:
    write(configured.home / "AGENTS.md", "\x1b[2Jrules")
    shown = tau(configured, "context", "--show")
    assert "\x1b" not in shown.out
    assert "\\x1b[2Jrules" in shown.out


def test_tau_context_exits_1_with_the_message_for_an_unusable_file(configured: World) -> None:
    path = write(configured.home / "AGENTS.md", b"\xff")
    result = tau(configured, "context")
    assert result.code == 1
    assert result.err.startswith("tau: context file ")
    assert str(path) in result.err
    assert "--no-context-files" in result.err
    assert tau(configured, "context", "-nc").code == 0


def test_tau_context_stops_in_an_untrusted_project(configured: World) -> None:
    root = configured.project("proj", {"system_prompt": "project"})
    result = tau(configured, "context", cwd=root)
    assert result.code == 1
    assert "tau trust" in result.err


# -- the shared start


LOOP = asyncio.new_event_loop()


@pytest.fixture(scope="module", autouse=True)
def close_loop() -> Iterator[None]:
    yield
    LOOP.close()


def run[T](coro: Coroutine[object, object, T]) -> T:
    return LOOP.run_until_complete(coro)


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    return parser.parse_args(list(argv))


def start(world: World, *argv: str, cwd: Path | None = None) -> Started:
    return run(start_session(parse(*argv), environ=world.environ, cwd=cwd or world.home))


def stop(started: Started) -> None:
    run(started.stop())


def prompt_of(session: Session) -> str:
    body = session.tree.walk(session.cursor(0).at)[1].body
    assert isinstance(body, Body.System)
    return body.text


def use(started: Started) -> None:
    """Give the session a message so that stopping it does not delete it."""
    session = started.session
    session.append(session.cursor(0).at, Body.User("hello"), Author.Frontend("t"), 0)


def test_a_new_session_starts_with_the_assembled_prompt_as_its_system_node(
    configured: World,
) -> None:
    write(configured.home / "AGENTS.md", "rules")
    started = start(configured, "--append-system-prompt", "more")
    try:
        expected = assemble(configured, append=("more",))
        assert prompt_of(started.session) == expected.text
        assert "rules" in expected.text
        assert tau(configured, "context", "--show", "--append-system-prompt", "more").out == (
            expected.text + "\n"
        )
    finally:
        stop(started)


def test_system_prompt_and_no_context_files_flags_make_the_node(configured: World) -> None:
    write(configured.home / "AGENTS.md", "rules")
    started = start(configured, "--system-prompt", "only this", "-nc")
    try:
        assert prompt_of(started.session) == "only this"
    finally:
        stop(started)


def test_dash_nc_is_a_flag_and_dash_n_still_takes_a_name(configured: World) -> None:
    flags = parse("-nc")
    assert flags.no_context_files
    assert flags.name is None
    named = parse("-n", "c")
    assert named.name == "c"
    assert not named.no_context_files


def test_an_unusable_context_file_stops_the_start_before_a_session_exists(
    configured: World,
) -> None:
    write(configured.home / "AGENTS.md", b"\xff")
    with pytest.raises(ContextFileError, match="--no-context-files"):
        start(configured)
    assert not (configured.tau / "sessions").exists() or not list(
        (configured.tau / "sessions").iterdir()
    )
    stop(start(configured, "-nc"))


def test_the_recorded_prompt_survives_a_reload_and_a_continue_unchanged(
    configured: World,
) -> None:
    agents = write(configured.home / "AGENTS.md", "version one")
    started = start(configured)
    use(started)
    recorded = prompt_of(started.session)
    directory = started.directory
    assert directory is not None
    stop(started)
    assert "version one" in recorded

    agents.write_text("version two", encoding="utf-8")
    configured.user_config({**MOCK, "system_prompt": "a new base"})
    reread = Session.load(directory)
    try:
        assert prompt_of(reread) == recorded
    finally:
        reread.close()
    for flags in (["-c"], ["--session", directory.name], ["-c", "-nc"]):
        continued = start(configured, *flags)
        try:
            assert prompt_of(continued.session) == recorded
            assert continued.session.tree.walk(continued.session.cursor(0).at)[1].body == (
                Body.System(recorded)
            )
        finally:
            stop(continued)


def test_a_fork_copies_the_prompt_it_does_not_rebuild_it(configured: World) -> None:
    agents = write(configured.home / "AGENTS.md", "version one")
    started = start(configured)
    use(started)
    recorded = prompt_of(started.session)
    directory = started.directory
    assert directory is not None
    stop(started)
    agents.write_text("version two", encoding="utf-8")
    forked = start(configured, "--fork", directory.name, "-nc")
    try:
        assert prompt_of(forked.session) == recorded
    finally:
        stop(forked)


@pytest.mark.parametrize(
    ("chosen", "reason"),
    [
        (["-c"], "the session already has its system prompt (node 1)"),
        (["--session", "SOME"], "the session already has its system prompt (node 1)"),
        (["--fork", "SOME"], "a fork copies its source's system prompt (node 1)"),
    ],
)
@pytest.mark.parametrize(
    "flag", [["--system-prompt", "x"], ["--append-system-prompt", "x"]], ids=["base", "append"]
)
def test_the_prompt_flags_are_refused_for_a_session_that_has_its_prompt(
    configured: World, chosen: list[str], reason: str, flag: list[str]
) -> None:
    with pytest.raises(ConfigError) as raised:
        start(configured, *chosen, *flag)
    assert flag[0] in str(raised.value)
    assert reason in str(raised.value)
    assert "start a new session for a different one" in str(raised.value)
