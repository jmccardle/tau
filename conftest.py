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
to the real one), depend on it. Instead the session fails if a test writes into
the real ``~/.tau`` (#70).
"""

from __future__ import annotations

import os
from pathlib import Path

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


def home_tau_changes(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """List the paths added, modified or removed between two snapshots.

    Args:
        before: A :func:`snapshot_home_tau` result.
        after: A later one for the same home.

    Returns:
        Sorted lines, each prefixed ``new``, ``modified`` or ``removed``.
    """
    lines = [f"new {p}" for p in after.keys() - before.keys()]
    lines += [f"modified {p}" for p in before.keys() & after.keys() if before[p] != after[p]]
    lines += [f"removed {p}" for p in before.keys() - after.keys()]
    return sorted(lines)


def _is_worker(config: pytest.Config) -> bool:
    """Whether this is an xdist worker, which leaves the check to the controller."""
    return hasattr(config, "workerinput")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot the real ``~/.tau`` before any test runs (#70)."""
    if not _is_worker(session.config):
        session.config.stash[_BEFORE] = snapshot_home_tau(Path.home())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session, naming each path, if a test wrote into the real ``~/.tau``.

    The whole tree is watched, so a live ``tau`` running beside the suite also
    trips it; that is a false positive worth one rerun, not a reason to look away.
    """
    if _is_worker(session.config):
        return
    changes = home_tau_changes(session.config.stash[_BEFORE], snapshot_home_tau(Path.home()))
    if not changes:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "tests wrote into the real ~/.tau (#70)", red=True)
        for line in changes:
            reporter.write_line(line)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
