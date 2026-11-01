"""docs/protocol.schema.json is generated (D21 section 8) by `tau._protocol.schema_text`.

    scripts/schema.py            rewrite the file
    scripts/schema.py --check    fail (with a diff) if the committed file differs

Any wire change is a readable diff of that file. At a release, if it differs from the last tag's,
`PROTOCOL` goes up by one. Needs a build of tau (`maturin develop`).
"""

import difflib
import sys
from pathlib import Path

from tau._protocol import schema_text

FILE = Path(__file__).parents[1] / "docs" / "protocol.schema.json"


def main(args: list[str]) -> int:
    if args not in ([], ["--check"]):
        print("usage: scripts/schema.py [--check]", file=sys.stderr)
        return 2
    wanted = schema_text()
    if not args:
        FILE.write_text(wanted, encoding="utf-8")
        return 0
    have = FILE.read_text(encoding="utf-8") if FILE.exists() else ""
    if have == wanted:
        print("schema: up to date")
        return 0
    diff = difflib.unified_diff(
        have.splitlines(keepends=True), wanted.splitlines(keepends=True), str(FILE), "generated"
    )
    sys.stderr.writelines(diff)
    print(f"schema.py: {FILE.name} is stale; run scripts/schema.py", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
