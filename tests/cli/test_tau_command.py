"""The `tau` command, driven through `main` with real files and real streams."""

import io
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from world import World, models

from tau_next import check_config, is_trusted, load_config
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
    assert "usage: tau" in result.out
    assert "config" in result.out
    assert "trust" in result.out


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


def test_check_of_a_sound_untrusted_project_exits_0_and_does_not_use_it(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "config", "check", cwd=root) == Result(0, "", "")
    shown = tau(world, "config", "check", "--show", "p", cwd=root)
    assert shown.code == 1
    assert "tau trust" in shown.err
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
    assert "modle" in result.out
    assert "cannot be trusted" in result.err
    assert not is_trusted(root, world.tau)


def test_trust_without_a_project_says_so(world: World) -> None:
    result = tau(world, "trust")
    assert result.code == 1
    assert "no project" in result.err
    assert "Traceback" not in result.err


@pytest.mark.parametrize("argv", [("config",), ("nonsense",)])
def test_a_usage_error_exits_2(world: World, argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as stop:
        tau(world, *argv)
    assert stop.value.code == 2


def test_config_json_in_the_examples_is_sound() -> None:
    environ = {"HOME": "/nonexistent-home", "EXAMPLE_API_KEY": "x"}
    sample_file = REPO / "examples" / "config.json"
    assert check_config(sample_file, environ=environ) == ()
    assert json.loads(sample_file.read_text(encoding="utf-8"))["default_model"] == "local:qwen"
