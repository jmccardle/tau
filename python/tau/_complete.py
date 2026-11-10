"""Completion of the named types (D16 section 5), answered from the session and the config.

No extension code runs here, so completion cannot hang or raise on an extension's account.
A path is read on the driver's machine, relative to the process's working directory.
"""

import asyncio
import os

from tau._config_layers import ModelCatalog
from tau._core import Session
from tau._values import NEW

NAMED = ("path", "model", "cursor", "label", "node")
"""The names `complete` knows: the `"format"` of each named type's schema."""


def _paths(prefix: str) -> list[str]:
    """The names in the directory `prefix` is in that start with the rest of it; a
    directory ends with the separator the prefix used (`/` if it has none). A leading `~` is the
    driver's user's home, as the path value will be read, and stays as typed. Only directories
    and regular files (through links) are offered: a link to nothing, a FIFO or a socket is not a
    path `@` would attach. Unreadable or missing: none (there is nothing to complete)."""
    if prefix == "~":
        return ["~/"]
    separators = "/" + (os.sep if os.sep != "/" else "")  # a Windows path may use either
    cut = max(prefix.rfind(c) for c in separators) + 1
    shown, tail = prefix[:cut], prefix[cut:]  # the directory as typed: "", "dir/", "/", "C:\\x\\"
    mark = prefix[cut - 1] if cut else "/"
    try:
        with os.scandir(os.path.expanduser(shown) or ".") as scan:
            names = sorted((e.name, e.is_dir()) for e in scan if e.is_dir() or e.is_file())
    except OSError:
        return []
    return [
        f"{shown}{name}{mark if is_dir else ''}"
        for name, is_dir in names
        if name.startswith(tail) and (tail.startswith(".") or not name.startswith("."))
    ]


async def complete(
    session: Session, catalog: ModelCatalog, cursor: int, named: str, prefix: str
) -> list[str]:
    """The values of the named type `named` that start with `prefix`, for `cursor`: `path`
    (directories end with `/`), `model` (qualified names), `cursor` (open ids and `"new"`),
    `label`, and `node` (the cursor's walk, newest first). `ValueError` for another name,
    `NoSuchCursor` for a cursor that is not open."""
    at = session.cursor(cursor).at
    match named:
        case "path":
            return await asyncio.to_thread(_paths, prefix)
        case "model":
            values = list(catalog.names())
        case "cursor":
            values = [str(c) for c in session.cursors()] + [NEW]
        case "label":
            values = [label for label, _ in session.labels()]
        case "node":
            values = [str(n.id) for n in reversed(session.tree.walk(at))]
        case _:
            raise ValueError(f"cannot complete {named!r}; the named types are {list(NAMED)}")
    return [v for v in values if v.startswith(prefix)]
