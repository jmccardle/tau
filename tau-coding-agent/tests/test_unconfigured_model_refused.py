"""Every head refuses a bare ``--model`` that is not a configured key (docs/SILENT-DEFAULTS.md §7).

Each head runs as a real child with its own τ home, and must exit 2 with one
``tau: error:`` line before any session exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.tau_home import HOME_ENV
from tau_agent_core.testing import child_env

_CONFIG = {
    "models": {
        "local": {
            "backend": "openai",
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "x",
        }
    },
    "default_model": "local",
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
        ["-p", "--model", "nope", "hi"],
        ["--mode", "rpc", "--model", "nope"],
        ["--mode", "repl", "--model", "nope"],
        ["--model", "nope"],
    ],
    ids=["print", "rpc", "repl", "tui"],
)
def test_each_head_refuses_an_unconfigured_bare_model(tmp_path: Path, argv: list[str]) -> None:
    out = _run(tmp_path, *argv)
    assert out.returncode == 2, out.stderr
    # prompt_toolkit warns about a non-tty stdin when the REPL builds its reader.
    lines = [line for line in out.stderr.splitlines() if not line.startswith("Warning: Input")]
    assert len(lines) == 1, out.stderr
    assert lines[0].startswith("tau: error: model 'nope' is not configured in ")
    assert "(configured: local)" in lines[0]
    assert "--model PROVIDER/nope" in lines[0]
    assert not list((tmp_path / "tau-home").rglob("*.jsonl"))


def test_print_mode_takes_a_provider_prefixed_name_as_ad_hoc(tmp_path: Path) -> None:
    """The ad-hoc form gets past model resolution; the run then fails on the provider, not the name."""
    out = _run(tmp_path, "-p", "--no-session", "--model", "acme/m", "hi")
    assert "is not configured" not in out.stderr
    assert "acme" in out.stderr
