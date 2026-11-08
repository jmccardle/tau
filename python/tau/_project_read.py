"""Reading inside a project without being fooled (D13, D19 section 4): the one helper every read
of a project's file goes through, which refuses symlinks and anything not a regular file, and the
text a refusal or a report may safely show.

`TrustError` is a refusal with the reason and the path it is about; `read_regular` is the
reader; `safe_text` and `safe_block` make a name or a file's text fit to print.
"""

import contextlib
import errno
import os
import stat
import unicodedata
from collections.abc import Callable, Generator
from pathlib import Path

MAX_BYTES = 1 << 20
_LATIN1_MAX = 0xFF
_BMP_MAX = 0xFFFF


_SURROGATE_BYTES = range(0xDC80, 0xDD00)  # a byte that was not UTF-8, as surrogateescape holds it
_UNSAFE = ("Cc", "Cf", "Cs", "Zl", "Zp")


def safe_text(text: str) -> str:
    """`text` with every control, format (such as right-to-left override), line and paragraph
    separator, and surrogate character written as an escape (`\\x1b`, `\\u202e`), so nothing a
    project file or a directory name says can move the cursor, repaint or reorder a line, or
    fail to encode. A byte that was not UTF-8 in a name shows as `\\xNN`. Newlines are escaped
    too: use `safe_block` for text of several lines."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if unicodedata.category(ch) not in _UNSAFE:
            out.append(ch)
        elif code in _SURROGATE_BYTES:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code <= _LATIN1_MAX:
            out.append(f"\\x{code:02x}")
        elif code <= _BMP_MAX:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def safe_block(text: str) -> str:
    """`text` split on `"\\n"` only, each line through `safe_text`, joined again: the lines a
    caller wrote stay lines, and nothing in the data can start another."""
    return "\n".join(safe_text(line) for line in text.split("\n"))


def shown(path: Path) -> str:
    return safe_text(str(path))


class TrustError(PermissionError):
    """A project directory tau cannot or will not trust, with the reason. `where` is the
    path the refusal is about (`None`: the project as a whole)."""

    def __init__(self, message: str, where: Path | None = None) -> None:
        super().__init__(message)
        self.where = where


def refusal(path: Path, rest: str) -> TrustError:
    return TrustError(f"{shown(path)} {rest}", path)


@contextlib.contextmanager
def unless_io_fails(path: Path, doing: str) -> Generator[None]:
    """An `OSError` inside is a `TrustError` naming `path` and what we were doing to it."""
    try:
        yield
    except TrustError:
        raise
    except OSError as e:
        raise refusal(path, f"cannot be {doing}: {e.strerror or e}") from e


def read_regular(path: Path) -> bytes | None:
    """The bytes of a regular file, or `None` if absent. A symlink or other kind of file is a
    `TrustError` naming the path, and so is one over the size limit or one that cannot be
    read. Opened non-blocking, so a FIFO swapped in cannot hang us."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise refusal(path, "is a symlink; tau refuses symlinks inside a project") from e
        raise refusal(path, f"cannot be read: {e.strerror}") from e
    with unless_io_fails(path, "read"), os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise refusal(path, "is not a regular file")
        if info.st_size > MAX_BYTES:
            raise refusal(path, f"is {info.st_size} bytes; the limit is {MAX_BYTES}")
        raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise refusal(path, f"grew past {MAX_BYTES} bytes while it was being read")
        return raw


PROMPTS_DIR = "prompts"
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"


def _children(directory: Path) -> list[Path]:
    """The paths in `directory` sorted by name, without those starting with `.`; none if there is
    no such directory. A symlink or a non-directory in its place is a `TrustError`."""
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise refusal(
            directory, "is a symlink or not a directory; tau refuses those inside a project"
        )
    with unless_io_fails(directory, "listed"):
        names = sorted(os.listdir(directory))
    return [directory / name for name in names if not name.startswith(".")]


def _is_markdown(name: str) -> bool:
    """Whether `name` ends in `.md` in any case: `x.MD` is a misplaced template, not no file."""
    return name.lower().endswith(".md")


def prompt_candidates(
    names: Callable[[str], list[str]], is_dir: Callable[[str], bool]
) -> list[str]:
    """The paths under `.tau/` that are, or look like they were meant to be, a template or a skill
    file: `prompts/*.md` and `skills/*/SKILL.md`, and the places they are mistaken for (a `.md` in
    any case, one level inside a `prompts/` subdirectory, a flat `skills/*.md`, a `skill.md` of
    another case, a skill one directory too deep). `names(relative)` lists a directory under
    `.tau/` (none if absent) and `is_dir(relative)` says whether a path is one; both leave out
    names starting with `.`."""
    found: list[str] = []
    for top in (PROMPTS_DIR, SKILLS_DIR):
        for name in names(top):
            path = f"{top}/{name}"
            if not is_dir(path):
                if _is_markdown(name):
                    found.append(path)
                continue
            for inner in names(path):
                wanted = _is_markdown(inner) if top == PROMPTS_DIR else inner.lower() == "skill.md"
                if wanted:
                    found.append(f"{path}/{inner}")
                elif top == SKILLS_DIR and is_dir(f"{path}/{inner}"):  # skills/GROUP/NAME/SKILL.md
                    found += [
                        f"{path}/{inner}/{deep}"
                        for deep in names(f"{path}/{inner}")
                        if deep.lower() == "skill.md"
                    ]
    return sorted(found)


def read_prompt_files(tau: Path) -> dict[str, bytes]:
    """The prompt templates and skills of a project's `.tau` directory, as bytes, and the files
    that look like one in the wrong place (`prompt_candidates`): path relative to `tau`, with
    `/`. Other files are not read. A symlink among them, or any file `read_regular` refuses, is a
    `TrustError`. Nothing is returned for a directory that is absent."""

    def names(relative: str) -> list[str]:
        return [p.name for p in _children(tau / relative)]

    def is_dir(relative: str) -> bool:
        path = tau / relative
        if path.is_symlink():
            raise refusal(path, "is a symlink; tau refuses symlinks inside a project")
        return path.is_dir()

    files: dict[str, bytes] = {}
    for relative in prompt_candidates(names, is_dir):
        raw = read_regular(tau / relative)
        if raw is None:
            raise refusal(tau / relative, "vanished while the project was read")
        files[relative] = raw
    return files
