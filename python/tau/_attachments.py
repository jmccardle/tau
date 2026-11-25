"""`Attachments`: the built-in extension that makes `@path` in an input name a file (D24 section 5).

`explain @src/main.rs` is sent as the line itself, then the file's text:

    explain @src/main.rs

    <attachment path="src/main.rs">
    ...
    </attachment>

A file over the `limit` option (10240 bytes), not UTF-8, or unreadable is not inlined: the model
gets `<reference path="/abs/src/big.log" size="2.1 MB" reason="over the 10 kB limit"/>` and can
`read` the part it needs. A word that names no regular file (a missing one, a directory, `@alice`)
stays as text, and a file named twice is attached once. A `</attachment>` inside a file's text is
written `<\\/attachment>`, so a file cannot end its own block and start another.

A file whose bytes are a PNG, JPEG, GIF or WebP image (judged by its bytes, not its name) is
attached as an image (D31): the draft carries it after the text, and the text gains one line,
`[image @shot.png: image/png 640x480]`, where a file's block would be. The `limit` is for text: an
image is held to `MAX_INPUT_BYTES` (20 MiB; a bigger one is a reference) and, after the hooks, to
what the model takes, as every user image is: a model without images halts the input with why.

A token is `@` followed by non-space characters, at the start of the text or after whitespace, or
`@"` and a path with spaces up to the next `"` (`@"my notes.txt"`). As in a shell, a backslash
makes the next character an ordinary one (`@my\\ notes.txt`; `\\\\` is a backslash); inside
quotes nothing is special but the closing `"`. On Windows a backslash separates directories
(`@src\\main.rs`, `@C:\\work\\a.txt`), so it escapes only a space. If the whole token names no
regular file, trailing punctuation is stripped one character at a time until one does (`see
@notes.txt.` attaches `notes.txt`). `~` expands to the home directory of whoever runs the session,
and a relative path is taken against the session's working directory, which is where `base_tools`
and path completion are rooted too.

A token that looks like a path (it has a `/`, a file extension or a `:LINE` suffix) but attaches
nothing is said to the person in a notice (`@missing.txt`, `@main.rs:10`). Plain words (`@alice`,
`john@host`) and code (in backticks or a fenced block) are silent.

It runs at `pre_input`, so the `User` node holds exactly what the model saw (I1), and it expands
only input a person typed (a draft authored by a `Frontend`, D17 section 1). Text a tool or an
extension wrote, such as a sub-agent's task or a template's body, is never expanded: it could
name a file the person's rules deny the model.
"""

import asyncio
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from tau._config import ConfigError
from tau._core import Author, BadImage, Body, Draft, Image
from tau._handles import Cursor, Subject
from tau._hook_order import EDIT, GUARD
from tau._images import MAX_INPUT_BYTES

BEFORE_EDITS = (GUARD + EDIT) // 2
"""Between the guards and the edits: an edit every other edit must see (D28)."""

_TOKEN = re.compile(r'(?<!\S)@(?:"([^"\n]+)"|((?:\\.|\S)+))')
_ESCAPE = re.compile(r"\\( )" if sys.platform == "win32" else r"\\(.)")
_CLOSE_TAG = re.compile(r"</(attachment)", re.IGNORECASE)
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)
_PATH_LIKE = re.compile(r"[\w~.+/-]*(?:/|\.\w|:\d+(?:-\d+)?$)[\w~.+/:-]*")
_PUNCTUATION = ".,;:!?)]}>'\""
_UNITS = ("B", "kB", "MB", "GB", "TB")
_IMAGE_HEADS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
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


def _candidates(token: str) -> list[str]:
    """The token, then the token with trailing punctuation stripped a character at a time."""
    found = [token]
    while found[-1] and found[-1][-1] in _PUNCTUATION:
        found.append(found[-1][:-1])
    return [c for c in found if c]


def _resolve(word: str, cwd: Path) -> Path | None:
    """The regular file `word` names (`~` expanded; relative to `cwd`), else `None`."""
    if sys.platform == "win32" and word.endswith((".", " ")):
        return None  # Windows ignores a trailing dot or space, so `a.txt.` would name `a.txt`
    path = Path(os.path.expanduser(word))
    path = path if path.is_absolute() else cwd / path
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _reference(path: Path, size: int | None, reason: str, limit: int = 0) -> str:
    """A `<reference/>`; `size` is shown in bytes where `size_text` would read as `limit` does."""
    if size is None:
        shown = ""
    else:
        text = f"{size} B" if size_text(size) == size_text(limit) else size_text(size)
        shown = f' size="{text}"'
    return f'<reference path="{quoted(str(path))}"{shown} reason="{quoted(reason)}"/>'


def _looks_like_an_image(path: Path) -> bool:
    """Whether the file starts as a PNG, JPEG, GIF or WebP does: worth reading whole as one."""
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return head.startswith(_IMAGE_HEADS) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def _image(typed: str, path: Path) -> tuple[str, Image | None]:
    """The line for the image file `typed` names and the image, read now; a reference (and no
    image) for one over `MAX_INPUT_BYTES`, unreadable, or not an image after all."""
    where = path.absolute()
    size = None
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            limit = size_text(MAX_INPUT_BYTES)
            return _reference(where, size, f"over the {limit} image limit", MAX_INPUT_BYTES), None
        image = Image(path.read_bytes())
    except OSError as e:
        return _reference(where, None, f"unreadable: {e.strerror or e}"), None
    except BadImage as e:
        return _reference(where, size, f"not an image: {e}"), None
    return f"[image @{typed}: {image.mime} {image.width}x{image.height}]", image


def _block(typed: str, path: Path, limit: int) -> str:
    """The attachment or reference for the file `typed` names, read now."""
    where = path.absolute()
    try:
        size = path.stat().st_size
        if size > limit:
            return _reference(where, size, f"over the {size_text(limit)} limit", limit)
        raw = path.read_bytes()
    except OSError as e:
        return _reference(where, None, f"unreadable: {e.strerror or e}")
    if len(raw) > limit:  # it grew while it was read
        return _reference(where, len(raw), f"over the {size_text(limit)} limit", limit)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _reference(where, len(raw), "not UTF-8")
    text = _CLOSE_TAG.sub(r"<\\/\1", text)
    return f'<attachment path="{quoted(typed)}">\n{text}\n</attachment>'


def _looks_like_a_path(token: str) -> bool:
    word = token.rstrip(_PUNCTUATION)
    return _PATH_LIKE.fullmatch(word) is not None


@dataclass(frozen=True)
class Expanded:
    """What `expand` made of a text: the `text` with the blocks, the `images` attached, in the
    order named, and the `missed` words that look like a path but attach nothing, as typed."""

    text: str
    images: list[Image]
    missed: list[str]


def _attached(word: str, path: Path, limit: int, *, images: bool) -> tuple[str, Image | None]:
    """The block or image line for the file `word` names, and the image if it is one."""
    if images and _looks_like_an_image(path):
        return _image(word, path)
    return _block(word, path, limit), None


def expand(text: str, cwd: Path, limit: int, *, images: bool = True) -> Expanded:
    """`text`, then the block of each file it names with `@` (the module docstring), each file
    once, in the order first named; `text` itself is never changed. With `images`, an image file
    is attached as an image and its line; without, it is a file like any other."""
    attached: list[Image] = []
    blocks: list[str] = []
    seen: set[Path] = set()
    missed: list[str] = []
    code = [m.span() for m in _CODE.finditer(text)]
    for match in _TOKEN.finditer(text):
        quoted_word = match.group(1)
        typed: str = quoted_word if quoted_word is not None else _ESCAPE.sub(r"\1", match.group(2))
        found = [quoted_word] if quoted_word is not None else _candidates(typed)
        for word in found:
            path = _resolve(word, cwd)
            if path is None:
                continue
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                line, image = _attached(word, path, limit, images=images)
                blocks.append(line)
                attached += [] if image is None else [image]
            break
        else:
            in_code = any(a <= match.start() < b for a, b in code)
            if not in_code and (quoted_word is not None or _looks_like_a_path(typed)):
                missed.append(typed)
    return Expanded(text if not blocks else text + "\n\n" + "\n".join(blocks), attached, missed)


async def attached_to(cursor: Cursor, typed: str) -> str:
    """The files the `@` words in `typed`, text a person typed into a command (a template's
    arguments), name: `"\n\n"` and their blocks as `pre_input` would add them to a line, or `""`
    if there are none or `attachments` is not loaded. Relative paths start at the process's
    working directory, as the extension's do. An `@` word that names nothing stays as text,
    unsaid: a command has no client to tell."""
    try:
        limit = cursor.options_of(Attachments.name).limit
    except ConfigError:
        return ""
    expanded = await asyncio.to_thread(expand, typed, Path.cwd(), limit, images=False)
    return expanded.text[len(typed) :]


class Attachments:
    """The built-in extension `attachments`: `@path` in every input names a file (the module
    docstring). `cwd` is the session's working directory, where relative paths start."""

    name = "attachments"
    order: ClassVar[Mapping[str, int]] = {"pre_input": BEFORE_EDITS}
    """After the guards (a refused input is never read from disk), before the edits, which
    must see the files attached (D24 section 1, D28)."""
    options = Options

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        """Rewrite what a person typed with the files its `@` words name, and say which `@`
        words that look like paths named none."""
        draft = subject.draft
        if draft is None or not isinstance(draft.by, Author.Frontend):
            return None
        if not isinstance(draft.body, Body.User):
            return None
        limit = cursor.options().limit
        body = draft.body
        found = await asyncio.to_thread(expand, body.text, self._cwd, limit)
        if found.text != body.text:
            images = [*body.images, *found.images]
            subject.draft = Draft(Body.User(found.text, images), draft.delivers, draft.by)
        for word in found.missed:
            cursor.tell(
                f"attachments: @{word} names no regular file in {self._cwd}, so it is sent as text",
                to=draft.by.client,
            )
        return None
