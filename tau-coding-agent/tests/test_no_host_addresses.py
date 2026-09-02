"""Nothing that ships names one machine.

τ is meant to be forked, installed, and run by someone else, so a LAN address
or a home directory baked into the shipped trees is a defect: it makes the
package work in exactly one place and fail quietly everywhere else.

Scope is deliberate. The four ``src/`` trees plus ``examples/`` and
``scripts/`` are what a user installs or copies from, so they are held strictly.
``docs/probe-results/`` and ``experiments/`` are dated records of runs against a
specific machine — the address there is the *data*, and rewriting it would be
falsifying a measurement.

The file list is walked, not asked of ``git``. See ``_shipped_files`` for why:
the release matrix tests a ``git archive`` export, which has no ``.git``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

LEAKS = re.compile(r"192\.168\.\d+\.\d+|/home/(john|kevin)\b")

#: Trees a user installs from or copies out of.
SHIPPED = (
    "tau-llm/src",
    "tau-agent-core/src",
    "tau-coding-agent/src",
    "tau-jmfts/src",
    "examples/",
    "scripts/",
)

#: This file names the pattern it forbids.
ALLOWED = {"tau-coding-agent/tests/test_no_host_addresses.py"}

#: File types whose text a person reads or a program parses.
SUFFIXES = {".py", ".json", ".sh", ".toml", ".md", ".txt"}

#: Directory names that are build output rather than source. ``*.egg-info``
#: lives INSIDE ``<package>/src`` after an editable install and its
#: ``SOURCES.txt`` is a ``.txt``, so it would otherwise be read.
NOT_SOURCE = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _shipped_files() -> list[str]:
    """Every shipped source file, enumerated from the filesystem.

    This used to ask ``git ls-files``, which cost the release matrix four
    failures per Python version: `docs/RELEASING.md` §2 builds the candidate
    with ``git archive``, so the tree under test has no ``.git`` and the
    subprocess raised. The suite was reporting a defect in τ where the only
    fact was that the harness had unpacked a tarball.

    Walking is also the stricter question. ``git ls-files`` sees TRACKED files,
    and a host address is just as baked in when it sits in a file that has been
    written but not yet added — which is the state every one of these files
    passes through. What the walk must exclude is build output, named above,
    because that is generated and not shipped.
    """
    found: list[str] = []
    for tree in SHIPPED:
        root = REPO / tree.rstrip("/")
        # A missing shipped tree is a fact about the checkout, not a tree to
        # skip. Skipping it would shrink this test's scope silently, which is
        # the failure mode a walk has and a `git ls-files` did not.
        assert root.is_dir(), f"{tree} is not in this checkout, so the scan would be incomplete"
        for path in sorted(root.rglob("*")):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            parts = path.relative_to(REPO).parts
            if any(part in NOT_SOURCE or part.endswith(".egg-info") for part in parts):
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel not in ALLOWED:
                found.append(rel)
    return found


def test_no_host_addresses_in_shipped_trees():
    offenders: list[str] = []
    for rel in _shipped_files():
        path = REPO / rel
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if LEAKS.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "host-specific addresses in a shipped tree — use an env var or an "
        "argument instead:\n" + "\n".join(offenders)
    )


def test_the_scan_actually_reaches_the_shipped_trees():
    """An enumeration that returns nothing passes this suite vacuously.

    The scan is the whole test: a walk that silently found no files would
    report a clean tree with the same green dot as a real pass. So the count is
    asserted, and one file from each shipped tree is named — a rename that
    empties a tree fails HERE, saying which, rather than turning the guard off.
    """
    found = _shipped_files()
    assert len(found) > 100, f"the walk found only {len(found)} files; it is not reaching the tree"

    for expected in (
        "tau-llm/src/tau_llm/streaming.py",
        "tau-agent-core/src/tau_agent_core/agent_loop.py",
        "tau-coding-agent/src/tau_coding_agent/app.py",
        "tau-jmfts/src/tau_jmfts/store.py",
        "examples/01_permission_gate.py",
    ):
        assert expected in found, f"{expected} is a shipped file and the scan missed it"

    # Build output is excluded, and the reason is that it is generated: an
    # editable install writes *.egg-info INSIDE <package>/src and its
    # SOURCES.txt carries absolute paths from whoever ran pip.
    assert not [p for p in found if ".egg-info" in p]
