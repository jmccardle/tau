"""``tau -p`` exits 1 with one ``tau: error:`` line when a turn-ending hook raises (#29).

A real child process, because the line and the exit code are what a script
driving ``tau -p`` reads (docs/HOOK-ERRORS.md §4).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.testing import child_env

_REPO = Path(__file__).resolve().parents[2]
_TREES = ["tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta"]


@pytest.mark.parametrize("hook", ["input", "before_agent_start"])
def test_a_raising_hook_is_exit_1_and_one_error_line(tmp_path: Path, hook: str) -> None:
    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    model = {
        "backend": "openai-completions",
        "provider": "openai",
        "model": "m",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "x",
    }
    (home / ".tau" / "config.json").write_text(
        json.dumps({"default_model": "m", "models": {"m": model}})
    )
    gate = tmp_path / "budgetgate.py"
    gate.write_text(
        "def register(api):\n"
        "    def check(event, ctx):\n"
        "        raise RuntimeError('ledger unreadable')\n"
        f"    api.on({hook!r}, check)\n"
    )
    pythonpath = os.pathsep.join(str(_REPO / t / "src") for t in _TREES)
    env = child_env(home, TMPDIR=str(tmp_path), PYTHONPATH=pythonpath)
    proc = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-e", str(gate), "-p", "--no-session", "hi"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    [line] = [x for x in proc.stderr.splitlines() if x.startswith("tau: error:")]
    assert line == (
        f"tau: error: extension 'budgetgate' {hook!r} handler raised RuntimeError: ledger unreadable"
    )
