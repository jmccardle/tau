"""`Attachments`: the built-in extension that makes `@path` in an input name a file (D24 section 5).

`explain @src/main.rs` is sent as the line itself, then the file's text:

    explain @src/main.rs

    <attachment path="src/main.rs">
    ...
    </attachment>

A file over the `limit` option (10240 bytes), not UTF-8, or unreadable is not inlined: the model
gets `<reference path="/abs/src/big.log" size="2.1 MB" reason="over 10 kB"/>` and can `read` the
part it needs. A word that names no regular file (a missing one, a directory, `@alice`) stays as
text, and a file named twice is attached once.

A token is `@` followed by non-space characters, at the start of the text or after whitespace.
If the whole token names no regular file, trailing punctuation is stripped one character at a time
until one does (`see @notes.txt.` attaches `notes.txt`). `~` expands to the home directory of
whoever runs the session, and a relative path is taken against the session's working directory,
which is where `base_tools` and path completion are rooted too.

It runs at `pre_input`, so the `User` node holds exactly what the model saw (I1), and every input
is expanded: a person's, a template's, a sub-agent's task.
"""

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from tau._core import Body, Draft
from tau._handles import Cursor, Subject

_TOKEN = re.compile(r"(?<!\S)@(\S+)")
_PUNCTUATION = ".,;:!?)]}>'\""
_UNITS = ("B", "kB", "MB", "GB", "TB")
_STEP = 1024


@dataclass(frozen=True)
class Options:
    """`Attachments`' options (`extensions.attachments`; `/config attachments`)."""

    limit: Annotated[int, "largest file, in bytes, attached whole; a bigger one is a reference"] = (
        10240
    )

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"an attachment limit must be at least 1 byte, not {self.limit}")


def size_text(size: int) -> str:
    """`size` bytes as people write it: `300 B`, `10 kB`, `2.1 MB` (1024 to a unit)."""
    if size < _STEP:
        return f"{size} B"
    value, unit = float(size), 0
    while value >= _STEP and unit < len(_UNITS) - 1:
        value /= _STEP
        unit += 1
    return f"{value:.1f}".removesuffix(".0") + f" {_UNITS[unit]}"


def quoted(text: str) -> str:
    """`text` as the inside of a double-quoted attribute."""
    escaped = text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return escaped.replace(">", "&gt;").replace("\n", " ")


def candidates(token: str) -> list[str]:
    """The token, then the token with trailing punctuation stripped a character at a time."""
    found = [token]
    while found[-1] and found[-1][-1] in _PUNCTUATION:
        found.append(found[-1][:-1])
    return [c for c in found if c]


def _resolve(word: str, cwd: Path) -> Path | None:
    """The regular file `word` names (`~` expanded; relative to `cwd`), else `None`."""
    path = Path(os.path.expanduser(word))
    path = path if path.is_absolute() else cwd / path
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _reference(path: Path, size: int | None, reason: str) -> str:
    shown = "" if size is None else f' size="{size_text(size)}"'
    return f'<reference path="{quoted(str(path))}"{shown} reason="{quoted(reason)}"/>'


def _block(typed: str, path: Path, limit: int) -> str:
    """The attachment or reference for the file `typed` names, read now."""
    where = path.absolute()
    try:
        size = path.stat().st_size
        if size > limit:
            return _reference(where, size, f"over {size_text(limit)}")
        raw = path.read_bytes()
    except OSError as e:
        return _reference(where, None, f"unreadable: {e.strerror or e}")
    if len(raw) > limit:  # it grew while it was read
        return _reference(where, len(raw), f"over {size_text(limit)}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _reference(where, len(raw), "not UTF-8")
    return f'<attachment path="{quoted(typed)}">\n{text}\n</attachment>'


def expand(text: str, cwd: Path, limit: int) -> str:
    """`text`, then the block of each file it names with `@` (the module docstring), each file
    once, in the order first named. `text` itself is never changed."""
    blocks: list[str] = []
    seen: set[Path] = set()
    for match in _TOKEN.finditer(text):
        for word in candidates(match.group(1)):
            path = _resolve(word, cwd)
            if path is None:
                continue
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                blocks.append(_block(word, path, limit))
            break
    return text if not blocks else text + "\n\n" + "\n".join(blocks)


class Attachments:
    """The built-in extension `attachments`: `@path` in every input names a file (the module
    docstring). `cwd` is the session's working directory, where relative paths start."""

    name = "attachments"
    options = Options

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        """Rewrite the draft's text with the files its `@` words name."""
        draft = subject.draft
        if draft is None or not isinstance(draft.body, Body.User):
            return None
        limit = cursor.options().limit
        text = draft.body.text
        expanded = await asyncio.to_thread(expand, text, self._cwd, limit)
        if expanded != text:
            subject.draft = Draft(Body.User(expanded), draft.delivers, draft.by)
        return None
