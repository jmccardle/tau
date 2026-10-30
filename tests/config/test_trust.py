"""Finding the project, trusting it, and its `.env` (D13), on real directories."""

import io
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tau_next import (
    LayerText,
    TrustError,
    UntrustedProject,
    approve_project,
    create_project_dir,
    ensure_trusted,
    find_project,
    is_interactive,
    is_trusted,
    project_environment,
    read_models,
    tau_home,
    terminal_ask,
    trust_report,
)

URL = "http://localhost:8100/v1"
LITERAL_KEY = "sk-literal-secret-9f3"
ENV_KEY = "sk-from-the-shell-77c"


@dataclass(frozen=True)
class World:
    home: Path
    tau: Path  # TAU_HOME
    environ: dict[str, str]

    def project(self, name: str = "proj", config: object | None = None) -> Path:
        root = self.home / name
        (root / ".tau").mkdir(parents=True)
        if config is not None:
            text = config if isinstance(config, str) else json.dumps(config)
            (root / ".tau" / "config.json").write_text(text, encoding="utf-8")
        return root

    def user_config(self, config: object) -> None:
        self.tau.mkdir(mode=0o700, exist_ok=True)
        (self.tau / "config.json").write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home, home / ".tau", {"HOME": str(home)})


def models(**named: dict[str, object]) -> dict[str, object]:
    return {"models": {k: {"model": k, "base_url": URL, **v} for k, v in named.items()}}


# -- TAU_HOME and finding the project


def test_tau_home_defaults_and_expands_from_the_given_environ(world: World) -> None:
    assert tau_home(world.environ) == world.home / ".tau"
    assert tau_home({**world.environ, "TAU_HOME": "~/elsewhere"}) == world.home / "elsewhere"
    assert tau_home({**world.environ, "TAU_HOME": "/srv/tau"}) == Path("/srv/tau")


def test_nearest_tau_dir_upward_wins(world: World) -> None:
    outer = world.project("outer")
    inner = outer / "sub" / "inner"
    (inner / ".tau").mkdir(parents=True)
    deep = inner / "a" / "b"
    deep.mkdir(parents=True)
    assert find_project(deep, world.environ) == inner
    assert find_project(outer / "sub", world.environ) == outer


def test_home_is_not_a_project_and_the_search_stops_there(world: World, tmp_path: Path) -> None:
    world.tau.mkdir()
    (tmp_path / ".tau").mkdir()  # above $HOME: never reached from inside it
    work = world.home / "work"
    work.mkdir()
    assert find_project(work, world.environ) is None
    assert find_project(world.home, world.environ) is None


def test_outside_home_the_search_runs_to_the_root(world: World, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    (other / ".tau").mkdir(parents=True)
    deep = other / "x" / "y"
    deep.mkdir(parents=True)
    assert find_project(deep, world.environ) == other


def test_the_parent_of_tau_home_is_never_a_project(world: World, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / ".tau").mkdir(parents=True)
    work = shared / "work"
    work.mkdir()
    environ = {**world.environ, "TAU_HOME": str(shared / ".tau")}
    assert find_project(work, environ) is None


# -- trust state


def test_trust_round_trip_and_state_file_is_private(world: World) -> None:
    root = world.project(config=models(a={}))
    assert not is_trusted(root, world.tau)
    approve_project(root, world.tau)
    assert is_trusted(root, world.tau)
    state = world.tau / "trusted.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(world.tau.stat().st_mode) == 0o700
    assert not list(world.tau.glob("*.tmp"))


def test_approving_two_projects_keeps_both(world: World) -> None:
    one, two = world.project("one", models(a={})), world.project("two", models(b={}))
    approve_project(one, world.tau)
    approve_project(two, world.tau)
    assert is_trusted(one, world.tau)
    assert is_trusted(two, world.tau)


def test_an_absent_config_can_be_trusted_and_adding_one_untrusts(world: World) -> None:
    root = world.project()
    approve_project(root, world.tau)
    assert is_trusted(root, world.tau)
    (root / ".tau" / "config.json").write_text(json.dumps(models(a={})), encoding="utf-8")
    assert not is_trusted(root, world.tau)


def test_a_changed_config_is_untrusted_again_with_a_diff(world: World) -> None:
    root = world.project(config=models(a={}))
    approve_project(root, world.tau)
    changed = models(a={"base_url": "http://elsewhere.example/v1"})
    (root / ".tau" / "config.json").write_text(json.dumps(changed), encoding="utf-8")
    assert not is_trusted(root, world.tau)
    report = trust_report(root, world.tau)
    assert "has changed since you approved it" in report
    assert "--- approved .tau/config.json" in report
    assert "+++ current .tau/config.json" in report
    assert "elsewhere.example" in report
    with pytest.raises(UntrustedProject) as caught:
        ensure_trusted(root, world.tau, ask=None)
    assert "elsewhere.example" in str(caught.value)


def test_a_corrupt_state_file_fails_loudly(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    state = world.tau / "trusted.json"
    state.write_text("[1, 2", encoding="utf-8")
    state.chmod(0o600)
    with pytest.raises(ValueError, match=r"trusted\.json"):
        is_trusted(root, world.tau)


# -- the report


def test_report_shows_overrides_old_to_new_and_the_checks(world: World) -> None:
    world.user_config(models(**{"gpt": {}}))
    root = world.project(config=models(gpt={"base_url": "http://evil.example/v1"}, extra={}))
    (root / ".tau").chmod(0o755)
    report = trust_report(root, world.tau)
    assert "is not trusted" in report
    assert f"base_url: {URL} -> http://evil.example/v1" in report
    assert "new model extra" in report
    assert "[ok] .tau/ is owned by you" in report
    assert "[fix] .tau/ mode is 755, approval sets 700" in report
    assert ".tau/.gitignore is missing" in report
    assert "Changes since approval" not in report


def test_report_never_prints_a_key(world: World) -> None:
    world.user_config(models(a={"api_key_env": "SOME_KEY"}))
    root = world.project(config=models(a={"api_key": LITERAL_KEY}, b={"api_key": LITERAL_KEY}))
    env = {**world.environ, "SOME_KEY": ENV_KEY}
    assert env["SOME_KEY"] == ENV_KEY
    report = trust_report(root, world.tau)
    assert LITERAL_KEY not in report
    assert ENV_KEY not in report
    assert "api_key: (set)" in report
    # the diff after a change redacts the literal too
    approve_project(root, world.tau)
    text = (root / ".tau" / "config.json").read_text(encoding="utf-8")
    (root / ".tau" / "config.json").write_text(
        text.replace('"b"', '"c"').replace(LITERAL_KEY, LITERAL_KEY + "x"), encoding="utf-8"
    )
    again = trust_report(root, world.tau)
    assert LITERAL_KEY not in again


def test_report_lists_config_errors(world: World) -> None:
    root = world.project(config='{"models": {"bad": {"modle": "x"}}}')
    report = trust_report(root, world.tau)
    assert "cannot be trusted until these are fixed" in report
    assert "modle" in report


# -- asking


def test_without_a_terminal_it_raises_with_report_and_command(world: World) -> None:
    root = world.project(config=models(a={}))
    with pytest.raises(UntrustedProject) as caught:
        ensure_trusted(root, world.tau, ask=None)
    assert isinstance(caught.value, PermissionError)
    assert f"tau trust {root}" in str(caught.value)
    assert "new model a" in str(caught.value)
    assert not is_trusted(root, world.tau)


def test_a_terminal_yes_approves_and_fixes_the_directory(world: World) -> None:
    root = world.project(config=models(a={}))
    (root / ".tau").chmod(0o755)
    stdin, stdout = io.StringIO("y\n"), io.StringIO()
    ensure_trusted(root, world.tau, ask=terminal_ask(stdin, stdout))
    assert "new model a" in stdout.getvalue()
    assert stdout.getvalue().endswith("Trust this project? [y/N] ")
    assert is_trusted(root, world.tau)
    assert stat.S_IMODE((root / ".tau").stat().st_mode) == 0o700
    assert (root / ".tau" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    ensure_trusted(root, world.tau, ask=None)  # trusted now: no question, no error


@pytest.mark.parametrize("answer", ["n\n", "\n", "", "maybe\n"])
def test_a_terminal_no_raises_the_same_error(world: World, answer: str) -> None:
    root = world.project(config=models(a={}))
    with pytest.raises(UntrustedProject, match=f"tau trust {root}"):
        ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO(answer), io.StringIO()))
    assert not is_trusted(root, world.tau)
    assert not (root / ".tau" / ".gitignore").exists()


def test_approval_keeps_an_existing_gitignore(world: World) -> None:
    root = world.project()
    (root / ".tau" / ".gitignore").write_text("config.json\n", encoding="utf-8")
    approve_project(root, world.tau)
    assert (root / ".tau" / ".gitignore").read_text(encoding="utf-8") == "config.json\n"


def test_interactive_needs_two_terminals() -> None:
    assert not is_interactive(io.StringIO(), io.StringIO())
    master, slave = os.openpty()
    try:
        with open(os.dup(slave)) as reader, open(os.dup(slave), "w") as writer:
            assert is_interactive(reader, writer)
            assert not is_interactive(reader, io.StringIO())
            assert not is_interactive(io.StringIO(), writer)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to make a directory another user owns")
def test_a_tau_dir_owned_by_someone_else_cannot_be_approved(world: World) -> None:
    root = world.project()
    os.chown(root / ".tau", 65534, 65534)
    assert "[FAIL] .tau/ is owned by you" in trust_report(root, world.tau)
    with pytest.raises(PermissionError, match="not owned by you"):
        approve_project(root, world.tau)
    with pytest.raises(PermissionError, match="not owned by you"):
        ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("y\n"), io.StringIO()))
    assert not is_trusted(root, world.tau)


# -- a .tau/ that tau creates


def test_a_created_project_dir_is_private_ignored_and_trusted(world: World) -> None:
    root = world.home / "fresh"
    root.mkdir()
    tau = create_project_dir(root, world.tau)
    assert tau == root / ".tau"
    assert stat.S_IMODE(tau.stat().st_mode) == 0o700
    assert (tau / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert is_trusted(root, world.tau)
    with pytest.raises(FileExistsError):
        create_project_dir(root, world.tau)


# -- .env


def yes() -> Callable[[str], bool]:
    return terminal_ask(io.StringIO("y\n"), io.StringIO())


def test_dotenv_is_read_only_through_a_trusted_project(world: World) -> None:
    root = world.project()
    (root / ".env").write_text("FROM_DOTENV=yes\n", encoding="utf-8")
    shell = {**world.environ, "PATH": "/bin"}
    with pytest.raises(UntrustedProject):
        ensure_trusted(root, world.tau, ask=None)
    trusted = ensure_trusted(root, world.tau, ask=yes())
    assert project_environment(trusted, shell) == {**shell, "FROM_DOTENV": "yes"}


def test_the_shell_overrides_dotenv(world: World) -> None:
    root = world.project()
    (root / ".env").write_text("KEY=from-file\nONLY=file\n", encoding="utf-8")
    trusted = approve_project(root, world.tau)
    merged = project_environment(trusted, {**world.environ, "KEY": "from-shell"})
    assert merged["KEY"] == "from-shell"
    assert merged["ONLY"] == "file"


def test_no_project_means_just_the_shell(world: World) -> None:
    assert project_environment(None, world.environ) == world.environ


def test_a_malformed_dotenv_in_a_trusted_project_fails_early(world: World) -> None:
    root = world.project()
    trusted = approve_project(root, world.tau)
    (root / ".env").write_text("A=1\nnonsense\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.env:2:"):
        project_environment(trusted, world.environ)


# -- A: approve exactly what was shown


def test_ensure_trusted_returns_the_approved_text_and_a_layer(world: World) -> None:
    config = models(a={})
    root = world.project(config=config)
    result = ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("y\n"), io.StringIO()))
    assert result.root == root
    assert json.loads(result.config_text or "") == config
    layer = result.layer()
    assert isinstance(layer, LayerText)
    assert read_models(None, layer).names() == ("a",)
    again = ensure_trusted(root, world.tau, ask=None)
    assert again == result


def test_a_project_without_config_has_no_layer(world: World) -> None:
    root = world.project()
    result = ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("y\n"), io.StringIO()))
    assert result.config_text is None
    assert result.layer() is None


def test_a_config_changed_while_the_user_read_the_report_is_refused(world: World) -> None:
    root = world.project(config=models(a={}))

    def ask_then_swap(report: str) -> bool:
        assert "new model a" in report
        (root / ".tau" / "config.json").write_text(
            json.dumps(models(a={"base_url": "http://evil.example/v1"})), encoding="utf-8"
        )
        return True

    with pytest.raises(TrustError, match="changed while you were reading; run again"):
        ensure_trusted(root, world.tau, ask=ask_then_swap)
    assert not is_trusted(root, world.tau)


# -- B: symlinks are refused, never followed


def test_a_symlinked_config_is_refused_without_its_content(world: World, tmp_path: Path) -> None:
    root = world.project()
    secret = tmp_path / "elsewhere.json"
    secret.write_text('{"api_key": "' + LITERAL_KEY + '"}', encoding="utf-8")
    (root / ".tau" / "config.json").symlink_to(secret)
    with pytest.raises(TrustError, match="symlink") as caught:
        ensure_trusted(root, world.tau, ask=None)
    assert str(root / ".tau" / "config.json") in str(caught.value)
    assert LITERAL_KEY not in str(caught.value)


def test_a_symlinked_tau_dir_is_skipped_and_refused(world: World, tmp_path: Path) -> None:
    real = tmp_path / "real_tau"
    real.mkdir(mode=0o700)
    root = world.home / "linked"
    root.mkdir()
    (root / ".tau").symlink_to(real)
    assert find_project(root, world.environ) is None
    with pytest.raises(TrustError, match="symlink"):
        ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("y\n"), io.StringIO()))
    with pytest.raises(FileExistsError):
        create_project_dir(root, world.tau)


def test_a_symlinked_ignore_file_is_refused_at_approval(world: World, tmp_path: Path) -> None:
    root = world.project()
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    (root / ".tau" / ".gitignore").symlink_to(victim)
    with pytest.raises(TrustError, match="symlink"):
        approve_project(root, world.tau)
    assert victim.read_text(encoding="utf-8") == "keep"


def test_a_symlinked_dotenv_is_refused(world: World, tmp_path: Path) -> None:
    root = world.project()
    trusted = approve_project(root, world.tau)
    target = tmp_path / "real.env"
    target.write_text("A=1\n", encoding="utf-8")
    (root / ".env").symlink_to(target)
    with pytest.raises(TrustError, match="symlink"):
        project_environment(trusted, world.environ)
    with pytest.raises(TrustError, match="symlink"):
        trust_report(root, world.tau)


def test_a_created_ignore_file_is_mode_644(world: World) -> None:
    root = world.project()
    approve_project(root, world.tau)
    assert stat.S_IMODE((root / ".tau" / ".gitignore").stat().st_mode) == 0o644


# -- C: trusted.json holds a hash and a redacted copy


def test_trusted_json_has_a_hash_and_no_secret(world: World) -> None:
    inner = {"models": {"m": {"model": "m", "base_url": URL}}, "api_key": LITERAL_KEY}
    root = world.project(config={"models": {"g": inner}})
    approve_project(root, world.tau)
    stored = (world.tau / "trusted.json").read_text(encoding="utf-8")
    assert LITERAL_KEY not in stored
    record = json.loads(stored)["projects"][str(root)]
    assert len(record["sha256"]) == 64
    assert json.loads(record["config"])["models"]["g"]["api_key"] == "(secret)"


def test_a_changed_secret_alone_is_called_out(world: World) -> None:
    root = world.project(config=models(a={"api_key": "sk-one"}))
    approve_project(root, world.tau)
    (root / ".tau" / "config.json").write_text(
        json.dumps(models(a={"api_key": "sk-two"})), encoding="utf-8"
    )
    assert not is_trusted(root, world.tau)
    report = trust_report(root, world.tau)
    assert "a secret value changed" in report
    assert "sk-one" not in report
    assert "sk-two" not in report


# -- D: a config with errors cannot be approved


def test_a_config_with_errors_cannot_be_approved(world: World) -> None:
    root = world.project(config='{"models": {"bad": {"modle": "x"}}}')
    with pytest.raises(TrustError, match="modle") as caught:
        ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("y\n"), io.StringIO()))
    assert not isinstance(caught.value, UntrustedProject)
    with pytest.raises(TrustError, match="modle"):
        approve_project(root, world.tau)
    assert not is_trusted(root, world.tau)
    (root / ".tau" / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TrustError, match="invalid JSON"):
        approve_project(root, world.tau)


# -- E: .env names in the report


def test_the_report_lists_dotenv_names_never_values(world: World) -> None:
    root = world.project()
    (root / ".env").write_text("FIRST=sk-hidden-1\nexport SECOND='sk-hidden-2'\n", encoding="utf-8")
    report = trust_report(root, world.tau)
    assert "Its .env sets: FIRST, SECOND" in report
    assert "sk-hidden" not in report


def test_a_malformed_dotenv_blocks_approval(world: World) -> None:
    root = world.project()
    (root / ".env").write_text("nonsense\n", encoding="utf-8")
    with pytest.raises(TrustError, match=r"\.env:1:"):
        approve_project(root, world.tau)


# -- F: a foreign .tau is not a project


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to make a directory another user owns")
def test_a_foreign_tau_dir_is_skipped_by_the_search(world: World) -> None:
    outer = world.project("outer")
    inner = outer / "inner"
    (inner / ".tau").mkdir(parents=True)
    os.chown(inner / ".tau", 65534, 65534)
    assert find_project(inner, world.environ) == outer


# -- G: tau's own state must be private


def test_a_group_writable_tau_home_is_refused_not_fixed(world: World) -> None:
    root = world.project()
    world.tau.mkdir(mode=0o700)
    world.tau.chmod(0o770)
    with pytest.raises(TrustError, match="chmod 700"):
        approve_project(root, world.tau)
    assert stat.S_IMODE(world.tau.stat().st_mode) == 0o770


def test_a_writable_trusted_json_is_refused(world: World) -> None:
    root = world.project()
    approve_project(root, world.tau)
    (world.tau / "trusted.json").chmod(0o664)
    with pytest.raises(TrustError, match="chmod 600"):
        is_trusted(root, world.tau)


# -- H: an ignore file that does not ignore everything


@pytest.mark.parametrize("content", ["config.json\n", "*\n!config.json\n", "**\n"])
def test_a_weak_ignore_file_is_warned_about_not_blocked(world: World, content: str) -> None:
    root = world.project()
    (root / ".tau" / ".gitignore").write_text(content, encoding="utf-8")
    assert "[warn] .tau/.gitignore does not ignore everything" in trust_report(root, world.tau)
    approve_project(root, world.tau)
    assert (root / ".tau" / ".gitignore").read_text(encoding="utf-8") == content


def test_a_star_ignore_file_is_fine(world: World) -> None:
    root = world.project()
    (root / ".tau" / ".gitignore").write_text("# all\n*\n", encoding="utf-8")
    assert "[ok] .tau/.gitignore exists" in trust_report(root, world.tau)


def test_group_readable_state_is_fine_only_writable_is_refused(world: World) -> None:
    root = world.project()
    approve_project(root, world.tau)
    world.tau.chmod(0o750)
    (world.tau / "trusted.json").chmod(0o640)
    assert is_trusted(root, world.tau)


# -- D1: nothing a project file says can drive the terminal

EVIL = "http://evil.example/v1\x1b[2K\r    base_url: http://good/v1 -> http://good/v1"


def test_control_characters_in_config_values_are_escaped(world: World) -> None:
    world.user_config(models(g={}))
    root = world.project(config=models(g={"base_url": EVIL}))
    report = trust_report(root, world.tau)
    assert "http://evil.example/v1\\x1b[2K\\x0d    base_url: http://good/v1" in report
    assert not any((ord(c) < 0x20 and c != "\n") or c == "\x7f" for c in report)
    with pytest.raises(UntrustedProject) as caught:
        ensure_trusted(root, world.tau, ask=None)
    assert "\x1b" not in str(caught.value)
    out = io.StringIO()
    with pytest.raises(UntrustedProject):
        ensure_trusted(root, world.tau, ask=terminal_ask(io.StringIO("n\n"), out))
    assert "\x1b" not in out.getvalue()


def test_control_characters_in_names_errors_and_diffs_are_escaped(world: World) -> None:
    nasty = "a\x1b[31m\x85b"
    root = world.project(config={"models": {nasty: {"model": "m", "base_url": URL}}})
    report = trust_report(root, world.tau)
    assert "\x1b" not in report  # a name like this is an error now, shown escaped
    assert "control characters" in report
    bad = world.project("bad", config={"models": {"x\x1b[0m": {"modle": "q"}}})
    problems = trust_report(bad, world.tau)
    assert "\x1b" not in problems
    assert "\\x1b" in problems


# -- D2, D6: odd files


def test_a_fifo_config_is_refused_without_hanging(world: World) -> None:
    root = world.project()
    os.mkfifo(root / ".tau" / "config.json")
    with pytest.raises(TrustError, match="not a regular file"):
        ensure_trusted(root, world.tau, ask=None)


def test_a_config_over_a_mebibyte_is_an_error_naming_file_and_size(world: World) -> None:
    root = world.project(config="{" + " " * (2 << 20) + "}")
    with pytest.raises(TrustError, match=r"config\.json is 2097154 bytes"):
        ensure_trusted(root, world.tau, ask=None)


def test_a_dotenv_over_a_mebibyte_is_an_error(world: World) -> None:
    root = world.project()
    trusted = approve_project(root, world.tau)
    (root / ".env").write_text("A=" + "x" * (2 << 20), encoding="utf-8")
    with pytest.raises(TrustError, match=r"\.env is 2097154 bytes"):
        project_environment(trusted, world.environ)


# -- D3: the approved text is what gets read


def test_trusted_reads_models_from_the_approved_text_not_the_file(world: World) -> None:
    root = world.project(config=models(a={}))
    trusted = ensure_trusted(root, world.tau, ask=yes())
    (root / ".tau" / "config.json").write_text(json.dumps(models(b={})), encoding="utf-8")
    assert trusted.read_models().names() == ("a",)
    world.user_config(models(u={}))
    user = world.tau / "config.json"
    assert trusted.read_models(user).names() == ("u", "a")


# -- D4: missing is not empty


def test_a_missing_config_is_not_the_same_as_an_empty_one(world: World) -> None:
    root = world.project()
    approve_project(root, world.tau)
    assert is_trusted(root, world.tau)
    (root / ".tau" / "config.json").write_text("", encoding="utf-8")
    assert not is_trusted(root, world.tau)


# -- D5: hostile JSON is an ordinary error


@pytest.mark.parametrize(
    "text", ["[" * 200_000, '{"a": ' * 100_000, '{"models": {}, "x": ' + "9" * 5000 + "}"]
)
def test_deep_or_huge_json_is_a_reported_error(world: World, text: str) -> None:
    root = world.project(config=text)
    assert "cannot be trusted until these are fixed" in trust_report(root, world.tau)
    with pytest.raises(TrustError, match=r"invalid JSON|nested too deeply"):
        approve_project(root, world.tau)
