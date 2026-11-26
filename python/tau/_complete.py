"""Completion of the named types (D16 section 5), answered from the session and the config, as
`Completion` items (D32 section 1): the value, a label a list shows instead, and one line of
detail.

No extension code runs here, so completion cannot hang or raise on an extension's account.
A path is read on the driver's machine, relative to the process's working directory.
"""

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tau._core import Body, Node, Part, Session
from tau._model_list import ModelInfo, model_detail
from tau._values import NEW

NAMED = ("path", "model", "cursor", "label", "node")
"""The names `complete` knows: the `"format"` of each named type's schema."""

DETAIL_WIDTH = 80
"""The most characters a completion's `detail` holds; a longer one is cut, ending in `…`."""


def _line(text: str | None) -> str | None:
    """`text` on one line: its whitespace runs, newlines included, made one space."""
    return None if text is None else " ".join(text.split())


@dataclass(frozen=True)
class Completion:
    """One thing that could finish what is being typed (D32 section 1). `value` is what is
    inserted, or sent: it replaces the word being typed whole (quoted for a command line, unquoted
    from `Client.complete` and `values`). `label` is what a list shows instead of `value` (`None`:
    show `value`), and `detail` one line beside it, dimmed (`None`: nothing). Neither holds a
    newline (whitespace runs become one space), and `detail` is cut at `DETAIL_WIDTH` characters
    with `…`."""

    value: str
    label: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        detail = _line(self.detail)
        if detail is not None and len(detail) > DETAIL_WIDTH:
            detail = detail[: DETAIL_WIDTH - 1] + "…"
        object.__setattr__(self, "label", _line(self.label))
        object.__setattr__(self, "detail", detail)


def _size(size: int) -> str:
    """A file's size as a list shows it: `512 B`, `4.2 kB`, `3.1 MB`, `1.0 GB` (decimal units)."""
    if size < 1000:  # noqa: PLR2004 - the unit's step
        return f"{size} B"
    for unit in ("kB", "MB", "GB"):
        size_in = size / 1000
        if size_in < 1000 or unit == "GB":  # noqa: PLR2004 - the unit's step
            return f"{size_in:.1f} {unit}"
        size = round(size_in)
    raise AssertionError("unreachable")


def _paths(prefix: str) -> list[Completion]:
    """The names in the directory `prefix` is in that start with the rest of it; a
    directory ends with the separator the prefix used (`/` if it has none). A leading `~` is the
    driver's user's home, as the path value will be read, and stays as typed. Only directories
    and regular files (through links) are offered: a link to nothing, a FIFO or a socket is not a
    path `@` would attach. Unreadable or missing: none (there is nothing to complete). Each is
    labelled with its name alone, and a file's detail is its size."""
    if prefix == "~":
        return [Completion("~/", "~/")]
    separators = "/" + (os.sep if os.sep != "/" else "")  # a Windows path may use either
    cut = max(prefix.rfind(c) for c in separators) + 1
    shown, tail = prefix[:cut], prefix[cut:]  # the directory as typed: "", "dir/", "/", "C:\\x\\"
    mark = prefix[cut - 1] if cut else "/"
    found = _scan(os.path.expanduser(shown) or ".")
    return [
        Completion(
            f"{shown}{name}{mark if is_dir else ''}",
            f"{name}{mark if is_dir else ''}",
            None if size is None else _size(size),
        )
        for name, is_dir, size in sorted(found)
        if name.startswith(tail) and (tail.startswith(".") or not name.startswith("."))
    ]


def _scan(directory: str) -> list[tuple[str, bool, int | None]]:
    """Each directory and regular file (through links) in `directory`: its name, whether it is a
    directory, and a file's size. Unreadable or missing: none."""
    found: list[tuple[str, bool, int | None]] = []
    try:
        with os.scandir(directory) as scan:
            for e in scan:
                if e.is_dir():
                    found.append((e.name, True, None))
                elif e.is_file():
                    found.append((e.name, False, e.stat().st_size))
    except OSError:
        return []
    return found


def _models(models: Sequence[ModelInfo]) -> list[Completion]:
    """Each model, detailed as `model_detail` says it (D32 section 8)."""
    return [Completion(m.name, None, model_detail(m)) for m in models]


def _cursors(session: Session) -> list[Completion]:
    """Each open cursor, labelled with its labels, its node and whether it is busy; then `new`."""
    labels = session.labels()
    found: list[Completion] = []
    for cursor in session.cursors():
        state = session.cursor(cursor)
        names = [label for label, on in labels if on == cursor]
        label = f"{cursor} {', '.join(names)}" if names else None
        detail = f"node {state.at}" + (" · busy" if state.busy else "")
        found.append(Completion(str(cursor), label, detail))
    return [*found, Completion(NEW, None, "a new cursor here")]


def _labels(session: Session) -> list[Completion]:
    """Each label, with the cursor it is on (closed or open) and that cursor's node."""
    closed = dict(session.closed())
    found: list[Completion] = []
    for label, cursor in session.labels():
        if cursor in closed:
            detail = f"closed cursor {cursor} · node {closed[cursor]}"
        else:
            detail = f"cursor {cursor} · node {session.cursor(cursor).at}"
        found.append(Completion(label, None, detail))
    return found


def _config(data: str) -> str:
    """What a `Config` node sets, on one line: `model anthropic:sonnet; step_limit limit=5`."""
    fields = json.loads(data)
    said: list[str] = []
    if "model" in fields:
        said.append(f"model {fields['model']}")
    for name, options in fields.get("extensions", {}).items():
        said.append(f"{name} " + ", ".join(f"{k}={v}" for k, v in options.items()))
    return "; ".join(said)


def node_summary(node: Node) -> str:
    """A node in one line: its kind, and what it says first. `User · fix the parser so that…`,
    `Assistant · calls read, bash`, `Results · 2 calls`, `Config · model anthropic:sonnet`,
    `System`, `Custom · todo`. What a `node` completion's detail is."""
    match node.body:
        case Body.User(text=text):
            kind, said = "User", _first_line(text)
        case Body.Assistant(parts=parts):
            kind, said = "Assistant", _reply_line(parts)
        case Body.Results(outcomes=outcomes):
            kind, said = "Results", f"{len(outcomes)} call{'' if len(outcomes) == 1 else 's'}"
        case Body.Config(config=config):
            kind, said = "Config", _config(config.data)
        case Body.System():
            kind, said = "System", ""
        case Body.Custom(kind=custom):
            kind, said = "Custom", custom
        case _:
            raise TypeError(f"node {node.id} holds a body completion does not know")
    return f"{kind} · {said}" if said else kind


def _first_line(text: str) -> str:
    """The first line of `text` that is not blank, or `""`."""
    return next((line for line in text.splitlines() if line.strip()), "")


def _reply_line(parts: list[Part]) -> str:
    """A reply's first line of text, else the tools it calls."""
    text = _first_line("".join(p.text for p in parts if isinstance(p, Part.Text)))
    calls = [p.tool for p in parts if isinstance(p, Part.Call)]
    return text or ("calls " + ", ".join(calls) if calls else "")


async def complete(
    session: Session,
    models: Callable[[], Sequence[ModelInfo]],
    cursor: int,
    named: str,
    prefix: str,
) -> list[Completion]:
    """The values of the named type `named` that start with `prefix`, for `cursor`, each a
    `Completion` with the label and detail D32 section 1 gives its kind: `path` (directories end
    with `/`; labelled with the name, a file's detail its size), `model` (qualified names, from
    `models`; `model_detail`), `cursor` (open ids, labelled with their labels, then
    `"new"`), `label` (the cursor it is on), and `node` (the cursor's walk, newest first; its
    `node_summary`). `ValueError` for another name, `NoSuchCursor` for a cursor that is not
    open."""
    at = session.cursor(cursor).at
    match named:
        case "path":
            return await asyncio.to_thread(_paths, prefix)
        case "model":
            found = _models(models())
        case "cursor":
            found = _cursors(session)
        case "label":
            found = _labels(session)
        case "node":
            found = [
                Completion(str(n.id), None, node_summary(n))
                for n in reversed(session.tree.walk(at))
            ]
        case _:
            raise ValueError(f"cannot complete {named!r}; the named types are {list(NAMED)}")
    return [c for c in found if c.value.startswith(prefix)]
