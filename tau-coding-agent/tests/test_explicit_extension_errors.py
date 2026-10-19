"""An ``-e`` value that names nothing loadable is one error line and exit 2 in every CLI head.

Real child processes, because the defect was a traceback escaping ``main``: the
refusal was right and only its delivery was wrong (docs/EXTENSION-PACKAGES.md §2.1).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.testing import child_env

_REPO = Path(__file__).resolve().parents[2]
_TREES = ["tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta"]


def _record_line(root: Path, rel: str) -> str:
    """One RECORD line with a real sha256, as pip writes it."""
    data = (root / rel).read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{rel},sha256={digest},{len(data)}"


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """An installed ``tau.extensions`` package, a τ home, and a child env that sees both."""
    site = tmp_path / "site"
    pkg = site / "pkgx"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "ext.py").write_text("def register(api):\n    pass\n")
    info = site / "pkgx_dist-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: pkgx-dist\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[tau.extensions]\next = pkgx.ext\n")
    lines = [_record_line(site, r) for r in ("pkgx/__init__.py", "pkgx/ext.py")]
    (info / "RECORD").write_text("\n".join([*lines, f"{info.name}/RECORD,,"]) + "\n")
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
    pythonpath = os.pathsep.join([str(site), *(str(_REPO / t / "src") for t in _TREES)])
    env = child_env(home, TMPDIR=str(tmp_path), PYTHONPATH=pythonpath)
    return site, tmp_path, env


@pytest.mark.parametrize(
    "head", [["-p", "--no-session", "hi"], ["--mode", "rpc", "--no-session"]], ids=["print", "rpc"]
)
def test_an_installed_module_file_named_by_path_is_one_error_line(installed, head):
    site, cwd, env = installed
    argv = [sys.executable, "-m", "tau_coding_agent.cli", "-e", str(site / "pkgx" / "ext.py")]
    proc = subprocess.run(
        [*argv, *head],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    [line] = proc.stderr.strip().splitlines()
    assert line.startswith("tau: error: -e ")
    assert "module pkgx.ext of the installed package pkgx-dist 1.0" in line


def test_a_name_nothing_provides_is_one_error_line(installed):
    _, cwd, env = installed
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tau_coding_agent.cli",
            "-p",
            "--no-session",
            "-e",
            "no_such_ext",
            "hi",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stderr.strip().startswith("tau: error: -e no_such_ext:")
