"""Repo-wide test setup: no test may resolve the tester's own τ home.

``TAU_HOME`` and ``TAU_PORTABLE_BASE`` are removed from ``os.environ`` when
this file is imported, which pytest does before it imports any package's
``tests/conftest.py`` or test module, so nothing imported during collection
can see them either. The home is resolved per call (docs/PORTABLE-HOME.md
§8). Child processes inherit the cleaned environment; a subprocess test still
builds its env with ``tau_agent_core.testing.child_env``.

Rationale: docs/PORTABLE-HOME.md §7.

``HOME`` itself is left alone: tests that exercise home resolution, and the
packages' ``_hermetic_extensions_dir`` fixtures (which compare ``Path.home()``
to the real one), depend on it. Instead the session fails if a test creates a file in
the real ``~/.tau`` (#70).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

import pytest

from tau_agent_core.tau_home import HOME_ENV_VARS

for _name in HOME_ENV_VARS:
    os.environ.pop(_name, None)

_BEFORE: pytest.StashKey[dict[str, tuple[int, int]]] = pytest.StashKey()


def snapshot_home_tau(home: Path) -> dict[str, tuple[int, int]]:
    """Map every file under ``<home>/.tau`` to its ``(mtime_ns, size)``.

    Args:
        home: The directory whose ``.tau`` is watched.

    Returns:
        Paths relative to ``home``; empty when ``.tau`` does not exist.
    """
    root = home / ".tau"
    seen: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            seen[str(path.relative_to(home))] = (stat.st_mtime_ns, stat.st_size)
    return seen


def _dashed(path: Path) -> str:
    """Encode *path* as the file session store names a cwd directory, minus the trailing ``--``."""
    return "--" + str(path).lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-")


def _names_inside(directory: str, roots: Iterable[Path]) -> bool:
    """Whether a dashed session directory name encodes a path inside one of *roots*."""
    for root in roots:
        prefix = _dashed(root)
        if directory == prefix + "--" or directory.startswith(prefix + "-"):
            return True
    return False


def home_tau_changes(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    roots: Iterable[Path],
) -> list[str]:
    """List the new files a test plausibly put under ``.tau``.

    Modifications and removals are ignored, since a live ``tau serve`` appends to
    its log and session files. A new file counts when it sits directly under
    ``.tau``, or under ``.tau/sessions/<dir>`` where ``<dir>`` encodes a cwd
    inside one of *roots* (the pytest rootdir and the temp directories).

    Args:
        before: A :func:`snapshot_home_tau` result.
        after: A later one for the same home.
        roots: Directories a test's cwd could be inside.

    Returns:
        Sorted lines, each ``new <path>``.
    """
    roots = list(roots)
    found = []
    for rel in after.keys() - before.keys():
        parts = Path(rel).parts
        direct = len(parts) == 2
        session = len(parts) >= 4 and parts[1] == "sessions" and _names_inside(parts[2], roots)
        if direct or session:
            found.append(f"new {rel}")
    return sorted(found)


def _is_worker(config: pytest.Config) -> bool:
    """Whether this is an xdist worker, which leaves the check to the controller."""
    return hasattr(config, "workerinput")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot the real ``~/.tau`` before any test runs (#70)."""
    if not _is_worker(session.config):
        session.config.stash[_BEFORE] = snapshot_home_tau(Path.home())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session, naming each path, if a test created a file in the real ``~/.tau``.

    Only plausibly-test files count (see :func:`home_tau_changes`), so a live
    ``tau serve`` writing beside the suite does not trip it.
    """
    if _is_worker(session.config):
        return
    roots = [session.config.rootpath, Path(tempfile.gettempdir()).resolve()]
    if session.config.option.basetemp:
        roots.append(Path(session.config.option.basetemp).resolve())
    after = snapshot_home_tau(Path.home())
    changes = home_tau_changes(session.config.stash[_BEFORE], after, roots)
    if not changes:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "tests wrote into the real ~/.tau (#70)", red=True)
        for line in changes:
            reporter.write_line(line)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
