"""examples/retry.py: `RetryProvider` (shipped in `tau`) in a program of your own."""

import os
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def clean_env(home: Path, tmp_path: Path) -> dict[str, str]:
    """Only what the example is given: where τ lives and the home directory. Windows also needs
    SYSTEMROOT, or Python cannot load its socket provider (WinError 10106)."""
    env = {"TAU_HOME": str(home), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_the_example_runs_against_the_mock_provider(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text((EXAMPLES / "config.json").read_text(encoding="utf-8"))
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / "retry.py")],
        env=clean_env(home, tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("reply ")
