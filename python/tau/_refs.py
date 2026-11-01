"""Refs (D18 section 4): `SESSION` or `SESSION:NODE`, and a path ref made absolute."""

import os
import re
from pathlib import Path

_NODE = re.compile(r"^(?P<session>.*):(?P<node>\d+)$", re.DOTALL)


def parse_ref(text: str) -> tuple[str, int | None]:
    """`SESSION` and the node of `SESSION:NODE`: `:NODE` is read only when the ref ends in `:`
    and decimal digits."""
    found = _NODE.match(text)
    if found is None:
        return text, None
    return found["session"], int(found["node"])


def absolute_ref(ref: str, cwd: Path) -> str:
    """`ref` with its path made absolute against `cwd` if it is a path (its session part has a
    `/`), keeping a `:NODE`; a name, an id or a prefix is left as it is."""
    session, node = parse_ref(ref)
    if "/" not in session:
        return ref
    path = os.path.abspath(cwd / session)
    return path if node is None else f"{path}:{node}"
