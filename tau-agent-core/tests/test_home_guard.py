"""#70: the root conftest fails a session whose tests change the real ``~/.tau``.

A live ``tau serve`` writes beside the suite, so its log, session trees and
extension state may change; everything else may not.
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


def test_a_live_writers_log_being_modified_passes(tmp_path):
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


def _touch(rel: str, mode: str = "a") -> str:
    return (
        "from pathlib import Path\n"
        "def test_w():\n"
        f"    p = Path.home() / {rel!r}\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"    with open(p, {mode!r}) as f:\n"
        "        f.write('more')\n"
    )


def test_a_new_file_in_ext_state_fails(tmp_path):
    result = _run(tmp_path, _touch(".tau/ext-state/x.jsonl"))
    assert result.returncode == 1
    assert "new .tau/ext-state/x.jsonl" in result.stdout


def test_a_modified_config_json_fails(tmp_path):
    result = _run(tmp_path, _touch(".tau/config.json"), existing={".tau/config.json": "{}"})
    assert result.returncode == 1
    assert "modified .tau/config.json" in result.stdout


def test_a_removed_authorization_store_fails(tmp_path):
    body = (
        "from pathlib import Path\n"
        "def test_w():\n"
        "    (Path.home() / '.tau' / 'extension-authorizations.json').unlink()\n"
    )
    result = _run(tmp_path, body, existing={".tau/extension-authorizations.json": "{}"})
    assert result.returncode == 1
    assert "removed .tau/extension-authorizations.json" in result.stdout


def test_a_live_session_file_being_modified_passes(tmp_path):
    rel = ".tau/sessions/--home-john-proj--/s.jsonl"
    result = _run(tmp_path, _touch(rel), existing={rel: "x"})
    assert result.returncode == 0, result.stdout


def test_a_new_dotfile_staging_file_passes(tmp_path):
    result = _run(tmp_path, _touch(".tau/.ext-auth-123.json", "w"))
    assert result.returncode == 0, result.stdout
