"""Mechanical enforcement of CLAUDE.md rules 3 and 4.

Rule 3: banned 0.15 vocabulary in identifiers, comments, and docstrings.
Rule 4: tests reach code only through the public API; no private access.
(`unittest.mock` is banned by ruff's banned-api setting in pyproject.toml.)

Not mechanically checked, because the words have legitimate uses: `head`
(banned only as a name for a frontend) and `fork` (banned only for
in-session branching; cross-session fork is a glossary term). Review
catches those.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNED = ["crates", "python", "tests", "scripts"]
SUFFIXES = {".rs", ".py", ".pyi", ".toml"}
SELF = Path(__file__).resolve()

# Each term with its inflections spelled out. A generic suffix wildcard would
# also match legitimate words (`framework`, `entryway`).
BANNED = [
    "lanes?",
    "suspensions?",
    "suspend(?:s|ed|ing)?",
    "responders?",
    "side_entr(?:y|ies)",
    "incomplete",
    "default_leaf",
    "acting_cursors?",
    "frame[sd]?",
    "entr(?:y|ies)",
    "backends?",
]
# Whole words, including inside snake_case/kebab-case identifiers
# (`default_lane`, `lane-id`) but not inside other words (`plane`).
BANNED_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(BANNED) + r")(?![A-Za-z])", re.IGNORECASE)
# Tool vocabulary that contains a banned word but means something else.
ALLOWED_PHRASES = [
    'build-backend = "maturin"',  # PEP 517 key
]

PRIVATE_RE = re.compile(r"\btau_next(?:\.\w+)*\._(?!_)")


def files() -> list[Path]:
    out: list[Path] = []
    for top in SCANNED:
        out += [p for p in (ROOT / top).rglob("*") if p.suffix in SUFFIXES and p.is_file()]
    out.append(ROOT / "pyproject.toml")
    out.append(ROOT / "Cargo.toml")
    return [p for p in out if p.resolve() != SELF and "target" not in p.parts]


def violations(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    found: list[str] = []
    is_test = rel.parts[0] == "tests"
    for n, line in enumerate(path.read_text().splitlines(), 1):
        text = line
        for phrase in ALLOWED_PHRASES:
            text = text.replace(phrase, "")
        found += [f"{rel}:{n}: banned term {m.group(1)!r}" for m in BANNED_RE.finditer(text)]
        if is_test and PRIVATE_RE.search(line):
            found.append(f"{rel}:{n}: test reaches a private tau_next name")
    return found


def main() -> int:
    found = [v for p in files() for v in violations(p)]
    for v in found:
        print(v, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
