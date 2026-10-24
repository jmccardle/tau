"""#70: the root conftest fails a session whose tests create a file in the real ``~/.tau``.

Only new files count: a live ``tau serve`` appends to what it already has.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tau_agent_core.testing import child_env

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


def _run(
    tmp_path: Path, body: str, existing: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a one-test project under the root conftest with ``HOME`` in ``tmp_path``."""
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(ROOT_CONFTEST, project / "conftest.py")
    (project / "test_it.py").write_text(body)
    home = tmp_path / "home"
    home.mkdir()
    for rel, text in (existing or {}).items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text)
    env = child_env(home, PYTHONPATH=os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


_WRITE = (
    "import os\n"
    "from pathlib import Path\n"
    "def test_w():\n"
    "    cwd = os.getcwd()\n"
    "    name = {name}\n"
    "    d = Path.home() / '.tau' / 'sessions' / name\n"
    "    d.mkdir(parents=True)\n"
    "    (d / 'leak.jsonl').write_text('x')\n"
)
_ROOTDIR_NAME = "'--' + cwd.lstrip('/').replace('/', '-') + '--'"


def test_a_new_session_dir_named_after_the_rootdir_fails_and_names_the_path(tmp_path):
    result = _run(tmp_path, _WRITE.format(name=_ROOTDIR_NAME))
    assert result.returncode == 1
    assert "1 passed" in result.stdout
    assert "tests wrote into the real ~/.tau" in result.stdout
    assert "leak.jsonl" in result.stdout and "new .tau/sessions/--" in result.stdout


def test_a_new_session_dir_named_after_an_unrelated_cwd_passes(tmp_path):
    result = _run(tmp_path, _WRITE.format(name="'--srv-someone-elses-project--'"))
    assert result.returncode == 0, result.stdout


def test_a_new_file_directly_under_dot_tau_fails(tmp_path):
    body = (
        "from pathlib import Path\n"
        "def test_w():\n"
        "    (Path.home() / '.tau').mkdir()\n"
        "    (Path.home() / '.tau' / 'serve.log').write_text('x')\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 1
    assert "new .tau/serve.log" in result.stdout


def test_a_pre_existing_file_being_modified_passes(tmp_path):
    body = (
        "from pathlib import Path\n"
        "def test_w():\n"
        "    with open(Path.home() / '.tau' / 'serve.log', 'a') as f:\n"
        "        f.write('more')\n"
    )
    result = _run(tmp_path, body, existing={".tau/serve.log": "x"})
    assert result.returncode == 0, result.stdout


def test_a_clean_session_passes(tmp_path):
    result = _run(tmp_path, "def test_ok():\n    pass\n")
    assert result.returncode == 0, result.stdout
