"""Nothing that ships names one machine.

τ is meant to be forked, installed, and run by someone else, so a LAN address
or a home directory baked into the shipped trees is a defect: it makes the
package work in exactly one place and fail quietly everywhere else.

Two scopes, because two things are published and the risk differs.

**Strict** — the five ``src/`` trees plus ``examples/`` and ``scripts/``, held
against ``LEAKS``. This is what a user installs or copies out of, so a LAN
address there is a bug in the software.

**Prose** — ``docs/``, ``ROADMAP.md`` and ``README.md``, held against
``PROSE_LEAKS``, which drops the LAN pattern and adds the private git host.
`docs/RELEASING.md` §"The two repositories" publishes this whole tree verbatim
to GitHub, so prose is disclosure rather than breakage: a home directory names
the author and the private remote names infrastructure, while a measured
192.168 address is the *record of a run* and rewriting it falsifies the
measurement. ``docs/probe-results/`` and ``experiments/`` are that kind of
record end to end and are exempt from both scans.

Known remainder, deliberately not covered: ``tau-*/tests/`` still carries
``/home/john`` fixture strings and 192.168 addresses in conformance records.
They publish too. Bringing them under the prose scope is a separate change.

The file list is walked, not asked of ``git``. See ``_shipped_files`` for why:
the release matrix tests a ``git archive`` export, which has no ``.git``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

LEAKS = re.compile(r"192\.168\.\d+\.\d+|/home/(john|kevin)\b")

#: Prose is published, not installed: a name discloses, an address is a record.
PROSE_LEAKS = re.compile(r"/home/(john|kevin)\b|dev\.ffwf\.net")

#: Trees a user installs from or copies out of.
SHIPPED = (
    "tau-llm/src",
    "tau-agent-core/src",
    "tau-coding-agent/src",
    "tau-jmfts/src",
    "tau-meta/src",
    "examples/",
    "scripts/",
)

#: Files GitHub gets verbatim that nobody installs from.
PROSE = ("docs/", "ROADMAP.md", "README.md")

#: Dated records of runs against one machine; the address there is the data.
PROSE_EXEMPT = ("docs/probe-results/",)

#: This file names the patterns it forbids.
ALLOWED = {"tau-coding-agent/tests/test_no_host_addresses.py"}

#: File types whose text a person reads or a program parses.
SUFFIXES = {".py", ".json", ".sh", ".toml", ".md", ".txt"}

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
    return _walk(SHIPPED, exempt=())


def _prose_files() -> list[str]:
    """Every file GitHub gets that nobody installs from, minus the run records."""
    return _walk(PROSE, exempt=PROSE_EXEMPT)


def _walk(roots: tuple[str, ...], *, exempt: tuple[str, ...]) -> list[str]:
    """Readable files under `roots`, as repo-relative posix paths.

    Each entry is a directory or a single file; a missing one asserts rather
    than scanning nothing, because an incomplete walk passes a scan-for-
    offenders test with the same green dot as a clean tree.
    """
    found: list[str] = []
    for tree in roots:
        root = REPO / tree.rstrip("/")
        assert root.exists(), f"{tree} is not in this checkout, so the scan would be incomplete"
        for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            parts = path.relative_to(REPO).parts
            if any(part in NOT_SOURCE or part.endswith(".egg-info") for part in parts):
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED or any(rel.startswith(prefix) for prefix in exempt):
                continue
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


def test_published_prose_names_no_person_and_no_private_remote():
    """`docs/` reaches GitHub verbatim, and nothing was checking it.

    The strict scan covers what a user installs; the squashed public commit
    carries the prose too, so a home directory or the private remote's hostname
    is published the moment the release is. Write `~/…` or a placeholder — the
    tree already cites sibling checkouts that way.
    """
    offenders: list[str] = []
    for rel in _prose_files():
        path = REPO / rel
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if PROSE_LEAKS.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "published prose names a person's home directory or the private "
        "remote:\n" + "\n".join(offenders)
    )


def test_the_prose_scan_reaches_the_docs_tree():
    found = _prose_files()
    assert len(found) > 50, f"the prose walk found only {len(found)} files"
    for expected in ("docs/RELEASING.md", "ROADMAP.md", "README.md"):
        assert expected in found, f"{expected} is published and the scan missed it"
    assert not [p for p in found if p.startswith("docs/probe-results/")]


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
        "tau-meta/src/tau_meta/__init__.py",
        "examples/01_permission_gate.py",
    ):
        assert expected in found, f"{expected} is a shipped file and the scan missed it"

    assert not [p for p in found if ".egg-info" in p]
