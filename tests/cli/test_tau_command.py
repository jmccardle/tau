"""The `tau` command, driven through `main` with real files and real streams."""

import io
import json
import os
import signal
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from world import URL, World, models

from tau_next import check_config, is_trusted, load_config, safe_block, safe_text
from tau_next.cli import main

LITERAL = "sk-literal-secret-9f3"
REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


def tau(world: World, *argv: str, cwd: Path | None = None, stdin: str = "") -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        environ=world.environ,
        cwd=cwd or world.home,
    )
    return Result(code, out.getvalue(), err.getvalue())


def test_the_tau_console_script_is_declared() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["tau"] == "tau_next.cli:main"


def test_no_subcommand_prints_help_and_exits_2(world: World) -> None:
    result = tau(world)
    assert result.code == 2
    assert result.out == ""
    assert "usage: tau" in result.err
    assert "config" in result.err
    assert "trust" in result.err


def test_config_alone_prints_the_help_for_config(world: World) -> None:
    result = tau(world, "config")
    assert result.code == 2
    assert result.out == ""
    assert "usage: tau config" in result.err
    assert "check" in result.err
    assert "config_command" not in result.err


# -- tau config check


def test_check_of_sound_config_is_silent_and_exits_0(world: World) -> None:
    world.user_config(models(a={}))
    assert tau(world, "config", "check") == Result(0, "", "")


def test_check_with_no_config_at_all_exits_0(world: World) -> None:
    assert tau(world, "config", "check").code == 0


def test_check_reports_line_and_column_and_exits_1(world: World) -> None:
    path = world.user_config('{\n  "models": {\n    "a": oops\n  }\n}\n')
    result = tau(world, "config", "check")
    assert result.code == 1
    assert result.out.startswith(f"{path}:3:")
    assert ": error: " in result.out


def test_check_reports_a_problem_with_a_path_and_a_suggestion(world: World) -> None:
    path = world.user_config(models(a={"modle": "x"}))
    result = tau(world, "config", "check")
    assert result.code == 1
    assert f"{path}: error: models.a.modle: " in result.out
    assert "model" in result.out


def test_check_prints_warnings_and_exits_0(world: World) -> None:
    world.user_config(models(a={"api_key": LITERAL}))
    result = tau(world, "config", "check")
    assert result.code == 0
    assert "warning" in result.out
    assert LITERAL not in result.out + result.err


def test_check_labels_an_untrusted_projects_problems(world: World) -> None:
    root = world.project(config=models(p={"modle": "x"}))
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert str(root / ".tau" / "config.json") in result.out
    assert "[untrusted project" in result.out


def test_check_of_a_sound_untrusted_project_fails_because_startup_would(world: World) -> None:
    root = world.project(config=models(p={}))
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert f"{root / '.tau'}: error: project is not trusted" in result.out
    shown = tau(world, "config", "check", "--show", "p", cwd=root)
    assert shown.code == 1
    assert "tau trust" in shown.out
    assert "Traceback" not in shown.err


def test_check_does_not_label_a_trusted_project(world: World) -> None:
    root = world.project(config=models(p={"api_key": LITERAL}))
    assert tau(world, "trust", cwd=root).code == 0
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 0
    assert "warning" in result.out
    assert "untrusted" not in result.out


def test_check_warns_about_an_unset_key_variable_unless_the_dotenv_sets_it(world: World) -> None:
    root = world.project(config=models(p={"api_key_env": "P_KEY"}))
    assert tau(world, "trust", cwd=root).code == 0
    assert "P_KEY" in tau(world, "config", "check", cwd=root).out
    (root / ".env").write_text("P_KEY=abc\n", encoding="utf-8")
    assert tau(world, "config", "check", cwd=root).out == ""


# -- tau config check --show


def sample(world: World) -> None:
    world.user_config(
        {
            "models": {
                "local": {
                    "base_url": "http://192.168.1.100:8100/v1",
                    "api_key": LITERAL,
                    "models": {"qwen": {"model": "/fast/qwen.gguf"}},
                },
                "hosted": {
                    "api_key_env": "HOSTED_KEY",
                    "base_url": "https://h.example/v1",
                    "models": {"qwen": {"model": "q"}},
                },
            }
        }
    )


def test_show_prints_each_value_with_its_source_and_group(world: World) -> None:
    sample(world)
    path = world.tau / "config.json"
    result = tau(world, "config", "check", "--show", "local:qwen")
    assert result.code == 0
    lines = result.out.splitlines()
    assert "local:qwen" in lines
    assert f"model        /fast/qwen.gguf  [{path}]" in result.out
    assert f"base_url     http://192.168.1.100:8100/v1  [{path}, group local]" in result.out
    assert "provider     openai  [default]" in result.out


def test_show_never_prints_a_key(world: World) -> None:
    sample(world)
    result = tau(world, "config", "check", "--show", "local:qwen")
    assert "api_key      (set)" in result.out
    assert LITERAL not in result.out + result.err


def test_show_a_bare_name_that_is_ambiguous_fails_with_the_list(world: World) -> None:
    sample(world)
    result = tau(world, "config", "check", "--show", "qwen")
    assert result.code == 1
    assert "local:qwen" in result.err
    assert "hosted:qwen" in result.err


def test_show_an_unknown_name_suggests(world: World) -> None:
    sample(world)
    result = tau(world, "config", "check", "--show", "local:qwne")
    assert result.code == 1
    assert "did you mean local:qwen" in result.err


def test_show_does_not_run_after_a_failed_check(world: World) -> None:
    world.user_config(models(a={"modle": "x"}))
    result = tau(world, "config", "check", "--show", "a")
    assert result.code == 1
    assert result.out.count("\n") == 1


# -- tau trust


def test_trust_prints_the_report_approves_and_load_config_then_works(world: World) -> None:
    root = world.project(config=models(p={"api_key": LITERAL}))
    (root / ".env").write_text("K=v\n", encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 0
    assert result.err == ""
    assert "K" in result.out
    assert LITERAL not in result.out
    assert result.out.rstrip().endswith(f"trusted {root}")
    assert is_trusted(root, world.tau)
    assert load_config(root, world.environ, ask=None).catalog.names() == ("p",)


def test_trust_takes_a_directory_argument(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "trust", str(root)).code == 0
    assert is_trusted(root, world.tau)


def test_trust_refuses_a_config_with_errors(world: World) -> None:
    root = world.project(config=models(p={"modle": "x"}))
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert result.out == ""
    assert "modle" in result.err  # the report travels with the refusal
    assert "Its config has errors" in result.err or "error" in result.err
    assert not is_trusted(root, world.tau)


def test_trust_without_a_project_says_so(world: World) -> None:
    work = world.home / "work"
    work.mkdir()
    result = tau(world, "trust", cwd=work)
    assert result.code == 1
    assert "no project" in result.err
    assert "Traceback" not in result.err


@pytest.mark.parametrize("argv", [("nonsense",), ("trust", "a", "b")])
def test_a_usage_error_exits_2(world: World, argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as stop:
        tau(world, *argv)
    assert stop.value.code == 2


def test_config_json_in_the_examples_is_sound() -> None:
    environ = {"HOME": "/nonexistent-home", "EXAMPLE_API_KEY": "x"}
    sample_file = REPO / "examples" / "config.json"
    assert check_config(sample_file, environ=environ) == ()
    assert json.loads(sample_file.read_text(encoding="utf-8"))["default_model"] == "local:qwen"


# -- control characters never reach the terminal

ESC = "\x1b"


def test_check_escapes_control_characters_in_paths_values_and_messages(world: World) -> None:
    world.user_config(
        {
            "models": {
                "a": {"model": "m", "base_url": URL, "mod" + ESC + "[2Jl": "x"},
                "b" + ESC + "[31m": {"model": "m", "base_url": URL},
            }
        }
    )
    result = tau(world, "config", "check")
    assert result.code == 1
    assert ESC not in result.out + result.err
    assert "\\x1b" in result.out
    assert "control characters" in result.out  # names are identifiers


def test_show_escapes_values_and_suggestions(world: World) -> None:
    world.user_config(models(a={"model": "m" + ESC + "[2J"}))
    shown = tau(world, "config", "check", "--show", "a")
    assert shown.code == 0
    assert ESC not in shown.out
    assert "m\\x1b[2J" in shown.out
    missing = tau(world, "config", "check", "--show", "a" + ESC + "[2J")
    assert missing.code == 1
    assert ESC not in missing.out + missing.err


def test_errors_are_escaped_whatever_their_source(world: World) -> None:
    root = world.project(config=models(p={}))
    (root / ".env").write_text("BAD" + ESC + "LINE\n", encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert ESC not in result.out + result.err


# -- tau trust DIR


def test_trust_walks_up_from_dir_like_no_argument(world: World) -> None:
    root = world.project(config=models(p={}))
    deep = root / "src" / "deep"
    deep.mkdir(parents=True)
    assert tau(world, "trust", str(deep)).code == 0
    assert is_trusted(root, world.tau)


def test_trust_resolves_a_relative_dir_against_cwd(world: World) -> None:
    root = world.project(config=models(p={}))
    (root / "src").mkdir()
    assert tau(world, "trust", "src", cwd=root).code == 0
    assert is_trusted(root, world.tau)


def test_trust_of_a_missing_directory_is_one_line(world: World) -> None:
    result = tau(world, "trust", "nowhere")
    assert result.code == 1
    assert result.err.count("\n") == 1
    assert "not a directory" in result.err


def test_trust_twice_says_already_trusted(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "trust", cwd=root).code == 0
    again = tau(world, "trust", cwd=root)
    assert again == Result(0, f"already trusted: {root}\n", "")


def test_trust_after_a_change_asks_again(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "trust", cwd=root).code == 0
    (root / ".tau" / "config.json").write_text(json.dumps(models(q={})), encoding="utf-8")
    again = tau(world, "trust", cwd=root)
    assert again.code == 0
    assert "trusted" in again.out
    assert "already" not in again.out


def test_trust_refuses_home_tau_home_and_its_parent(world: World) -> None:
    world.tau.mkdir(mode=0o700)
    for target in (world.home, world.tau):
        result = tau(world, "trust", str(target))
        assert result.code == 1
        assert "refusing to trust" in result.err
    elsewhere = {**world.environ, "TAU_HOME": str(world.home / "cfg" / ".tau")}
    (world.home / "cfg" / ".tau").mkdir(parents=True, mode=0o700)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["trust", str(world.home / "cfg")],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        environ=elsewhere,
        cwd=world.home,
    )
    assert code == 1
    assert "the parent of TAU_HOME" in err.getvalue()


def test_a_refused_trust_prints_the_report_with_the_refusal(world: World) -> None:
    root = world.project(config=models(p={"modle": "x"}))
    result = tau(world, "trust", cwd=root)
    assert result.out == ""
    assert "Checks:" in result.err or "modle" in result.err


# -- config check: everything that would make startup refuse


def test_check_reports_an_untrusted_project_without_a_config_file(world: World) -> None:
    root = world.project()
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert "project is not trusted" in result.out


@pytest.mark.parametrize("trust_first", [True, False])
def test_check_reports_a_malformed_dotenv_trusted_or_not(world: World, trust_first: bool) -> None:
    root = world.project(config=models(p={}))
    if trust_first:
        assert tau(world, "trust", cwd=root).code == 0
    (root / ".env").write_text("this is not a dotenv line\n", encoding="utf-8")
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert f"{root / '.env'}: error: " in result.out


def test_check_reports_a_symlinked_dotenv_and_a_symlinked_config(world: World) -> None:
    root = world.project(config=models(p={}))
    target = world.home / "elsewhere"
    target.write_text("K=v\n", encoding="utf-8")
    (root / ".env").symlink_to(target)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert "symlink" in result.out
    (root / ".tau" / "config.json").unlink()
    (root / ".tau" / "config.json").symlink_to(target)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert "symlink" in result.out
    assert "Traceback" not in result.err


# -- interruption, end of input, empty names


def test_ctrl_c_at_a_prompt_exits_130_without_a_traceback(world: World) -> None:
    sample(world)
    master, slave = os.openpty()
    err = io.StringIO()
    with (
        os.fdopen(os.dup(slave), "w") as tty_out,
        os.fdopen(slave, "r") as tty_in,
        os.fdopen(master, "w"),
    ):
        timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT))
        timer.start()
        try:
            code = main(
                ["config", "check", "--show", "qwen"],
                stdin=tty_in,
                stdout=tty_out,
                stderr=err,
                environ=world.environ,
                cwd=world.home,
            )
        finally:
            timer.cancel()
    assert code == 130
    assert "Traceback" not in err.getvalue()
    assert "interrupted" in err.getvalue()


def test_an_empty_model_name_is_an_error_not_a_fallback(world: World) -> None:
    world.user_config({**models(a={}), "default_model": "a"})
    result = tau(world, "config", "check", "--show", "")
    assert result.code == 1
    assert "must not be empty" in result.err


# -- the second review


def run_main(
    world: World, argv: list[str], cwd: Path, environ: dict[str, str] | None = None
) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv,
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        environ=environ or world.environ,
        cwd=cwd,
    )
    return Result(code, out.getvalue(), err.getvalue())


def test_safe_block_keeps_written_lines_and_escapes_everything_else() -> None:
    assert safe_block("one\ntwo") == "one\ntwo"
    assert safe_block("a\rb\x0bc\x85d\u2028e") == "a\\x0db\\x0bc\\x85d\\u2028e"
    assert safe_text("x\u202ey") == "x\\u202ey"  # right-to-left override
    assert safe_text("\U000e0041") == "\\U000e0041"  # a tag character (format)
    assert safe_text("n" + os.fsdecode(b"\xff")) == "n\\xff"  # a byte that was not UTF-8


def test_an_error_cannot_forge_a_second_line(world: World) -> None:
    result = tau(world, "trust", "nodir\rtau: trusted /etc")
    assert result.code == 1
    assert result.err.count("\n") == 1
    assert result.err.startswith("tau: ")
    assert "\r" not in result.err


def test_a_multi_line_error_stays_multi_line(world: World) -> None:
    root = world.project(config=models(p={"modle": "x"}))
    result = tau(world, "trust", cwd=root)
    assert result.err.count("\n") > 3


def test_a_non_utf8_directory_name_does_not_crash_trust(world: World) -> None:
    root = world.home / os.fsdecode(b"np\xff")
    (root / ".tau").mkdir(parents=True)
    (root / ".tau" / "config.json").write_text(json.dumps(models(p={})), encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 0
    result.out.encode("utf-8")  # strictly encodable
    assert "np\\xff" in result.out
    assert is_trusted(root, world.tau)


def test_a_non_utf8_directory_name_does_not_crash_check(world: World) -> None:
    root = world.home / os.fsdecode(b"nq\xfe")
    (root / ".tau").mkdir(parents=True)
    (root / ".tau" / "config.json").write_text(json.dumps(models(p={})), encoding="utf-8")
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    (result.out + result.err).encode("utf-8")
    assert "nq\\xfe" in result.out


def test_trust_refuses_a_project_inside_tau_home_found_by_walking_up(world: World) -> None:
    (world.tau / ".tau").mkdir(parents=True, mode=0o700)
    sub = world.tau / "sub"
    sub.mkdir()
    result = tau(world, "trust", str(sub))
    assert result.code == 1
    assert "refusing to trust" in result.err
    assert not (world.tau / "trusted.json").exists()


def test_the_parent_of_tau_home_is_refused_only_when_tau_home_is_named_dot_tau(
    world: World,
) -> None:
    other = world.home / "cfg" / "tau"
    other.mkdir(parents=True, mode=0o700)
    (world.home / "cfg" / ".tau").mkdir()
    environ = {**world.environ, "TAU_HOME": str(other)}
    result = run_main(world, ["trust", str(world.home / "cfg")], world.home, environ)
    assert "refusing" not in result.err  # cfg/.tau is a project here, not TAU_HOME


def test_a_writable_trusted_json_is_reported_under_its_own_path(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "trust", cwd=root).code == 0
    state = world.tau / "trusted.json"
    state.chmod(0o666)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert result.out.startswith(f"{state}: error: ")
