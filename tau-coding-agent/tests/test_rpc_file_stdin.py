"""``tau --mode rpc`` with stdin a regular file or ``/dev/null`` (#72): read to EOF, exit 0, no traceback."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.testing import child_env

TIMEOUT_S = 60.0


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A ``$HOME`` whose config resolves a model without ever reaching one."""
    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    (home / "tmp").mkdir()
    (home / ".tau" / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "backend": "openai",
                        "model": "fake-model",
                        "base_url": "http://127.0.0.1:1/v1",
                        "api_key": "x",
                        "tools": [],
                    }
                },
                "default_model": "fake",
            }
        )
    )
    return home


def _run_rpc(home: Path, stdin: object) -> subprocess.CompletedProcess[bytes]:
    """Run rpc mode to completion on ``stdin``; a hang raises ``TimeoutExpired``."""
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "--mode", "rpc"],
        stdin=stdin,
        capture_output=True,
        env=child_env(home, TMPDIR=str(home / "tmp")),
        timeout=TIMEOUT_S,
    )


def test_dev_null_stdin_is_immediate_eof(fake_home: Path) -> None:
    """``/dev/null`` ends the session cleanly: exit 0, nothing on stderr."""
    with open("/dev/null", "rb") as devnull:
        proc = _run_rpc(fake_home, devnull)
    assert b"Traceback" not in proc.stderr, proc.stderr.decode()
    assert proc.returncode == 0, proc.stderr.decode()


def test_regular_file_stdin_is_processed_to_eof(fake_home: Path, tmp_path: Path) -> None:
    """A command file redirected into stdin is answered, then EOF ends the session."""
    commands = tmp_path / "commands.jsonl"
    commands.write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_state"}) + "\n")
    with open(commands, "rb") as stdin:
        proc = _run_rpc(fake_home, stdin)
    assert b"Traceback" not in proc.stderr, proc.stderr.decode()
    assert proc.returncode == 0, proc.stderr.decode()
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert any(r.get("id") == 1 and "result" in r for r in responses), responses
