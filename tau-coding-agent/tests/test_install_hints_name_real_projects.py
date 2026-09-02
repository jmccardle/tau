"""Every ``pip install`` a user is told to run has to be one that works.

The import name and the distribution name differ throughout τ: the package
imports as ``tau_llm`` but installs as ``ffwf-tau-llm``. So an error message
that hands the reader an install command is one substitution away from naming a
project that does not exist on PyPI, and nothing catches it — the message is
only reachable when the extra is *missing*, which is exactly the machine that
cannot check the suggestion.

That happened. Both new provider modules shipped ``pip install
'tau-llm[google]'`` and ``pip install 'tau-llm[anthropic]'``; neither resolves.
This test reads the four distribution names out of the ``pyproject.toml`` files
rather than restating them, so renaming a project keeps the test true.

Path installs (``pip install -e ./tau-jmfts``) are left alone: they name a
directory in a checkout, not an index entry.

The file list is walked, not asked of ``git``. See ``_shipped_python`` for why:
the release matrix tests a ``git archive`` export, which has no ``.git``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Trees whose text a user reads and acts on.
SHIPPED = (
    "tau-llm/src",
    "tau-agent-core/src",
    "tau-coding-agent/src",
    "tau-jmfts/src",
)

#: ``pip install`` followed by its first argument, with optional quoting and
#: an optional ``-e``. The extras suffix is captured separately so it can be
#: dropped before the name is compared.
HINT = re.compile(r"""pip install\s+(?:-e\s+)?["'`]?(?P<name>[^\s"'`\[]+)""")

#: This file quotes the defect it forbids.
ALLOWED = {"tau-coding-agent/tests/test_install_hints_name_real_projects.py"}


def _distribution_names() -> set[str]:
    names = set()
    for package in ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts"):
        with (REPO / package / "pyproject.toml").open("rb") as handle:
            names.add(tomllib.load(handle)["project"]["name"])
    return names


#: Directory names that are build output rather than source. ``*.egg-info``
#: lives INSIDE ``<package>/src`` after an editable install.
NOT_SOURCE = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _shipped_python() -> list[str]:
    """Every shipped ``.py`` file, enumerated from the filesystem.

    This used to ask ``git ls-files``, which cost the release matrix a failure
    per Python version: `docs/RELEASING.md` §2 builds the candidate with
    ``git archive``, so the tree under test has no ``.git`` and the subprocess
    raised. Walking is also the stricter question — an install hint naming a
    project that does not exist is just as broken in a file that has been
    written but not yet added.
    """
    found: list[str] = []
    for tree in SHIPPED:
        root = REPO / tree
        # A missing shipped tree would silently shrink this test's scope.
        assert root.is_dir(), f"{tree} is not in this checkout, so the scan would be incomplete"
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(REPO).parts
            if any(part in NOT_SOURCE or part.endswith(".egg-info") for part in parts):
                continue
            found.append(path.relative_to(REPO).as_posix())
    return found


def _is_path_install(name: str) -> bool:
    return name.startswith((".", "/", "~"))


def test_every_install_hint_names_a_real_distribution() -> None:
    known = _distribution_names()
    offenders = []

    for path in _shipped_python():
        if path in ALLOWED:
            continue
        text = (REPO / path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in HINT.finditer(line):
                name = match.group("name")
                if _is_path_install(name) or name in known:
                    continue
                offenders.append(f"{path}:{lineno}: pip install {name}")

    assert not offenders, (
        "these install hints name a project that is not published:\n  "
        + "\n  ".join(offenders)
        + f"\npublished names are: {sorted(known)}"
    )


def test_the_scan_actually_reaches_the_shipped_trees() -> None:
    """An enumeration that returns nothing passes the test above vacuously."""
    found = _shipped_python()
    assert len(found) > 50, f"the walk found only {len(found)} files; it is not reaching the tree"
    assert "tau-llm/src/tau_llm/streaming.py" in found
    assert not [p for p in found if ".egg-info" in p]


def test_the_pattern_would_catch_the_defect_it_was_written_for() -> None:
    """A guard nobody has seen fail is a guard nobody knows works."""
    known = _distribution_names()

    bad = HINT.search("pip install 'tau-llm[google]'")
    assert bad is not None
    assert bad.group("name") == "tau-llm"
    assert bad.group("name") not in known

    good = HINT.search("pip install 'ffwf-tau-llm[google]'")
    assert good is not None
    assert good.group("name") == "ffwf-tau-llm"
    assert good.group("name") in known

    path = HINT.search("pip install -e ./tau-jmfts")
    assert path is not None
    assert _is_path_install(path.group("name"))
