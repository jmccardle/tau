"""Shared helpers for the hygiene scripts (deadcode, coverage_report, complexity).

Holds the distribution table, source-file discovery, and repo-root lookup.
Not a CLI; imported by sibling scripts in ``scripts/arch/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DISTRIBUTIONS = {
    "tau-llm": "tau-llm/src",
    "tau-agent-core": "tau-agent-core/src",
    "tau-coding-agent": "tau-coding-agent/src",
    "tau-jmfts": "tau-jmfts/src",
    "tau-meta": "tau-meta/src",
}
SRC_TREES = list(DISTRIBUTIONS.values())
EXTRA_TREES = ["examples", "scripts"]


def src_files() -> list[Path]:
    """Return every ``.py`` file under the five src trees, sorted, repo-relative."""
    out: list[Path] = []
    for tree in SRC_TREES:
        out += [p.relative_to(ROOT) for p in (ROOT / tree).rglob("*.py")]
    return sorted(out)


def test_files() -> list[Path]:
    """Return every ``.py`` file under the ``tau-*/tests`` trees, sorted."""
    out: list[Path] = []
    for dist in DISTRIBUTIONS:
        out += [p.relative_to(ROOT) for p in (ROOT / dist / "tests").rglob("*.py")]
    return sorted(out)


def extra_files() -> list[Path]:
    """Return every ``.py`` file under ``examples/`` and ``scripts/``, sorted."""
    out: list[Path] = []
    for tree in EXTRA_TREES:
        out += [p.relative_to(ROOT) for p in (ROOT / tree).rglob("*.py")]
    return sorted(out)


def dist_of(rel: str | Path) -> str:
    """Return the distribution name owning a repo-relative path, or ``other``."""
    s = str(rel)
    for name in DISTRIBUTIONS:
        if s.startswith(name + "/"):
            return name
    return "other"


def tool(name: str) -> list[str]:
    """Return argv prefix for ``python -m name``, exiting non-zero if missing."""
    import importlib.util

    if importlib.util.find_spec(name) is None:
        sys.exit(f"missing tool '{name}': run `{sys.executable} -m pip install {name}`")
    return [sys.executable, "-m", name]


def write_json(path: Path, data: object) -> None:
    """Write ``data`` as sorted, indented JSON to ``path``."""
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
