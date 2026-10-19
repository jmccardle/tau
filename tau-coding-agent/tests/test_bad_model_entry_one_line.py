"""A malformed model entry is fatal only when selected (#65, docs/SILENT-DEFAULTS.md §8).

The selected entry stops a head in one ``tau: error:`` line, exit 2. Any other
bad entry is one warning at startup, is listed by ``get_models`` with its
error, and is refused if a run switches to it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.tau_home import HOME_ENV
from tau_agent_core.testing import child_env
from tau_coding_agent.backends import model_entry_errors
from tau_coding_agent.config import ConfigError, ModelConfigError
from tau_coding_agent.headless import check_model_entries

_GOOD = {"backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "x"}
_MODELS = {"good": _GOOD, "old": {**_GOOD, "prompt_cache": "anthropic"}}
_ERROR = "models.old.prompt_cache is a boolean; got 'anthropic'"
_FULL_ERROR = (
    f"{_ERROR}. The dialect moved to prompt_cache_dialect: 'anthropic', "
    "and 'off' is prompt_cache: false"
)


def _home(tmp_path: Path, default_model: str, **extra: Any) -> Path:
    home = tmp_path / "tau-home"
    home.mkdir(exist_ok=True)
    config = {"models": _MODELS, "default_model": default_model, **extra}
    (home / "config.json").write_text(json.dumps(config))
    return home


def _env(tmp_path: Path, home: Path) -> dict[str, str]:
    return child_env(tmp_path / "home", **{HOME_ENV: str(home), "TMPDIR": str(tmp_path)})


def _run(
    tmp_path: Path,
    *argv: str,
    default_model: str = "good",
    stdin: str | None = None,
    **extra: Any,
) -> subprocess.CompletedProcess[str]:
    home = _home(tmp_path, default_model, **extra)
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", *argv],
        env=_env(tmp_path, home),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        timeout=60,
    )


def _warning(tmp_path: Path) -> str:
    path = tmp_path / "tau-home" / "config.json"
    return f"{path}: model 'old' is unavailable and can't be selected: {_FULL_ERROR}"


@pytest.mark.parametrize(
    ("argv", "default_model"),
    [
        (["-p", "--model", "old", "hi"], "good"),
        (["-p", "--model", "old:high", "hi"], "good"),
        (["-p", "hi"], "old"),
        (["--mode", "rpc"], "old"),
        (["--mode", "rpc", "--model", "old"], "good"),
        (["--mode", "repl"], "old"),
        ([], "old"),
        (["--model", "old"], "good"),
        (["serve", "--listen", "127.0.0.1:0"], "old"),
        (["serve", "-d", "--listen", "127.0.0.1:0"], "old"),
    ],
    ids=[
        "print-model",
        "print-model-level",
        "print-default",
        "rpc-default",
        "rpc-model",
        "repl-default",
        "tui-default",
        "tui-model",
        "serve",
        "serve-daemon",
    ],
)
def test_the_selected_bad_entry_is_one_line_and_exit_2(
    tmp_path: Path, argv: list[str], default_model: str
) -> None:
    out = _run(tmp_path, *argv, default_model=default_model)
    assert out.returncode == 2, out.stderr
    # prompt_toolkit warns about a non-tty stdin when the REPL builds its reader.
    lines = [line for line in out.stderr.splitlines() if not line.startswith("Warning: Input")]
    assert len(lines) == 1, out.stderr
    assert lines[0].startswith("tau: error: ")
    assert f"the selected model 'old' does not build: {_FULL_ERROR}" in lines[0]
    assert lines[0].endswith("Fix models.old, or select a model that builds")


def test_an_unused_bad_entry_warns_and_rpc_lists_and_refuses_it(tmp_path: Path) -> None:
    """Startup succeeds; get_models carries the error; set_model to it is refused."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "get_models"},
        {"jsonrpc": "2.0", "id": 2, "method": "set_model", "params": {"name": "old"}},
        {"jsonrpc": "2.0", "id": 3, "method": "get_state"},
    ]
    out = _run(tmp_path, "--mode", "rpc", stdin="".join(json.dumps(r) + "\n" for r in requests))
    assert out.returncode == 0, out.stderr
    assert out.stderr.splitlines() == [f"tau: warning: {_warning(tmp_path)}"]
    replies = {
        reply["id"]: reply
        for reply in map(json.loads, out.stdout.splitlines())
        if reply.get("id") is not None
    }
    rows = {row["name"]: row for row in replies[1]["result"]["models"]}
    assert rows["old"]["model"] is None and _ERROR in rows["old"]["error"]
    assert rows["good"]["error"] is None
    assert _ERROR in replies[2]["error"]["message"]
    assert replies[3]["result"]["model"]["id"] == "m"


def test_an_unused_bad_entry_warns_once_before_a_print_run(tmp_path: Path) -> None:
    out = _run(tmp_path, "-p", "--no-session", "hi")
    assert out.stderr.splitlines()[0] == f"tau: warning: {_warning(tmp_path)}"
    assert out.stderr.count(_ERROR) == 1, out.stderr
    assert "does not build" not in out.stderr


def test_the_serve_daemon_logs_an_unused_bad_entry(tmp_path: Path) -> None:
    home = _home(tmp_path, "good")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tau_coding_agent.cli", "serve", "--listen", "127.0.0.1:0"],
        env=_env(tmp_path, home),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []
    try:
        deadline = time.monotonic() + 60
        assert proc.stdout is not None
        while time.monotonic() < deadline and not any("warning:" in ln for ln in lines):
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    assert any(ln.endswith(f" warning: {_warning(tmp_path)}") for ln in lines), lines
    assert any("tau serve listening on" in ln for ln in lines), lines


def test_check_model_entries_raises_only_for_the_selected_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    config: dict[str, Any] = {"models": _MODELS}
    assert check_model_entries(config, "good") == [
        f"{tmp_path / 'config.json'}: model 'old' is unavailable and can't be selected: "
        + model_entry_errors(config)["old"]
    ]
    assert len(check_model_entries(config, None)) == 1
    assert len(check_model_entries(config, "openai/adhoc")) == 1
    assert check_model_entries(config, "old", fatal=False) == []
    with pytest.raises(ModelConfigError, match="the selected model 'old' does not build"):
        check_model_entries(config, "old:low")


def test_every_problem_in_every_bad_entry_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    config = {
        "models": {
            "a": {**_GOOD, "max_tokens": 0, "grammar": "regex"},
            "b": {**_GOOD, "temperature": "hot"},
            "c": "not an object",
            "later": {"backend": "registered-by-an-extension", "model": "x"},
            "good": _GOOD,
        }
    }
    errors = model_entry_errors(config)
    assert sorted(errors) == ["a", "b", "c"]
    assert all("\n" not in message for message in errors.values())
    assert "models.a.max_tokens must be a positive integer" in errors["a"]
    assert "models.a.grammar must be 'llguidance' or 'gbnf'" in errors["a"]
    assert "models.b is not valid: temperature:" in errors["b"]
    assert errors["c"] == "models.c must be a JSON object; got 'not an object'"
    with pytest.raises(ModelConfigError) as excinfo:
        model_entry_errors({"models": ["not", "an", "object"]})
    assert isinstance(excinfo.value, ConfigError) and isinstance(excinfo.value, ValueError)


def test_the_model_picker_leaves_out_an_entry_that_does_not_build() -> None:
    """Listed by get_models with its error, but never offered to select."""
    from tau_agent_core.flows import enumerate_domain, unavailable_models
    from tau_agent_core.projections import model_catalog
    from tau_coding_agent.backends import make_model_resolver

    resolver = make_model_resolver(
        {"models": {**_MODELS, "ext": {"backend": "acme", "model": "m"}, "c": "x"}}
    )
    found = enumerate_domain("model_name", models=resolver)
    assert [value.value for value in found.values] == ["good"]
    assert sorted(unavailable_models(resolver)) == ["c", "ext", "old"]
    rows = {row["name"]: row for row in model_catalog(resolver)}
    assert rows["ext"]["model"] is None and "'acme'" in rows["ext"]["error"]
    assert rows["old"]["model"] is None and _ERROR in rows["old"]["error"]
    assert rows["c"]["error"] == "models.c must be a JSON object; got 'x'"
    assert rows["good"]["error"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--stop", "--listen", "127.0.0.1:1"],
        ["serve", "--tail", "--listen", "127.0.0.1:1"],
        ["serve", "--schema"],
        ["ext", "list"],
    ],
    ids=["serve-stop", "serve-tail", "serve-schema", "ext-list"],
)
def test_commands_that_use_no_model_are_not_blocked_or_warned(
    tmp_path: Path, argv: list[str]
) -> None:
    """A typo in the selected entry must not stop you stopping or tailing a running daemon."""
    out = _run(tmp_path, *argv, default_model="old")
    assert "models.old" not in out.stderr, out.stderr
    assert "Traceback" not in out.stderr


@pytest.mark.parametrize("name", ["old", "ext"])
def test_switching_to_a_bad_entry_mid_run_is_refused_with_its_error(name: str) -> None:
    """ctx.set_model's contract error is tau_agent_core's, so no extension imports a head."""
    from tau_agent_core import ModelUnavailableError
    from tau_coding_agent.backends import create_backend, make_model_resolver

    config = {"models": {**_MODELS, "ext": {"backend": "acme", "model": "m"}}}
    backend = create_backend({**_GOOD, "config_name": "good"})
    session = backend.agent_session
    session.set_model_resolver(make_model_resolver(config))
    ctx = session._extension_api.context
    expected = _ERROR if name == "old" else "backend 'acme' is not a registered"
    with pytest.raises(ModelUnavailableError, match=expected):
        ctx.set_model(name)
    with pytest.raises(ModelUnavailableError, match=expected):
        session.set_model(name)
    with pytest.raises(KeyError, match="unknown model 'nope'"):
        ctx.set_model("nope")
    assert session.model_name == "good"


async def test_the_tui_notifies_each_unused_bad_entry_on_mount(make_app) -> None:
    """The TUI's channel is a notification, not stderr under the screen."""
    warnings = check_model_entries({"models": _MODELS}, "good")
    app = make_app(cli_run_config={"model_warnings": warnings})
    async with app.run_test():
        notes = [(n.title, n.message, n.severity) for n in app._notifications]
    assert ("Config", warnings[0], "warning") in notes


_SQUASH = {**_GOOD, "compaction": {"enable": True}}
_SQUASH_ERROR = "unknown key 'models.squash.compaction.enable'"


def _rpc(tmp_path: Path, *requests: dict[str, Any], **extra: Any) -> tuple[Any, dict]:
    stdin = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
    out = _run(tmp_path, "--mode", "rpc", stdin=stdin, **extra)
    replies = {
        reply["id"]: reply
        for reply in map(json.loads, out.stdout.splitlines())
        if reply.get("id") is not None
    }
    return out, replies


def test_an_unused_entry_with_a_bad_compaction_block_warns_lists_and_refuses(
    tmp_path: Path,
) -> None:
    """Its own block is part of the entry, so it is that entry's error, not startup's."""
    out, replies = _rpc(
        tmp_path,
        {"id": 1, "method": "get_models"},
        {"id": 2, "method": "set_model", "params": {"name": "squash"}},
        models={"good": _GOOD, "squash": _SQUASH},
    )
    assert out.returncode == 0, out.stderr
    [line] = out.stderr.splitlines()
    assert line.startswith("tau: warning: ") and "model 'squash' is unavailable" in line
    assert _SQUASH_ERROR in line
    rows = {row["name"]: row for row in replies[1]["result"]["models"]}
    assert rows["squash"]["model"] is None and _SQUASH_ERROR in rows["squash"]["error"]
    assert _SQUASH_ERROR in replies[2]["error"]["message"]


@pytest.mark.parametrize(
    "argv", [["-p", "--model", "squash", "hi"], ["--mode", "rpc", "--model", "squash"]]
)
def test_a_selected_entry_with_a_bad_compaction_block_is_exit_2(
    tmp_path: Path, argv: list[str]
) -> None:
    out = _run(tmp_path, *argv, models={"good": _GOOD, "squash": _SQUASH})
    assert out.returncode == 2, out.stderr
    assert out.stderr.count("\n") == 1, out.stderr
    assert out.stderr.startswith("tau: error: ")
    assert f"the selected model 'squash' does not build: {_SQUASH_ERROR}" in out.stderr


@pytest.mark.parametrize("argv", [["-p", "hi"], ["--mode", "rpc"]])
def test_a_bad_top_level_compaction_block_is_fatal_whatever_is_selected(
    tmp_path: Path, argv: list[str]
) -> None:
    """The top-level block applies to every model, so no selection avoids it."""
    out = _run(tmp_path, *argv, models={"good": _GOOD}, compaction={"enable": True})
    assert out.returncode == 2, out.stderr
    assert out.stderr.count("\n") == 1, out.stderr
    assert out.stderr.startswith("tau: error: ")
    config = tmp_path / "tau-home" / "config.json"
    assert out.stderr.startswith(f"tau: error: {config}: unknown key 'compaction.enable'")
