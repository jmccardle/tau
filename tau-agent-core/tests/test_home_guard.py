"""#70: the root conftest fails a session whose tests write into the real ``~/.tau``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tau_agent_core.testing import child_env

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run a one-test project under the root conftest with ``HOME`` in ``tmp_path``."""
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(ROOT_CONFTEST, project / "conftest.py")
    (project / "test_it.py").write_text(body)
    home = tmp_path / "home"
    home.mkdir()
    env = child_env(home, PYTHONPATH=os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_write_into_the_real_tau_fails_the_session_and_names_the_path(tmp_path):
    result = _run(
        tmp_path,
        "from pathlib import Path\n"
        "def test_w():\n"
        "    d = Path.home() / '.tau' / 'sessions'\n"
        "    d.mkdir(parents=True)\n"
        "    (d / 'leak.jsonl').write_text('x')\n",
    )
    assert result.returncode == 1
    assert "1 passed" in result.stdout
    assert "new .tau/sessions/leak.jsonl" in result.stdout


def test_a_clean_session_passes(tmp_path):
    result = _run(tmp_path, "def test_ok():\n    pass\n")
    assert result.returncode == 0, result.stdout
