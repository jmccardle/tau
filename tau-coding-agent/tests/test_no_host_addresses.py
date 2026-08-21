"""Nothing that ships names one machine.

τ is meant to be forked, installed, and run by someone else, so a LAN address
or a home directory baked into the shipped trees is a defect: it makes the
package work in exactly one place and fail quietly everywhere else.

Scope is deliberate. The four ``src/`` trees plus ``examples/`` and
``scripts/`` are what a user installs or copies from, so they are held strictly.
``docs/probe-results/`` and ``experiments/`` are dated records of runs against a
specific machine — the address there is the *data*, and rewriting it would be
falsifying a measurement.
"""

from __future__ import annotations

import re
import subprocess
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


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [
        p
        for p in out.split("\0")
        if p
        and p not in ALLOWED
        and p.startswith(SHIPPED)
        and p.rsplit(".", 1)[-1] in {"py", "json", "sh", "toml", "md", "txt"}
    ]


def test_no_host_addresses_in_shipped_trees():
    offenders: list[str] = []
    for rel in _tracked():
        path = REPO / rel
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if LEAKS.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "host-specific addresses in a shipped tree — use an env var or an "
        "argument instead:\n" + "\n".join(offenders)
    )
