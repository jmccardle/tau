"""The `tau` command, driven through `main` with real files and real streams."""

import io
import json
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from world import PROVIDERS, URL, Result, World, loaded, models, tau, trust

from tau import TrustError, check_config, load_config
from tau.cli import main

LITERAL = "sk-literal-secret-9f3"
REPO = Path(__file__).resolve().parents[2]
ESC = "\x1b"


def test_no_subcommand_is_the_terminal_ui_which_needs_a_terminal(world: World) -> None:
    result = tau(world)
    assert result.code == 2
    assert result.out == ""
    assert "needs a terminal" in result.err
    assert "tau -p" in result.err


def test_config_alone_prints_the_help_for_config(world: World) -> None:
    result = tau(world, "config")
    assert result.code == 2
    assert result.out == ""
    assert "usage: tau config" in result.err
    assert "check" in result.err


@pytest.mark.parametrize("argv", [("nonsense",), ("trust", "a", "b")])
def test_a_usage_error_exits_2(world: World, argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as stop:
        tau(world, *argv)
    assert stop.value.code == 2


# -- tau config check


def test_check_of_sound_config_or_none_at_all_is_silent_and_exits_0(world: World) -> None:
    assert tau(world, "config", "check") == Result(0, "", "")
    world.user_config(models(a={}))
    assert tau(world, "config", "check") == Result(0, "", "")


def test_check_reports_line_and_column_and_exits_1(world: World) -> None:
    path = world.user_config('{\n  "models": {\n    "a": oops\n  }\n}\n')
    result = tau(world, "config", "check")
    assert result.code == 1
    assert result.out.startswith(f"{path}:3:")
    assert ": error: " in result.out


def test_check_reports_the_options_of_the_extensions_it_knows(world: World) -> None:
    world.user_config({**models(a={}), "extensions": {"step_limit": {"limit": 0}}})
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "extensions.step_limit: " in result.out
    assert "at least 1" in result.out
    world.user_config({**models(a={}), "extensions": {"step_limit": {"limit": "ten"}}})
    assert "option 'limit' must be integer" in tau(world, "config", "check").out
    world.user_config({**models(a={}), "extensions": {"step_limit": {"limit": 10}, "mine": {}}})
    assert tau(world, "config", "check") == Result(
        0,
        "config: warning: extensions.mine: no loaded extension is named 'mine'; "
        "its options are ignored\n",
        "",
    ), "options of an extension nothing loads are a warning"


def test_check_reports_the_options_of_an_extension_that_is_not_an_object(world: World) -> None:
    world.user_config({**models(a={}), "extensions": 3})
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "extensions: 'extensions' must be an object" in result.out
    world.user_config({**models(a={}), "extensions": {"step_limit": 3}})
    assert (
        "extensions.step_limit: 'extensions.step_limit' must be an object"
        in tau(world, "config", "check").out
    )


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
    assert tau(world, "config", "check", cwd=world.project("empty")).code == 1  # no config file


def test_check_does_not_label_a_trusted_project(world: World) -> None:
    root = world.project(config=models(p={"api_key": LITERAL}))
    trust(world, root)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 0
    assert "warning" in result.out
    assert "untrusted" not in result.out


def test_check_warns_about_an_unset_key_variable_unless_the_dotenv_sets_it(world: World) -> None:
    root = world.project(config=models(p={"api_key_env": "P_KEY"}))
    trust(world, root)
    assert "P_KEY" in tau(world, "config", "check", cwd=root).out
    (root / ".env").write_text("P_KEY=abc\n", encoding="utf-8")
    trust(world, root)  # approval pins the .env too
    assert tau(world, "config", "check", cwd=root).out == ""


def test_check_reports_a_symlinked_project_config(world: World) -> None:
    root = world.project()
    target = world.home / "elsewhere"
    target.write_text("{}", encoding="utf-8")
    (root / ".tau" / "config.json").symlink_to(target)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert "symlink" in result.out
    assert "Traceback" not in result.err


def test_the_example_config_is_sound() -> None:
    environ = {
        "HOME": "/nonexistent-home",
        "EXAMPLE_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "GEMINI_API_KEY": "x",
    }
    sample_file = REPO / "examples" / "config.json"
    assert check_config(sample_file, environ=environ, providers=PROVIDERS) == ()
    assert json.loads(sample_file.read_text(encoding="utf-8"))["default_model"] == "local:qwen"


# -- tau config check --show


def sample(world: World) -> None:
    world.user_config(
        {
            "models": {
                "local": {
                    "base_url": "http://llama.test:8100/v1",
                    "api_key": LITERAL,
                    "models": {"qwen": {"model": "/models/qwen.gguf"}},
                },
                "hosted": {
                    "api_key_env": "HOSTED_KEY",
                    "base_url": "https://h.example/v1",
                    "models": {"qwen": {"model": "q"}},
                },
            }
        }
    )


def test_show_prints_each_value_with_its_source_and_group_and_never_a_key(world: World) -> None:
    sample(world)
    path = world.tau / "config.json"
    result = tau(world, "config", "check", "--show", "local:qwen")
    assert result.code == 0
    assert "local:qwen" in result.out.splitlines()
    assert f"model        /models/qwen.gguf  [{path}]" in result.out
    assert f"base_url     http://llama.test:8100/v1  [{path}, group local]" in result.out
    assert "provider     openai  [default]" in result.out
    assert "api_key      (set)" in result.out
    assert LITERAL not in result.both


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
    assert loaded(world, root).catalog.names() == ("p",)


def test_trust_walks_up_from_a_directory_given_absolute_or_relative(world: World) -> None:
    root = world.project(config=models(p={}))
    (root / "src" / "deep").mkdir(parents=True)
    assert tau(world, "trust", "src", cwd=root).code == 0
    assert loaded(world, root).catalog.names() == ("p",)
    other = world.project("other", models(q={}))
    (other / "x").mkdir()
    assert tau(world, "trust", str(other / "x")).code == 0
    assert loaded(world, other).catalog.names() == ("q",)


def test_trust_refuses_a_config_with_errors_and_the_report_travels_with_the_refusal(
    world: World,
) -> None:
    root = world.project(config=models(p={"modle": "x"}))
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert result.out == ""
    assert "modle" in result.err
    assert result.err.count("\n") > 3  # a multi-line refusal stays multi-line
    with pytest.raises(TrustError, match="modle"):
        loaded(world, root)


def test_trust_of_a_missing_directory_says_so_in_one_line(world: World) -> None:
    missing = tau(world, "trust", "nowhere")
    assert missing.code == 1
    assert missing.err.count("\n") == 1
    assert "not a directory" in missing.err
    assert "Traceback" not in missing.err


def test_trust_twice_says_already_trusted_and_a_change_asks_again(world: World) -> None:
    root = world.project(config=models(p={}))
    assert tau(world, "trust", cwd=root).code == 0
    assert tau(world, "trust", cwd=root) == Result(0, f"already trusted: {root}\n", "")
    (root / ".tau" / "config.json").write_text(json.dumps(models(q={})), encoding="utf-8")
    again = tau(world, "trust", cwd=root)
    assert again.code == 0
    assert "already trusted" not in again.out


# -- special directories are never trusted


def refused(world: World, target: Path, tau_home: Path | None = None) -> Result:
    environ = world.environ if tau_home is None else {**world.environ, "TAU_HOME": str(tau_home)}
    return tau(replace(world, environ=environ), "trust", str(target))


def test_trust_refuses_home_tau_home_and_what_is_found_inside_tau_home(world: World) -> None:
    (world.tau / ".tau").mkdir(parents=True, mode=0o700)
    sub = world.tau / "sub"
    sub.mkdir()
    for target in (world.home, world.tau, sub):
        result = refused(world, target)
        assert result.code == 1
        assert "refusing to trust" in result.err
    assert not (world.tau / "trusted.json").exists()


def test_the_parent_of_tau_home_is_refused_only_when_tau_home_is_named_dot_tau(
    world: World,
) -> None:
    named = world.home / "cfg" / ".tau"
    named.mkdir(parents=True, mode=0o700)
    result = refused(world, world.home / "cfg", tau_home=named)
    assert result.code == 1
    assert "the parent of TAU_HOME" in result.err
    other = world.home / "cfg2" / "tau"
    other.mkdir(parents=True, mode=0o700)
    (world.home / "cfg2" / ".tau").mkdir()  # a project here, since TAU_HOME is not named .tau
    assert "refusing" not in refused(world, world.home / "cfg2", tau_home=other).err


# -- what reaches the terminal is escaped


def test_check_and_show_escape_control_characters_in_paths_values_and_suggestions(
    world: World,
) -> None:
    world.user_config(
        {
            "models": {
                "a": {"model": "m" + ESC + "[2J", "base_url": URL, "mod" + ESC + "[2Jl": "x"},
                "b" + ESC + "[31m": {"model": "m", "base_url": URL},
            }
        }
    )
    result = tau(world, "config", "check")
    assert result.code == 1
    assert ESC not in result.both
    assert "\\x1b" in result.out
    world.user_config(models(a={"model": "m" + ESC + "[2J"}))
    shown = tau(world, "config", "check", "--show", "a")
    assert shown.code == 0
    assert ESC not in shown.out
    assert "m\\x1b[2J" in shown.out
    missing = tau(world, "config", "check", "--show", "a" + ESC + "[2J")
    assert missing.code == 1
    assert ESC not in missing.both


def test_trust_escapes_every_error_whatever_its_source_and_directory_names(world: World) -> None:
    root = world.project(config=models(p={}))
    (root / ".env").write_text("BAD" + ESC + "LINE\n", encoding="utf-8")
    result = tau(world, "trust", cwd=root)
    assert result.code == 1
    assert ESC not in result.both
    # A name that is not UTF-8. APFS (macOS) refuses one (EILSEQ): there, a name with an escape
    # character stands in, so the rest of the test still runs.
    odd_name, escaped = (
        ("np" + ESC + "[2J", "np\\x1b[2J")
        if sys.platform == "darwin"
        else (os.fsdecode(b"np\xff"), "np\\xff")
    )
    odd = world.home / odd_name
    (odd / ".tau").mkdir(parents=True)
    (odd / ".tau" / "config.json").write_text(
        json.dumps(models(p={"base_url": "http://h/v1\u202e\U000e0041"})), encoding="utf-8"
    )
    trusted = tau(world, "trust", cwd=odd)
    assert trusted.code == 0
    trusted.out.encode("utf-8")  # strictly encodable
    assert escaped in trusted.out
    assert "\u202e" not in trusted.out
    assert "\\u202e" in trusted.out
    checked = tau(world, "config", "check", cwd=odd)
    (checked.out + checked.err).encode("utf-8")


def test_an_error_cannot_forge_a_second_tau_line(world: World) -> None:
    result = tau(world, "trust", "nodir\rtau: trusted /etc")
    assert result.code == 1
    assert result.err.count("\n") == 1
    assert result.err.startswith("tau: ")
    assert "\r" not in result.err


# -- startup refusals show up in check


def test_a_writable_trusted_json_is_reported_under_its_own_path(world: World) -> None:
    root = world.project(config=models(p={}))
    trust(world, root)
    state = world.tau / "trusted.json"
    state.chmod(0o666)
    result = tau(world, "config", "check", cwd=root)
    assert result.code == 1
    assert result.out.startswith(f"{state}: error: ")
    with pytest.raises(PermissionError):
        load_config(root, world.environ, ask=None)


# -- interruption


@pytest.mark.skipif(sys.platform == "win32", reason="Windows: no pseudo-terminals")
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


# -- providers check their own settings


def mock_model(**settings: object) -> dict[str, object]:
    return {"models": {"m": {"model": "m", "provider": "mock", **settings}}}


@pytest.mark.parametrize(
    ("settings", "complaint"),
    [
        ({"options": {"reply": "hi", "delay_ms": 0}, "max_tokens": 3}, None),
        ({"options": {"delay_ms": 1.5}}, "'delay_ms' must be a non-negative integer"),
        ({"options": {"delay_ms": -1}}, "'delay_ms' must be a non-negative integer"),
        ({"options": {"reply": ""}}, "'reply' must be a non-empty string"),
        ({"options": {"reply": 7}}, "'reply' must be a non-empty string"),
        ({"options": {"replly": "x"}}, "unknown option 'replly'; known options are"),
        ({"base_url": URL}, "has no use for base_url"),
    ],
)
def test_check_asks_the_mock_provider_about_its_settings(
    world: World, settings: dict[str, object], complaint: str | None
) -> None:
    world.user_config(mock_model(**settings))
    result = tau(world, "config", "check")
    if complaint is None:
        assert result == Result(0, "", "")
    else:
        assert result.code == 1
        assert complaint in result.out
        assert "model 'm' (mock)" in result.out


def test_check_asks_the_openai_provider_for_its_base_url_and_refuses_options(
    world: World,
) -> None:
    world.user_config({"models": {"m": {"model": "m", "options": {"top_k": 3}}}})
    result = tau(world, "config", "check")
    assert result.code == 1
    assert "needs a base_url" in result.out
    assert "unknown option 'top_k'" in result.out


def test_check_show_prints_max_tokens_and_options(world: World) -> None:
    world.user_config(mock_model(max_tokens=3, options={"reply": "a b"}))
    result = tau(world, "config", "check", "--show", "m")
    assert result.code == 0
    assert "max_tokens   3  [" in result.out
    assert 'options      {"reply": "a b"}  [' in result.out
