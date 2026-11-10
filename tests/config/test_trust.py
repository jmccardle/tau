"""Finding the project and trusting it (D13), through `tau trust`, `tau config check`, and
`load_config`, on real directories."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from world import (
    EOF,
    URL,
    World,
    has_project,
    loaded,
    models,
    project_root,
    tau,
    terminal,
    trust,
    yes,
)

from tau import ConfigError, TrustError, UntrustedProject, load_config

LITERAL_KEY = "sk-literal-secret-9f3"
SHELL_KEY = "sk-from-the-shell-77c"


def write_config(root: Path, config: object) -> None:
    text = config if isinstance(config, str) else json.dumps(config)
    (root / ".tau" / "config.json").write_text(text, encoding="utf-8")


# -- finding the project and TAU_HOME

PROJECTS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: projects (D13 trust) are not ported; finding one is an error",
)


def test_tau_home_defaults_to_dot_tau_and_expands_a_tilde(world: World) -> None:
    assert loaded(world).home == world.home / ".tau"
    environ = {**world.environ, "TAU_HOME": "~/elsewhere"}
    assert load_config(world.home, environ, ask=None).home == world.home / "elsewhere"
    absolute = world.home.parent / "srv" / "tau"  # "/srv/tau" is not absolute on Windows
    environ = {**world.environ, "TAU_HOME": str(absolute)}
    assert load_config(world.home, environ, ask=None).home == absolute


def test_without_home_windows_reads_userprofile_and_elsewhere_it_is_an_error(
    world: World,
) -> None:
    """A Windows shell need not set HOME; its home is `%USERPROFILE%` there."""
    environ = {k: v for k, v in world.environ.items() if k != "HOME"}
    environ["USERPROFILE"] = str(world.home)
    if sys.platform == "win32":
        assert load_config(world.home, environ, ask=None).home == world.home / ".tau"
    else:
        with pytest.raises(ConfigError, match="HOME is not set"):
            load_config(world.home, environ, ask=None)


def test_the_nearest_project_upward_wins(world: World) -> None:
    outer = world.project("outer", models(o={}))
    inner = outer / "sub" / "inner"
    (inner / ".tau").mkdir(parents=True)
    deep = inner / "a" / "b"
    deep.mkdir(parents=True)
    for root in (inner, outer):
        trust(world, root)
    assert project_root(world, deep) == inner
    assert project_root(world, outer / "sub") == outer


def test_home_is_not_a_project_and_the_search_stops_there(world: World, tmp_path: Path) -> None:
    world.tau.mkdir()
    (tmp_path / ".tau").mkdir()  # above $HOME: never reached from inside it
    work = world.home / "work"
    work.mkdir()
    assert not has_project(world, work)
    assert not has_project(world, world.home)


@PROJECTS
def test_outside_home_the_search_runs_to_the_root(world: World, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    (other / ".tau").mkdir(parents=True)
    deep = other / "x" / "y"
    deep.mkdir(parents=True)
    assert has_project(world, deep)


def test_the_parent_of_tau_home_is_never_a_project(world: World, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / ".tau").mkdir(parents=True)
    work = shared / "work"
    work.mkdir()
    environ = {**world.environ, "TAU_HOME": str(shared / ".tau")}
    assert load_config(work, environ, ask=None).project is None


# -- trust state


def test_trusting_makes_the_project_load_and_the_state_is_private(world: World) -> None:
    root = world.project(config=models(a={}))
    with pytest.raises(UntrustedProject):
        loaded(world, root)
    trust(world, root)
    assert loaded(world, root).catalog.names() == ("a",)
    assert stat.S_IMODE((world.tau / "trusted.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(world.tau.stat().st_mode) == 0o700


def test_two_trusted_projects_stay_trusted(world: World) -> None:
    one, two = world.project("one", models(a={})), world.project("two", models(b={}))
    trust(world, one)
    trust(world, two)
    assert loaded(world, one).catalog.names() == ("a",)
    assert loaded(world, two).catalog.names() == ("b",)


def test_a_changed_config_is_untrusted_again_and_the_report_shows_the_change(
    world: World,
) -> None:
    root = world.project(config=models(a={}))
    trust(world, root)
    write_config(root, models(a={"base_url": "http://elsewhere.example/v1"}))
    with pytest.raises(UntrustedProject, match=f"tau trust {root}") as caught:
        loaded(world, root)
    assert "elsewhere.example" in str(caught.value)
    check = tau(world, "config", "check", cwd=root)
    assert check.code == 1
    assert "not trusted" in check.out
    again = tau(world, "trust", cwd=root)
    assert again.code == 0
    assert "elsewhere.example" in again.out
    assert "has changed since you approved it" in again.out
    assert loaded(world, root).catalog.resolve("a").base_url == "http://elsewhere.example/v1"


def test_a_missing_config_is_not_an_empty_one_and_adding_one_untrusts(world: World) -> None:
    root = world.project()
    trust(world, root)
    assert loaded(world, root).catalog.names() == ()
    write_config(root, "{}")
    with pytest.raises(UntrustedProject):
        loaded(world, root)
    trust(world, root)
    write_config(root, models(a={}))
    with pytest.raises(UntrustedProject):
        loaded(world, root)


def test_group_readable_state_is_fine_only_writable_state_is_refused(world: World) -> None:
    root = world.project()
    trust(world, root)
    world.tau.chmod(0o750)
    (world.tau / "trusted.json").chmod(0o640)
    assert loaded(world, root).project is not None
    (world.tau / "trusted.json").chmod(0o664)
    with pytest.raises(TrustError, match="chmod 600"):
        loaded(world, root)
    check = tau(world, "config", "check", cwd=root)
    assert check.code == 1
    assert check.out.startswith(f"{world.tau / 'trusted.json'}: error: ")


def test_a_group_writable_tau_home_is_refused_not_fixed(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    world.tau.chmod(0o770)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "chmod 700" in result.err
    assert stat.S_IMODE(world.tau.stat().st_mode) == 0o770


def test_a_corrupt_state_file_fails_loudly(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    state = world.tau / "trusted.json"
    state.write_text("[1, 2", encoding="utf-8")
    state.chmod(0o600)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "trusted.json" in result.err
    assert "Traceback" not in result.err


def test_the_state_file_holds_a_hash_and_no_secret(world: World) -> None:
    inner = {"models": {"m": {"model": "m", "base_url": URL}}, "api_key": LITERAL_KEY}
    root = world.project(config={"models": {"g": inner}})
    trust(world, root)
    stored = (world.tau / "trusted.json").read_text(encoding="utf-8")
    assert LITERAL_KEY not in stored
    record = json.loads(stored)["projects"][str(root)]
    assert len(record["sha256"]) == 64


# -- the report: what it says and never says


def test_the_report_shows_overrides_old_to_new_and_the_directory_checks(world: World) -> None:
    world.user_config(models(gpt={}))
    root = world.project(config=models(gpt={"base_url": "http://evil.example/v1"}, extra={}))
    (root / ".tau").chmod(0o755)
    result = tau(world, "trust", cwd=root)
    assert result.code == 0
    assert '"base_url": "http://evil.example/v1"' in result.out
    assert '"extra"' in result.out
    assert ".tau/ mode is 755" in result.out
    assert (root / ".tau").stat().st_mode & 0o777 == 0o700


def test_the_report_shows_an_override_of_options_and_max_tokens(world: World) -> None:
    world.user_config({"models": {"gpt": {"model": "gpt", "provider": "mock"}}})
    override = {"max_tokens": 77, "options": {"reply": "from-project"}}
    root = world.project(config={"models": {"gpt": override}})
    result = tau(world, "trust", cwd=root)
    assert result.code == 0
    assert '"max_tokens": 77' in result.out
    assert '"reply": "from-project"' in result.out


def test_the_report_never_prints_a_key_or_a_dotenv_value(world: World) -> None:
    world.user_config(models(a={"api_key_env": "SOME_KEY"}))
    root = world.project(config=models(a={"api_key": LITERAL_KEY}, b={"api_key": LITERAL_KEY}))
    (root / ".env").write_text("FIRST=sk-hidden-1\nexport SECOND='sk-hidden-2'\n", encoding="utf-8")
    world.environ["SOME_KEY"] = SHELL_KEY
    result = tau(world, "trust", cwd=root)
    assert result.code == 0
    assert "FIRST, SECOND" in result.out  # names, so you know what the project can set
    for secret in (LITERAL_KEY, SHELL_KEY, "sk-hidden"):
        assert secret not in result.both
    # the diff after a change redacts too, and says only that a secret changed
    write_config(root, models(a={"api_key": LITERAL_KEY + "x"}, b={"api_key": LITERAL_KEY}))
    again = tau(world, "trust", cwd=root)
    assert "a secret value changed" in again.out
    assert LITERAL_KEY not in again.both


def test_the_report_goes_to_the_asker_and_a_refused_project_stays_untrusted(world: World) -> None:
    root = world.project(config=models(a={}))
    shown: list[str] = []

    def no(report: str) -> bool:
        shown.append(report)
        return False

    with pytest.raises(UntrustedProject, match=f"tau trust {root}"):
        load_config(root, world.environ, ask=no)
    assert '"models"' in shown[0]
    assert not (root / ".tau" / ".gitignore").exists()
    assert load_config(root, world.environ, ask=yes).project is not None
    assert (root / ".tau" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    loaded(world, root)  # trusted now: no question


def test_a_config_changed_while_the_report_was_read_is_refused(world: World) -> None:
    root = world.project(config=models(a={}))

    def ask_then_swap(_report: str) -> bool:
        write_config(root, models(a={"base_url": "http://evil.example/v1"}))
        return True

    with pytest.raises(TrustError, match="changed while you were reading; run again"):
        load_config(root, world.environ, ask=ask_then_swap)
    with pytest.raises(UntrustedProject):
        loaded(world, root)


@pytest.mark.parametrize(("typed", "approved"), [("y\n", True), ("n\n", False), (EOF, False)])
def test_a_live_terminal_asks_and_only_yes_approves(
    world: World, typed: str, approved: bool
) -> None:
    root = world.project(config=models(a={}))
    with terminal(typed) as ask:
        assert ask.ask is not None
        if approved:
            assert load_config(root, world.environ, ask=ask.ask).project is not None
        else:
            with pytest.raises(UntrustedProject, match=f"tau trust {root}"):
                load_config(root, world.environ, ask=ask.ask)


def test_a_changed_or_added_dotenv_is_untrusted_again(world: World) -> None:
    root = world.project(config=models(a={}))
    (root / ".env").write_text("A=1\n", encoding="utf-8")
    trust(world, root)
    assert loaded(world, root).environ["A"] == "1"
    (root / ".env").write_text("A=2\nB=3\n", encoding="utf-8")
    with pytest.raises(UntrustedProject, match=r"\.env changed since you approved it"):
        loaded(world, root)
    assert "not trusted" in tau(world, "config", "check", cwd=root).out
    again = tau(world, "trust", cwd=root)
    assert again.code == 0
    assert ".env changed since you approved it" in again.out
    assert "A, B" in again.out
    assert loaded(world, root).environ["B"] == "3"
    (root / ".env").unlink()
    with pytest.raises(UntrustedProject, match=r"\.env changed"):
        loaded(world, root)


def test_a_dotenv_added_to_a_trusted_project_needs_approval(world: World) -> None:
    root = world.project(config=models(a={}))
    trust(world, root)
    (root / ".env").write_text("LATE=1\n", encoding="utf-8")
    with pytest.raises(UntrustedProject, match=r"\.env changed"):
        loaded(world, root)


def test_the_trust_command_for_a_path_with_spaces_can_be_pasted(world: World) -> None:
    root = world.project("my project")
    with pytest.raises(UntrustedProject) as caught:
        loaded(world, root)
    assert f"tau trust '{root}'" in str(caught.value)
    assert f"tau trust '{root}'" in tau(world, "config", "check", cwd=root).out


# -- a config with errors cannot be approved


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('{"models": {"bad": {"modle": "x"}}}', "modle"),
        ("{not json", "invalid JSON"),
        ("[" * 200_000, "invalid JSON"),
        ('{"a": ' * 100_000, "invalid JSON"),
    ],
)
def test_a_config_with_errors_or_hostile_json_cannot_be_approved(
    world: World, text: str, named: str
) -> None:
    root = world.project(config=text)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert named in result.err or "nested too deeply" in result.err
    assert "Traceback" not in result.err
    with terminal("y\n") as ask, pytest.raises(TrustError) as caught:
        load_config(root, world.environ, ask=ask.ask)
    assert not isinstance(caught.value, UntrustedProject)
    assert not (root / ".tau" / ".gitignore").exists()


def test_a_malformed_dotenv_blocks_approval(world: World) -> None:
    root = world.project()
    (root / ".env").write_text("nonsense\n", encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert f"{root / '.env'}:1:" in result.err


@pytest.mark.parametrize("name", ["config.json", ".env"])
def test_a_file_over_a_mebibyte_is_refused_naming_file_and_size(world: World, name: str) -> None:
    root = world.project()
    path = root / ".tau" / name if name == "config.json" else root / name
    path.write_text("{" + " " * (2 << 20) + "}", encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert name in result.err
    assert "2097154" in result.err


# -- trusted.json that cannot be read, a project that does not exist yet


def test_a_trusted_json_that_is_a_directory_is_one_line_not_a_traceback(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    (world.tau / "trusted.json").mkdir(mode=0o700)
    for argv in (("trust",), ("config", "check")):
        result = tau(world, *argv, cwd=root)
        assert result.code == 1
        assert "Traceback" not in result.both
        assert "trusted.json" in result.both
    assert tau(world, "trust", cwd=root).err.count("\n") == 1


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="root reads a file of mode 000; Windows file modes cannot deny reading",
)
def test_a_trusted_json_nobody_can_read_is_one_line_not_a_traceback(world: World) -> None:
    root = world.project()
    trust(world, root)
    (world.tau / "trusted.json").chmod(0)
    for argv in (("trust",), ("config", "check")):
        result = tau(world, *argv, cwd=root)
        assert result.code == 1
        assert "Traceback" not in result.both
        assert "trusted.json" in result.both
    assert tau(world, "trust", cwd=root).err.count("\n") == 1


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX makes the project; see the next test")
def test_trust_with_no_project_says_projects_are_not_available_on_windows(world: World) -> None:
    work = world.home / "work"
    work.mkdir()
    result = tau(world, "trust", cwd=work)
    assert result.code == 1
    assert "projects are not available on Windows yet" in result.err
    assert not (work / ".tau").exists()


@PROJECTS
def test_trust_makes_the_project_where_there_is_none(world: World) -> None:
    work = world.home / "work"
    (work / "src").mkdir(parents=True)
    result = tau(world, "trust", "src", cwd=work)
    assert result.code == 0, result.err
    tau_dir = work / "src" / ".tau"
    assert stat.S_IMODE(tau_dir.stat().st_mode) == 0o700
    assert (tau_dir / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert project_root(world, work / "src") == work / "src"
    assert not (work / ".tau").exists()


def test_trust_uses_the_project_above_rather_than_making_a_new_one(world: World) -> None:
    root = world.project()
    (root / "deep").mkdir()
    assert tau(world, "trust", "deep", cwd=root).code == 0
    assert not (root / "deep" / ".tau").exists()
    assert project_root(world, root) == root


def test_trust_cannot_make_a_project_over_a_file_named_dot_tau(world: World) -> None:
    work = world.home / "work"
    work.mkdir()
    (work / ".tau").write_text("not a directory", encoding="utf-8")
    result = tau(world, "trust", cwd=work)
    assert result.code == 1
    assert "Traceback" not in result.err
    assert result.err.count("\n") == 1


# -- symlinks, FIFOs, and foreign owners are refused, never followed


@pytest.mark.parametrize("name", ["config.json", ".gitignore", ".env"])
def test_a_symlinked_project_file_is_refused_without_reading_or_writing_through_it(
    world: World, tmp_path: Path, name: str
) -> None:
    root = world.project()
    target = tmp_path / "elsewhere"
    target.write_text(f"API_KEY={LITERAL_KEY}\n", encoding="utf-8")
    link = root / name if name == ".env" else root / ".tau" / name
    link.symlink_to(target)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "symlink" in result.err
    assert str(link) in result.err or name in result.err
    assert LITERAL_KEY not in result.both
    assert target.read_text(encoding="utf-8") == f"API_KEY={LITERAL_KEY}\n"
    with pytest.raises(TrustError, match="symlink"):
        load_config(root, world.environ, ask=yes)


def test_a_dotenv_symlinked_after_approval_is_refused_at_startup(
    world: World, tmp_path: Path
) -> None:
    root = world.project(config=models(a={}))
    trust(world, root)
    target = tmp_path / "real.env"
    target.write_text("A=1\n", encoding="utf-8")
    (root / ".env").symlink_to(target)
    with pytest.raises(TrustError, match="symlink"):
        loaded(world, root)
    assert "symlink" in tau(world, "config", "check", cwd=root).out


def test_a_symlinked_tau_dir_is_not_a_project(world: World, tmp_path: Path) -> None:
    real = tmp_path / "real_tau"
    real.mkdir(mode=0o700)
    root = world.home / "linked"
    root.mkdir()
    (root / ".tau").symlink_to(real)
    assert not has_project(world, root)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "no project" in result.err
    assert "symlink" in result.err  # says why the .tau it found is not a project


@pytest.mark.parametrize("name", ["config.json", ".env"])
def test_a_fifo_is_refused_without_hanging(world: World, name: str) -> None:
    root = world.project()
    os.mkfifo(root / ".tau" / name if name == "config.json" else root / name)
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert "not a regular file" in result.err


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() != 0,
    reason="needs root to make a directory another user owns; Windows has no projects yet",
)
def test_a_tau_dir_owned_by_someone_else_is_not_a_project_or_approvable(world: World) -> None:
    outer = world.project("outer", models(o={}))
    inner = outer / "inner"
    (inner / ".tau").mkdir(parents=True)
    os.chown(inner / ".tau", 65534, 65534)
    trust(world, outer)
    assert project_root(world, inner) == outer
    foreign = world.project("foreign")
    os.chown(foreign / ".tau", 65534, 65534)
    refused = tau(world, "trust", cwd=foreign)
    assert refused.code == 1
    assert "not owned by you" in refused.err


# -- approval fixes what it can


def test_approval_writes_a_private_ignore_file_and_keeps_an_existing_one(world: World) -> None:
    fresh = world.project("fresh")
    trust(world, fresh)
    ignore = fresh / ".tau" / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o644
    kept = world.project("kept")
    (kept / ".tau" / ".gitignore").write_text("config.json\n", encoding="utf-8")
    result = tau(world, "trust", cwd=kept)
    assert result.code == 0
    assert "does not ignore everything" in result.out  # warned, not blocked
    assert (kept / ".tau" / ".gitignore").read_text(encoding="utf-8") == "config.json\n"


# -- permission errors are one line naming the path, never a traceback

CANNOT_DENY_ROOT = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod cannot deny root; Windows file modes cannot deny anyone",
)


def one_line_refusal(
    world: World, root: Path, path: Path, argvs: tuple[tuple[str, ...], ...]
) -> None:
    """Each command fails with status 1 and says which path, without a traceback; in one line."""
    for argv in argvs:
        result = tau(world, *argv, cwd=root)
        assert result.code == 1, result.both
        assert "Traceback" not in result.both
        assert str(path) in result.both, result.both
        refusals = [line for line in result.both.splitlines() if "Permission denied" in line]
        assert len(refusals) == 1, result.both


@CANNOT_DENY_ROOT
def test_a_config_nobody_can_read_is_one_line_naming_it(world: World) -> None:
    root = world.project(config=models(a={}))
    (root / ".tau" / "config.json").chmod(0)
    one_line_refusal(world, root, root / ".tau" / "config.json", (("trust",), ("config", "check")))


@CANNOT_DENY_ROOT
def test_a_tau_dir_nobody_can_open_is_one_line_naming_it(world: World) -> None:
    root = world.project()
    (root / ".tau").chmod(0)
    try:
        one_line_refusal(world, root, root / ".tau", (("trust",), ("config", "check")))
    finally:
        (root / ".tau").chmod(0o700)


@CANNOT_DENY_ROOT
def test_a_tau_home_nobody_can_open_is_one_line_naming_it(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    world.tau.chmod(0)
    try:
        one_line_refusal(world, root, world.tau, (("trust",), ("config", "check")))
    finally:
        world.tau.chmod(0o700)


@CANNOT_DENY_ROOT
def test_a_read_only_tau_home_cannot_take_the_approval_and_says_so(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o500)
    try:
        one_line_refusal(world, root, world.tau, (("trust",),))
    finally:
        world.tau.chmod(0o700)
    assert not (root / "trusted.json").exists()


@CANNOT_DENY_ROOT
def test_a_failed_approval_leaves_no_new_tau_dir_behind(world: World) -> None:
    work = world.home / "work"
    work.mkdir()
    world.tau.mkdir(mode=0o500)
    try:
        assert tau(world, "trust", cwd=work).code == 1
    finally:
        world.tau.chmod(0o700)
    assert not (work / ".tau").exists()
    assert not has_project(world, work)


def test_a_record_from_before_dotenv_was_pinned_is_untrusted_not_an_error(world: World) -> None:
    root = world.project(config=models(a={}))
    world.tau.mkdir(mode=0o700)
    record = {"sha256": "0" * 64, "config": ""}
    state = world.tau / "trusted.json"
    state.write_text(json.dumps({"projects": {str(root.resolve()): record}}), encoding="utf-8")
    state.chmod(0o600)
    with pytest.raises(UntrustedProject):
        load_config(root, world.environ, ask=None)
    trust(world, root)  # asking again works, and replaces the old record
    assert project_root(world, root) == root


def test_an_untrusted_project_with_errors_does_not_offer_the_trust_command(world: World) -> None:
    root = world.project(config=models(a={"max_tokens": 0}))
    with pytest.raises(UntrustedProject) as raised:
        load_config(root, world.environ, ask=None)
    assert "tau trust" not in str(raised.value)
    assert "cannot be trusted until these are fixed" in str(raised.value)


@PROJECTS
def test_a_symlinked_tau_above_does_not_block_trusting_below_it(
    world: World, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    (shared / "proj").mkdir(parents=True)
    (shared / ".tau").symlink_to(tmp_path / "nowhere")
    result = tau(world, "trust", str(shared / "proj"), cwd=tmp_path)
    assert result.code == 0, result.err
    assert (shared / "proj" / ".tau").is_dir()
    assert project_root(world, shared / "proj") == shared / "proj"
