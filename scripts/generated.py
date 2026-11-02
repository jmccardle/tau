"""Shared by the generators (api_docs.py, protocol_guide.py): write a set of generated files, or
check that they are up to date and say how they differ."""

import difflib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).parents[1]


def sync(files: Mapping[Path, str], *, check: bool, label: str, owned: Sequence[Path] = ()) -> int:
    """Write `files`, or with `check` compare them with what is on disk and print a diff.

    `owned` are directories every file of which is generated: one not in `files` is stale.
    Returns the exit status."""
    stale = [
        p
        for d in owned
        if d.is_dir()
        for p in sorted(d.rglob("*"))
        if p.is_file() and p not in files
    ]
    if not check:
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        for path in stale:
            path.unlink()
        return 0
    status = 0
    for path, text in sorted(files.items()):
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        if have != text:
            status = 1
            sys.stderr.writelines(
                difflib.unified_diff(
                    have.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    str(path.relative_to(ROOT)),
                    "generated",
                )
            )
    for path in stale:
        status = 1
        print(
            f"{label}: {path.relative_to(ROOT)} is not generated any more; remove it",
            file=sys.stderr,
        )
    if status:
        print(f"{label}: stale; run scripts/{Path(sys.argv[0]).name}", file=sys.stderr)
    else:
        print(f"{label}: up to date")
    return status
