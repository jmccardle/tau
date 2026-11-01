"""The system prompt (D20): assembled once, when a session is created, and recorded as its
`System` node.

`system_prompt(cwd, loaded, append=..., context_files=...)` returns the text and where each part
of it came from. The parts, each joined to the next by a blank line: the base text (the nearest
`system_prompt` layer, else the built-in `prompts/base.md` with the working directory written
in), the `append` sections, then the context files in a `<project_context>` block: one
`AGENTS.md` or `CLAUDE.md` from `$TAU_HOME`, one from each directory from `/` down to `cwd`, and
the trusted project's `.tau/SYSTEM.md`. Nothing is read again once the session exists.

A context file that is found and cannot be used (unreadable, not UTF-8, a broken symlink) is a
`ContextFileError`: a prompt quietly missing its instructions looks like a model that ignored
them.
"""

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from tau._config import ConfigError
from tau._config_layers import CLI
from tau._startup import LoadedConfig
from tau._trust import SYSTEM_MD, TAU_DIR

CONTEXT_NAMES = ("AGENTS.md", "CLAUDE.md")
"""The files a directory can give, the first that is a regular file."""


class ContextFileError(ConfigError):
    """A context file that was found and cannot be used. The message names the path and the
    cause, and says how to start without context files."""


@dataclass(frozen=True)
class PromptSource:
    """One part of a system prompt: `origin` is `"base"` (the built-in text), `"config"` (a
    config file's `system_prompt`), `"flag"` (`--system-prompt` or an appended section) or a
    context file's absolute path; `size` is the bytes of text it contributes (a file's, after
    its BOM is dropped)."""

    origin: str
    size: int


@dataclass(frozen=True)
class SystemPrompt:
    """An assembled prompt: the `text` of the `System` node, and its `sources` in order."""

    text: str
    sources: tuple[PromptSource, ...]


def system_prompt(
    cwd: Path,
    loaded: LoadedConfig,
    *,
    append: Sequence[str] = (),
    context_files: bool = True,
) -> SystemPrompt:
    """The prompt for a session in `cwd` (resolved here), given the loaded config: its base text,
    `append` (`--append-system-prompt`, in order) and, if `context_files`, the files as the module
    says. A part with no text is left out. `ContextFileError` for a file found and unusable."""
    here = cwd.resolve()
    parts: list[tuple[str, str]] = []
    catalog = loaded.catalog
    if catalog.system_prompt is None:
        base = files("tau").joinpath("prompts", "base.md").read_text(encoding="utf-8")
        parts.append(("base", base.rstrip("\n").replace("{{cwd}}", str(here))))
    else:
        origin = "flag" if catalog.system_prompt_source == CLI else "config"
        parts.append((origin, catalog.system_prompt))
    parts += [("flag", text) for text in append]
    parts = [(origin, text) for origin, text in parts if text]
    found = _context_files(here, loaded) if context_files else []
    sources = [PromptSource(origin, len(text.encode())) for origin, text in parts]
    sources += [PromptSource(str(path), len(text.encode())) for path, text in found]
    texts = [text for _, text in parts]
    if found:
        texts.append(_block(found))
    return SystemPrompt("\n\n".join(texts), tuple(sources))


def _block(found: Sequence[tuple[Path, str]]) -> str:
    lines = ["<project_context>", "Project-specific instructions and guidelines:"]
    for path, text in found:
        lines += ["", f'<project_instructions path="{path}">', text.rstrip("\n")]
        lines.append("</project_instructions>")
    return "\n".join([*lines, "</project_context>"])


def _context_files(cwd: Path, loaded: LoadedConfig) -> list[tuple[Path, str]]:
    """The context files in prompt order, weakest first, each read once (by resolved path)."""
    ancestors = list(reversed((cwd, *cwd.parents)))
    shadowed = _shadowed(cwd)
    candidates = [loaded.home, *ancestors]
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directory in candidates:
        path = _context_file_in(directory)
        if path is None:
            continue
        path = path.resolve()
        if path == shadowed or path in seen:
            continue
        seen.add(path)
        found.append((path, _decoded(_read(path), path)))
    project = loaded.project
    if project is not None and project.system_md is not None:
        path = project.root / TAU_DIR / SYSTEM_MD
        found.append((path, _decoded(project.system_md, path)))
    return found


def _context_file_in(directory: Path) -> Path | None:
    """The first of `CONTEXT_NAMES` in `directory` that is a regular file (symlinks followed);
    a directory or FIFO of that name is not one. A symlink to nothing is `ContextFileError`."""
    for name in CONTEXT_NAMES:
        path = directory / name
        try:
            mode = path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            if path.is_symlink():
                raise _unusable(path, "is a symlink to nothing") from None
            continue
        except OSError as e:
            raise _unusable(path, e.strerror or str(e)) from e
        if stat.S_ISREG(mode):
            return path
    return None


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise _unusable(path, e.strerror or str(e)) from e


def _decoded(raw: bytes, path: Path) -> str:
    """Strict UTF-8, a leading BOM dropped."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _unusable(path, f"is not valid UTF-8 ({e.reason} at byte {e.start})") from e


def _unusable(path: Path, cause: str) -> ContextFileError:
    return ContextFileError(
        f"context file {path} cannot be used: {cause}. Fix it, or start with "
        "--no-context-files to skip the context files"
    )


def _git_unreadable(path: Path, cause: Exception) -> ContextFileError:
    return ContextFileError(
        f"{path} cannot be read ({getattr(cause, 'strerror', None) or cause}), so it is not "
        "known whether a worktree's context file hides its main checkout's. Fix it, or start "
        "with --no-context-files to skip the context files"
    )


def _shadowed(cwd: Path) -> Path | None:
    """The main checkout's context file that a linked worktree nested inside it replaces: when
    `cwd` is in a worktree whose main repository is an ancestor of the worktree's root (git's
    `.claude/worktrees/` layout), the worktree's own file hides the main repository's file of the
    same name. A worktree elsewhere, or one with no context file of its own, hides nothing."""
    root = next((d for d in (cwd, *cwd.parents) if os.path.lexists(d / ".git")), None)
    if root is None or not (root / ".git").is_file():
        return None
    git_dir = _git_dir(root / ".git")
    if git_dir is None:
        return None
    try:
        link = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except FileNotFoundError:  # not a linked worktree (a submodule's git directory)
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise _git_unreadable(git_dir / "commondir", e) from e
    common = (git_dir / link).resolve()
    main = common.parent
    own = _context_file_in(root)
    if common.name != ".git" or main == root or not root.is_relative_to(main) or own is None:
        return None
    return (main / own.name).resolve()


def _git_dir(dot_git: Path) -> Path | None:
    """Where a worktree's `.git` file points (`gitdir: PATH`), or `None` if it is not that."""
    try:
        text = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise _git_unreadable(dot_git, e) from e
    if not text.startswith("gitdir:"):
        return None
    return (dot_git.parent / text.removeprefix("gitdir:").strip()).resolve()
