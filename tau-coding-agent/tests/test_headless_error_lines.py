"""#73 and #74: a headless failure is one `tau: error:` line, and a retry is one line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TREES = ["tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta"]
_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _run_print(tmp_path: Path, models: dict, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``tau -p`` in a throwaway τ home whose config holds ``models``."""
    (tmp_path / "config.json").write_text(
        json.dumps({"default_model": next(iter(models)), "models": models})
    )
    env = {k: v for k, v in os.environ.items() if k not in (*_KEY_VARS, "TAU_PORTABLE_BASE")}
    env["TAU_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(str(_REPO / t / "src") for t in _TREES)
    return subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-p", *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def test_each_retry_is_printed_once_and_the_failure_is_one_line(tmp_path):
    """#73: the head's `[τ] … retry N of 3` line, not also the `tau_llm.retry` log line."""
    models = {
        "down": {
            "backend": "openai",
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "k",
        }
    }
    proc = _run_print(tmp_path, models, "--no-session", "hi")
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    lines = proc.stderr.strip().splitlines()
    retries = [ln for ln in lines if "retry " in ln and " of 3 in " in ln]
    assert len(retries) == 3, proc.stderr
    assert all(ln.startswith("[τ] ") and "model 'm'" in ln for ln in retries), proc.stderr
    assert len(lines) == 4, proc.stderr
    assert lines[-1].startswith("tau: error: ") and "127.0.0.1" in lines[-1]


def test_thinking_off_on_a_model_that_cannot_honour_it_is_one_line(tmp_path):
    """#74: the refusal is one `tau: error:` line, not a ValueError traceback."""
    models = {"noreason": {"backend": "openai", "model": "m", "api_key": "k"}}
    proc = _run_print(tmp_path, models, "--no-session", "--thinking", "off", "hi")
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    [line] = proc.stderr.strip().splitlines()
    assert line.startswith("tau: error: ")
    assert "off" in line and "noreason" in line
