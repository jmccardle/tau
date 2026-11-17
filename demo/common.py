"""Shared plumbing of the races: the work directory, the two sandboxes and the fake server.

Everything lives under `$DEMO_WORK` (default `demo/.work`). A version's sandbox is its own
`HOME` and `TAU_HOME`; nothing outside the work directory is read or written, and the real
`~/.tau` and `~/.tau-0.15` are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(
    os.environ.get("DEMO_WORK")
    or Path(os.environ.get("TMPDIR", "/tmp")) / f"tau-demo-{os.getuid()}"
).resolve()
VERSIONS = ("15", "16")
FAKE = HERE / "fake_openai.py"
SESSION_STEP_LIMIT = 100000  # the fixtures chain far more calls than 0.16's default limit


def venv(version: str) -> Path:
    return WORK / f"venv{version}"


def tau_bin(version: str) -> Path:
    path = venv(version) / "bin" / "tau"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run demo/setup.sh")
    return path


def sandbox(version: str) -> Path:
    return WORK / f"home{version}"


def fixture_dir() -> Path:
    return WORK / "fixture"


def env_for(version: str) -> dict[str, str]:
    """A minimal environment: the sandbox as HOME and TAU_HOME, nothing else inherited that could
    point τ at a real home, a key or a proxy."""
    home = sandbox(version)
    env = {
        "PATH": f"{venv(version) / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TAU_HOME": str(home / ".tau"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "USER": "demo",  # shown in the TUI; the real login name stays out of the recordings
        "LOGNAME": "demo",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "NO_PROXY": "127.0.0.1,localhost",
    }
    return env


def write_config(version: str, port: int, theme: str) -> None:
    """Point `version`'s sandbox at the fake server on `port`. The two formats differ; the model
    name and URL are the same."""
    tau_home = sandbox(version) / ".tau"
    tau_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox(version).chmod(0o700)
    tau_home.chmod(0o700)
    url = f"http://127.0.0.1:{port}/v1"
    if version == "15":
        config = {
            "models": {
                "fake": {
                    "backend": "openai",  # rules: allow(backend): the key of 0.15's config file
                    "model": "fake-model",
                    "base_url": url,
                    "api_key": "none",
                }
            },
            "default_model": "fake",
            "enter_key": "submit",  # 0.15 inserts a newline on Enter by default
            "compaction": {"auto": False},
            "theme": theme,
        }
    else:
        config = {
            "default_model": "fake:model",
            "models": {"fake": {"base_url": url, "models": {"model": {"model": "fake-model"}}}},
            "extensions": {
                "step_limit": {"limit": SESSION_STEP_LIMIT},
                "compaction": {"auto": False},
            },
            "tui": {"theme": theme},
        }
    (tau_home / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def print_cmd(version: str, words: list[str], *, cont: bool = False) -> list[str]:
    """`tau -p` for `version`: the same words, the same model, no context files."""
    cmd = [str(tau_bin(version)), "-p", "-nc"]
    if not cont:  # 0.16 refuses -m on an existing session; both then use the session's own model
        cmd += ["-m", "fake" if version == "15" else "fake:model"]
    if version == "15":
        cmd += ["--no-fun"]
    if cont:
        cmd += ["-c"]
    return cmd + words


def tui_cmd(version: str) -> list[str]:
    """The TUI opening the newest session of the current directory. 0.16 has `-c`; in 0.15 `-c`
    is headless only and the TUI chooses from a list (`--resume`), so `choose_session` presses
    Enter on it."""
    if version == "15":
        return [str(tau_bin(version)), "--resume", "--no-fun"]
    return [str(tau_bin(version)), "-c"]


PICKER_15 = "Resume a session"


@contextmanager
def fake_server(script: Path, paced: bool = False) -> Iterator[int]:
    """Run the fake server on a free port for the duration; yield the port."""
    proc = subprocess.Popen(
        [sys.executable, str(FAKE), "--script", str(script)] + (["--paced"] if paced else []),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line.startswith("listening "):
            raise SystemExit(f"fake server did not start: {line!r}")
        yield int(line.rsplit(":", 1)[1])
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def server_stats(port: int) -> dict[str, object]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stats") as r:
        return json.load(r)


def monotonic() -> float:
    return time.perf_counter()
