"""A malformed model entry stops every head in one line, exit 2 (#65, docs/SILENT-DEFAULTS.md §8).

Every entry is checked at startup, the selected one or not, because
``get_models``, the serve hello and the TUI's picker build them all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.tau_home import HOME_ENV
from tau_agent_core.testing import child_env
from tau_coding_agent.backends import validate_model_entries
from tau_coding_agent.config import ConfigError, ModelConfigError

_GOOD = {"backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", "api_key": "x"}
_CONFIG = {
    "models": {"good": _GOOD, "old": {**_GOOD, "prompt_cache": "anthropic"}},
    "default_model": "good",
}


def _run(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "tau-home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(_CONFIG))
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", *argv],
        env=child_env(tmp_path / "home", **{HOME_ENV: str(home), "TMPDIR": str(tmp_path)}),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["-p", "--model", "old", "hi"],
        ["-p", "hi"],
        ["--mode", "rpc"],
        ["serve", "--listen", "127.0.0.1:0"],
        ["serve", "-d", "--listen", "127.0.0.1:0"],
    ],
    ids=["print-selected", "print-unselected", "rpc", "serve", "serve-daemon"],
)
def test_a_bad_entry_is_one_line_and_exit_2(tmp_path: Path, argv: list[str]) -> None:
    out = _run(tmp_path, *argv)
    assert out.returncode == 2, out.stderr
    assert out.stderr.count("\n") == 1, out.stderr
    assert out.stderr.startswith("tau: error: ")
    assert "models.old.prompt_cache is a boolean; got 'anthropic'" in out.stderr
    assert "prompt_cache_dialect" in out.stderr
    assert "Traceback" not in out.stderr


def test_every_bad_entry_is_named_in_one_config_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    config = {
        "models": {
            "a": {**_GOOD, "max_tokens": 0, "grammar": "regex"},
            "b": {**_GOOD, "temperature": "hot"},
            "c": "not an object",
            "later": {"backend": "registered-by-an-extension", "model": "x"},
        }
    }
    with pytest.raises(ModelConfigError) as excinfo:
        validate_model_entries(config)
    message = str(excinfo.value)
    assert isinstance(excinfo.value, ConfigError) and isinstance(excinfo.value, ValueError)
    assert "\n" not in message
    assert "models.a.max_tokens must be a positive integer" in message
    assert "models.a.grammar must be 'llguidance' or 'gbnf'" in message
    assert "models.b is not valid: temperature:" in message
    assert "models.c must be a JSON object" in message
    assert "later" not in message


def test_the_model_picker_leaves_out_an_entry_that_does_not_build() -> None:
    """Listed by get_models with its error, but never offered to select."""
    from tau_agent_core.flows import enumerate_domain, unavailable_models
    from tau_agent_core.projections import model_catalog
    from tau_coding_agent.backends import make_model_resolver

    resolver = make_model_resolver(
        {"models": {"good": _GOOD, "ext": {"backend": "acme", "model": "m"}}}
    )
    found = enumerate_domain("model_name", models=resolver)
    assert [value.value for value in found.values] == ["good"]
    assert list(unavailable_models(resolver)) == ["ext"]
    rows = {row["name"]: row for row in model_catalog(resolver)}
    assert rows["ext"]["model"] is None and "'acme'" in rows["ext"]["error"]
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
def test_commands_that_use_no_model_are_not_blocked(tmp_path: Path, argv: list[str]) -> None:
    """A typo in one entry must not stop you stopping or tailing a running daemon."""
    out = _run(tmp_path, *argv)
    assert "models.old" not in out.stderr, out.stderr
    assert "Traceback" not in out.stderr


def test_an_extension_catches_an_entry_that_does_not_build_by_the_core_name() -> None:
    """ctx.set_model's contract error is tau_agent_core's, so no extension imports a head."""
    from tau_agent_core import ModelUnavailableError
    from tau_coding_agent.backends import create_backend, make_model_resolver

    config = {"models": {"good": _GOOD, "ext": {"backend": "acme", "model": "m"}}}
    backend = create_backend({**_GOOD, "config_name": "good"})
    session = backend.agent_session
    session.set_model_resolver(make_model_resolver(config))
    ctx = session._extension_api.context
    with pytest.raises(ModelUnavailableError, match="backend 'acme' is not a registered"):
        ctx.set_model("ext")
    with pytest.raises(KeyError, match="unknown model 'nope'"):
        ctx.set_model("nope")
    assert session.model_name == "good"
