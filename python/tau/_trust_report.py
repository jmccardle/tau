"""What the trust report (D13) shows of the project's prompt, skill and hooks files: the text
`tau trust` prints before it asks (`_trust` assembles the report; this module words these parts).
"""

from pathlib import Path

from tau._command_hooks_file import HookFileError, parse_hooks
from tau._project_read import FOREIGN_BASES, safe_block, safe_text

_SHOWN_CHARS = 2000
"""How much of one prompt or skill file the report shows."""


def shown_path(path: str) -> str:
    """A key of `Trusted.prompt_files` as the report names it: under `.tau/`, or `.agents/`,
    `.claude/`."""
    return path if path.split("/", maxsplit=1)[0] in FOREIGN_BASES else f".tau/{path}"


def prompt_text(raw: bytes) -> list[str]:
    """The text of a prompt or skill file as report lines, cut at `_SHOWN_CHARS` characters, which
    the last line says."""
    text = raw.decode("utf-8", errors="replace")
    lines = [f"    {line}" for line in safe_block(text[:_SHOWN_CHARS].rstrip()).split("\n")]
    if len(text) > _SHOWN_CHARS:
        lines.append(f"    ... cut: {len(text) - _SHOWN_CHARS} more characters not shown")
    return lines


def hooks_listing(raw: bytes, *, changed: bool) -> list[str]:
    """The report's section for `.tau/hooks.json`: what it is, and, when it is new or changed since
    approval, the whole file. It is never cut: it is the commands you are about to let run."""
    lines = ["", f"Its hooks file: {len(raw)} bytes. Its commands run with your authority:"]
    if changed:
        lines += [f"    {line}" for line in safe_block(raw.decode("utf-8", "replace")).split("\n")]
    return lines


def hooks_problems(raw: bytes | None, path: Path) -> list[str]:
    """What keeps a project's `hooks.json` from being approved: it is not UTF-8, or not a hooks
    file (`parse_hooks` names each problem)."""
    if raw is None:
        return []
    try:
        parse_hooks(raw.decode("utf-8"), str(path))
    except UnicodeDecodeError:
        return [safe_text(f"{path} is not valid UTF-8")]
    except HookFileError as e:
        return [safe_block(str(e))]
    return []
