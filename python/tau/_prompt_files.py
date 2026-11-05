"""Reading template and skill files (D24 section 6): what a `PromptFile` is, how a file's bytes
become one, how the arguments of a template are read, and every problem a set of files has.

`read_row` reads the files under one `.tau/`-like base and returns the prompts and ALL the
problems at once (a `Problem` is a message and, when the file's name is a command's name, that
name), so the start, `tau trust` and a reload say everything wrong in one go. A file that is in
the wrong place (`prompt_candidates`) is a problem, not an ignored file.
"""

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tau._attachments import candidates, quoted
from tau._project_read import (
    PROMPTS_DIR,
    SKILL_FILE,
    SKILLS_DIR,
    prompt_candidates,
    safe_text,
)
from tau._protocols import EXTENSION_NAME

_PLACEHOLDER = re.compile(r"\$(\$|ARGUMENTS|[1-9][0-9]*)")
_FENCE = "---"
_KNOWN = ("name", "description", "argument-hint", "disable-model-invocation")
_BLOCK = ("|", ">")
_TEMPLATE_SUFFIX = ".md"
_QUOTE = '"'
_SKILL_PATH = re.compile(r"(?<!\S)@(\S+)")


class SkillFileError(ValueError):
    """A prompt template or skill file that cannot be used; the message names the file (several
    lines, one per problem, when there are several)."""


@dataclass(frozen=True)
class Problem:
    """What is wrong with one file: the `message` (it names the file) and the command `name` the
    file would have had (`None`: it has no usable name, so no command)."""

    message: str
    name: str | None


@dataclass(frozen=True)
class PromptFile:
    """One template or skill, read: its `kind`, `name`, the `description` that is the command's
    help, the `hint` about its argument (`""`: none given), its `body`, the `path` of its file,
    and whether the model is told about it (`listed`, skills only)."""

    kind: Literal["template", "skill"]
    name: str
    description: str
    hint: str
    body: str
    path: Path
    listed: bool

    def expand(self, args: str) -> str:
        """The text `/name args` sends. `ValueError`, worded as the command's refusal, for a
        template whose arguments cannot be split into words (an unbalanced `"`) or that would
        send nothing."""
        if self.kind == "skill":
            body = _from_skill_directory(self.body, self.path.parent)
            block = f'<skill name="{quoted(self.name)}" path="{quoted(str(self.path))}">\n'
            block += f"{body}\n</skill>"
            return f"{block}\n\n{args}" if args else block
        words: list[str] | None = None

        def fill(match: re.Match[str]) -> str:
            nonlocal words
            key = match.group(1)
            if key == "$":
                return "$"
            if key == "ARGUMENTS":
                return args
            if words is None:
                words = self._words(args)
            number = int(key)
            return words[number - 1] if number <= len(words) else ""

        text = _PLACEHOLDER.sub(fill, self.body)
        if args and all(m.group(1) == "$" for m in _PLACEHOLDER.finditer(self.body)):
            text = f"{text}\n\n{args}"
        return self._sendable(text)

    def _words(self, args: str) -> list[str]:
        try:
            return words_of(args)
        except ValueError as e:
            raise ValueError(self._refusal(str(e))) from None

    def _sendable(self, text: str) -> str:
        if not text.strip():
            raise ValueError(self._refusal("nothing to send: the template is empty"))
        return text

    def _refusal(self, reason: str) -> str:
        usage = f" (usage: /{self.name} {self.hint})" if self.hint else ""
        return f"/{self.name}: {reason}{usage}"


def words_of(args: str) -> list[str]:
    """The words of `args`: split on whitespace, with `"` grouping words (the quotes go) and every
    other character, a `'` and a `\\` too, as it is. `ValueError` for a `"` never closed."""
    words: list[str] = []
    current: list[str] = []
    started = quoting = False
    for ch in args:
        if ch == _QUOTE:
            quoting, started = not quoting, True
        elif ch.isspace() and not quoting:
            if started:
                words.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
            started = True
    if quoting:
        raise ValueError('a " in the arguments is never closed')
    if started:
        words.append("".join(current))
    return words


def _from_skill_directory(body: str, directory: Path) -> str:
    """`body` with each `@path` that names a file inside `directory` made absolute, so the
    attachment is that file whatever the session's working directory is."""

    def fix(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith(("/", "~")):
            return match.group(0)
        for word in candidates(token):
            if (directory / word).is_file():
                return f"@{directory / token}"
        return match.group(0)

    return _SKILL_PATH.sub(fix, body)


# -- reading a file


def _frontmatter(text: str, where: Path) -> tuple[dict[str, str], str]:
    """The `key: value` lines between the `---` lines that open `text`, and the rest of it."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].strip() != _FENCE:
        return {}, "\n".join(lines)
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == _FENCE), None)
    if end is None:
        raise SkillFileError(f"{where}: the frontmatter opens with --- and never closes")
    found: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t":
            continue
        key, colon, value = line.partition(":")
        if not colon or not key.strip():
            raise SkillFileError(f"{where}:{number}: expected `key: value`, got {line!r}")
        found[key.strip()] = _unquoted(value.strip())
    for key in _KNOWN:
        if key in found and (not found[key] or found[key][0] in _BLOCK):
            raise SkillFileError(f"{where}: `{key}` must be one line of text after the colon")
    return found, "\n".join(lines[end + 1 :])


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":  # noqa: PLR2004
        return value[1:-1]
    return value


def _flag(meta: Mapping[str, str], where: Path) -> bool:
    given = meta.get("disable-model-invocation", "false")
    if given not in ("true", "false"):
        raise SkillFileError(f"{where}: `disable-model-invocation` is {given!r}, not true or false")
    return given == "false"


def _read_file(kind: Literal["template", "skill"], name: str, raw: bytes, path: Path) -> PromptFile:
    """The template or skill `name` that `raw`, the bytes of `path`, is."""
    if not EXTENSION_NAME.fullmatch(name):
        raise SkillFileError(
            f"{path}: the name {name!r} is not ASCII letters, digits, `_` and `-`: rename it"
        )
    try:
        text = raw.decode("utf-8-sig")  # a byte order mark is not text
    except UnicodeDecodeError as e:
        raise SkillFileError(f"{path}: not valid UTF-8") from e
    meta, body = _frontmatter(text, path)
    if meta.get("name", name) != name:
        raise SkillFileError(f"{path}: `name: {meta['name']}` is not the file's name, {name!r}")
    body = body.strip("\n").rstrip()
    first = next((line.strip() for line in body.split("\n") if line.strip()), "")
    description = meta.get("description") or (first if kind == "template" else "")
    if not description:
        what = "a skill needs a `description`" if kind == "skill" else "it has no text"
        raise SkillFileError(f"{path}: no description, and {what}")
    hint = meta.get("argument-hint", "")
    return PromptFile(
        kind, name, description, hint, body, path, kind == "skill" and _flag(meta, path)
    )


def command_name(parts: tuple[str, ...]) -> str | None:
    """The command a file of a `prompt_candidates` path would be, if its name were right: the
    template's stem, the skill's directory (`None`: it is not a usable name)."""
    name = parts[1] if parts[0] == SKILLS_DIR and len(parts) == 3 else Path(parts[-1]).stem  # noqa: PLR2004
    return name if EXTENSION_NAME.fullmatch(name) else None


def _misplaced(parts: tuple[str, ...], path: Path) -> Problem:
    """The problem a file found by `prompt_candidates` has when it is not `prompts/NAME.md` or
    `skills/NAME/SKILL.md`."""
    top, name = parts[0], Path(parts[-1]).stem
    if top == PROMPTS_DIR and len(parts) == 2:  # noqa: PLR2004
        fix = f"rename it {name}{_TEMPLATE_SUFFIX}, with `.md` in lower case"
        return Problem(f"{path}: misplaced template: {fix}", name)
    if top == PROMPTS_DIR:
        fix = f"move it to {PROMPTS_DIR}/{name}{_TEMPLATE_SUFFIX}; subdirectories are not read"
        return Problem(f"{path}: misplaced template: {fix}", name)
    if len(parts) == 2:  # noqa: PLR2004
        fix = f"a skill is a directory: move it to {SKILLS_DIR}/{name}/{SKILL_FILE}"
        return Problem(f"{path}: misplaced skill: {fix}", name)
    fix = f"rename it {SKILL_FILE}, in capitals"
    return Problem(f"{path}: misplaced skill: {fix}", parts[1])


def read_row(
    files: Mapping[Path, bytes], base: Path
) -> tuple[dict[str, PromptFile], list[Problem]]:
    """The prompts among `files` (path to bytes; those under `base/prompts/` and `base/skills/`
    that `prompt_candidates` finds), and every problem: a file that cannot be used, one in the
    wrong place, and a name that is both a template and a skill (neither is then a prompt)."""
    found: dict[str, PromptFile] = {}
    problems: list[Problem] = []
    clashing: set[str] = set()
    for path, raw in sorted(files.items()):
        parts = path.relative_to(base).parts
        try:
            if parts[0] == PROMPTS_DIR and len(parts) == 2 and parts[1].endswith(_TEMPLATE_SUFFIX):  # noqa: PLR2004
                prompt = _read_file("template", path.stem, raw, path)
            elif parts[0] == SKILLS_DIR and len(parts) == 3 and parts[2] == SKILL_FILE:  # noqa: PLR2004
                prompt = _read_file("skill", parts[1], raw, path)
            else:
                problems.append(_misplaced(parts, path))
                continue
        except SkillFileError as e:
            problems.append(Problem(str(e), command_name(parts)))
            continue
        before = found.setdefault(prompt.name, prompt)
        if before is not prompt:
            clashing.add(prompt.name)
            problems.append(
                Problem(
                    f"/{prompt.name} is both a {before.kind} ({before.path}) and a {prompt.kind} "
                    f"({prompt.path}): rename one",
                    prompt.name,
                )
            )
    return {n: p for n, p in found.items() if n not in clashing}, problems


def _lister(base: Path) -> tuple[Callable[[str], list[str]], Callable[[str], bool]]:
    """`prompt_candidates`' two questions answered from the filesystem under `base`."""

    def names(relative: str) -> list[str]:
        try:
            return sorted(n for n in os.listdir(base / relative) if n[0] != ".")
        except (FileNotFoundError, NotADirectoryError):
            return []

    return names, lambda relative: (base / relative).is_dir()


def user_files(home: Path) -> dict[Path, bytes]:
    """The user's template and skill files, and those that look like one in the wrong place."""
    found: dict[Path, bytes] = {}
    for relative in prompt_candidates(*_lister(home)):
        if (home / relative).is_file():
            found[home / relative] = (home / relative).read_bytes()
    return found


def signature(base: Path) -> tuple[tuple[str, int, int], ...]:
    """What a stat of every file `prompt_candidates` finds under `base` says (its path, modified
    time and size): it differs when a file is added, removed or edited."""
    found: list[tuple[str, int, int]] = []
    for relative in prompt_candidates(*_lister(base)):
        try:
            info = (base / relative).stat()
        except OSError:
            found.append((relative, 0, -1))
        else:
            found.append((relative, info.st_mtime_ns, info.st_size))
    return tuple(found)


def problems_in(files: Mapping[str, bytes], tau: Path) -> list[str]:
    """Every problem with a project's prompt files (`Trusted.prompt_files`, relative to `tau`,
    the `.tau/` directory), as lines safe to print."""
    row = {tau / relative: raw for relative, raw in files.items()}
    return [safe_text(p.message) for p in read_row(row, tau)[1]]
