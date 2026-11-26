"""Mechanical enforcement of CLAUDE.md rules 3 and 4, and of the built-ins that are leaves.

Rule 3: banned 0.15 vocabulary in identifiers, comments, and docstrings.
Rule 4: tests (tests/) reach code only through the public API. This script catches
imports of private tau names; ruff SLF001 catches private attribute
access, ruff banned-api catches `unittest.mock`, and tests/conftest.py
refuses the `monkeypatch` fixture.

Not mechanically checked, because the words have legitimate uses: `head`
(banned only as a name for a frontend) and `fork` (banned only for
in-session branching; cross-session fork is a glossary term). Review
catches those.

A **leaf** built-in (D28 section 6) is one tau could ship without: nothing in `python/tau`
imports its module except the table of built-ins (`_builtins.py`'s `BUILTINS`), so deleting
the module and its one row there leaves a working tau. `usage` is one: a replacement takes its
counting rules from the public `tau.Reading`, `reading` and `counted`, not from `tau._usage`.

A line may use a banned word for an unrelated meaning by saying so on that
line, naming the word and the reason:

    entry_points = ...  # rules: allow(entry): Python packaging API
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNED = ["crates", "python", "tests", "scripts", "examples", "demo"]
SUFFIXES = {".rs", ".py", ".pyi", ".toml"}
SELF = Path(__file__).resolve()

# Each term with its inflections spelled out. A generic suffix wildcard would
# also match legitimate words (`framework`, `entryway`).
BANNED = {
    "lane": "lanes?",
    "suspension": "suspensions?",
    "suspend": "suspend(?:s|ed|ing)?",
    "responder": "responders?",
    "side_entry": "side_entr(?:y|ies)",
    "incomplete": "incomplete",
    "default_leaf": "default_lea(?:f|ves)",
    "acting_cursor": "acting_cursors?",
    "frame": "frame[sd]?",
    "entry": "entr(?:y|ies)",
    "backend": "backends?",
}
# Whole words, including inside snake_case/kebab-case identifiers
# (`default_lane`, `lane-id`) but not inside other words (`plane`).
BANNED_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(f"(?P<{k}>{v})" for k, v in BANNED.items()) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
# CamelCase boundaries become underscores first, so `LaneId`, `SideEntry`,
# and `isIncomplete` are checked as `Lane_Id`, `Side_Entry`, `is_Incomplete`.
CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
ALLOW_RE = re.compile(r"rules: allow\((\w+)\): \S")
LEAVES = {"tau._usage": Path("python/tau/_builtins.py")}
"""Each leaf built-in's module, and the one file that may import it."""


def is_build_output(path: Path) -> bool:
    """Cargo's `target/` of the root, or of a package under `crates/` or `examples/`: generated."""
    parts = path.relative_to(ROOT).parts
    package = parts[0] in ("crates", "examples") and parts[2:3] == ("target",)
    return parts[0] == "target" or package


def files() -> list[Path]:
    out: list[Path] = []
    for top in SCANNED:
        out += [p for p in (ROOT / top).rglob("*") if p.suffix in SUFFIXES and p.is_file()]
    out += [ROOT / "pyproject.toml", ROOT / "Cargo.toml"]
    return [p for p in out if p.resolve() != SELF and not is_build_output(p)]


def banned_terms(line: str) -> list[str]:
    allowed = set(ALLOW_RE.findall(line))
    text = CASE_BOUNDARY.sub("_", line)
    return [m.group(0) for m in BANNED_RE.finditer(text) if m.lastgroup not in allowed]


def is_private(dotted: str) -> bool:
    return any(p.startswith("_") and not p.startswith("__") for p in dotted.split("."))


def private_imports(source: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.ImportFrom(module=str(mod)) if mod.split(".")[0] == "tau":
                if is_private(mod) or any(is_private(a.name) for a in node.names):
                    lines.append(node.lineno)
            case ast.Import(names=names):
                if any(a.name.startswith("tau") and is_private(a.name) for a in names):
                    lines.append(node.lineno)
            case _:
                pass
    return lines


def imported(source: str) -> list[tuple[int, str]]:
    """Each `tau` module a source imports, with its line: `from tau import _x` and
    `from tau._x import y` both import `tau._x`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.ImportFrom(module="tau", names=names):
                found += [(node.lineno, f"tau.{a.name}") for a in names]
            case ast.ImportFrom(module=str(mod)) if mod.split(".")[0] == "tau":
                found.append((node.lineno, mod))
            case ast.Import(names=names):
                found += [(node.lineno, a.name) for a in names if a.name.split(".")[0] == "tau"]
            case _:
                pass
    return found


def leaf_imports(rel: Path, source: str) -> list[str]:
    """Imports of a leaf built-in's module in `python/tau`, other than by its one importer."""
    if rel.parts[:2] != ("python", "tau") or rel.suffix != ".py":
        return []
    return [
        f"{rel}:{n}: imports the leaf built-in {mod}; only {LEAVES[mod]} may (D28 section 6)"
        for n, mod in imported(source)
        if mod in LEAVES and rel != LEAVES[mod]
    ]


def violations(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    source = path.read_text()
    found = [
        f"{rel}:{n}: banned term {term!r}"
        for n, line in enumerate(source.splitlines(), 1)
        for term in banned_terms(line)
    ]
    found += leaf_imports(rel, source)
    if rel.parts[0] == "tests" and path.suffix == ".py":
        found += [f"{rel}:{n}: test imports a private tau name" for n in private_imports(source)]
    return found


def main() -> int:
    found = [v for p in files() for v in violations(p)]
    for v in found:
        print(v, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
