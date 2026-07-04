"""E5 §8 — the LANDED status is real, not aspirational.

This is the proving test for the DOCS step that closes E5.2–E5.5: it asserts that
``docs/EXTENSIONS-E5-WIRING.md`` (banner + §8 step list) and ``ROADMAP.md`` mark
steps **S29–S37 landed with their REAL commit shas**. It is deliberately NOT a
tautology — it does not merely grep the prose for "LANDED". For every step it
extracts the sha the doc claims and resolves it against ACTUAL git history
(``git show -s --format=%s <sha>``), asserting the commit exists AND its subject
names that step (e.g. the sha the doc attributes to S30 is really the "E5 S30 —
eliminate the context hook" commit). A wrong, invented, or copy-pasted sha fails.

Reference: docs/EXTENSIONS-E5-WIRING.md §8 (E5.2–E5.5); mirrors how E5.1 (S25–S28)
was closed in commit a2f72d3.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_E5_DOC = _REPO_ROOT / "docs" / "EXTENSIONS-E5-WIRING.md"
_ROADMAP = _REPO_ROOT / "ROADMAP.md"

# The steps this DOCS closure claims landed, paired with the real commit each was
# landed in (from `git log --oneline` for this run). The test verifies the doc's
# OWN claimed sha resolves to a commit whose subject names the step — so this map
# is the expectation, not the source the doc is checked against.
_EXPECTED: dict[str, str] = {
    "S29": "dbacc98",
    "S30": "f2d326f",
    "S31": "c7dc4e5",
    "S32": "82d215b",
    "S33": "2d68d75",
    "S34": "b39ed96",
    "S35": "e913a41",
    "S36": "37cacca",
    "S37": "ef07b2a",
}

_SHA_TOKEN = re.compile(r"`([0-9a-f]{7,40})`")


def _git_subject(sha: str) -> str | None:
    """The commit subject for ``sha``, or ``None`` if it is not a real commit."""
    proc = subprocess.run(
        ["git", "show", "-s", "--format=%s", sha],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _section_8() -> str:
    text = _E5_DOC.read_text(encoding="utf-8")
    start = text.index("## 8.")
    return text[start:]


def _step_bullets() -> dict[str, str]:
    """Map ``S29`` → the text of its ``- **S29 — … ✅** (`sha`)`` bullet in §8.

    A bullet runs from its ``- **S<n>`` marker to the next ``- **S`` marker or the
    next ``###`` heading, so per-step assertions (the ✅, the sha) are scoped to the
    right step and can't be satisfied by a neighbour's text.
    """
    sec = _section_8()
    markers = list(re.finditer(r"^- \*\*(S\d+)\b", sec, flags=re.MULTILINE))
    bullets: dict[str, str] = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(sec)
        chunk = sec[m.start() : end]
        nxt = chunk.find("\n### ")
        if nxt != -1:
            chunk = chunk[:nxt]
        bullets[m.group(1)] = chunk
    return bullets


@pytest.mark.parametrize("step", list(_EXPECTED))
def test_section8_step_marked_landed_with_real_sha(step: str) -> None:
    bullets = _step_bullets()
    assert step in bullets, f"§8 has no bullet for {step}"
    bullet = bullets[step]

    # Marked landed.
    assert "✅" in bullet, f"{step} bullet is not marked landed (no ✅): {bullet!r}"

    # Claims the real sha for this step (a hex token that IS this step's commit).
    claimed = {tok for tok in _SHA_TOKEN.findall(bullet) if _git_subject(tok) is not None}
    assert claimed, f"{step} bullet cites no resolvable commit sha: {bullet!r}"

    expected = _EXPECTED[step]
    assert any(
        sha == expected or expected.startswith(sha) or sha.startswith(expected)
        for sha in claimed
    ), f"{step} bullet cites {sorted(claimed)}, not the real commit {expected}"

    # The cited commit's subject actually names this step — binds doc → git history.
    subject = _git_subject(expected)
    assert subject is not None, f"{expected} is not a real commit"
    assert step in subject, f"{expected} subject {subject!r} does not name {step}"


def test_banner_declares_e5_2_through_e5_5_landed() -> None:
    banner = _E5_DOC.read_text(encoding="utf-8")[:2000]
    assert "E5.2–E5.5" in banner or "E5.2-E5.5" in banner
    assert "LANDED" in banner
    # No longer advertised as still-PLAN in the banner.
    assert "still PLAN" not in banner


def test_roadmap_marks_e5_landed_with_the_real_shas() -> None:
    roadmap = _ROADMAP.read_text(encoding="utf-8")
    assert "S25–S37" in roadmap or "S29–S37" in roadmap
    # Every step's real sha is cited somewhere in the E5 state block.
    for step, sha in _EXPECTED.items():
        assert f"`{sha}`" in roadmap, f"ROADMAP omits {step}'s commit `{sha}`"
