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

from tau._attachments import quoted
from tau._project_read import (
    FOREIGN_BASES,
    PROMPTS_DIR,
    SKILL_FILE,
    SKILLS_DIR,
    foreign_candidates,
    prompt_candidates,
    safe_text,
)
from tau._protocols import EXTENSION_NAME

_PLACEHOLDER = re.compile(r"\$(\$|ARGUMENTS|[1-9][0-9]*|\{[1-9][0-9]*\})")
_FENCE = "---"
_KNOWN = ("name", "description", "argument-hint", "disable-model-invocation")
_HEADER = re.compile(r"([|>])([1-9][-+]?|[-+][1-9]?|)(?:[ \t]+#.*)?")  # a block scalar's first line
_NESTED = re.compile(r"-(?:\s|$)|[^\s#][^:]*:(?:\s|$)")  # a line that opens a list item or a key
_COMMENT = re.compile(r"(?:^|[ \t])#")  # where a comment starts in a plain value
_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.DOTALL)
_HEX = ("x", "u", "U")
_BOOLEANS = {
    "true": True,
    "True": True,
    "TRUE": True,
    "false": False,
    "False": False,
    "FALSE": False,
}
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": "\u2028", "P": "\u2029",
}  # fmt: skip
_TEMPLATE_SUFFIX = ".md"
_QUOTE = '"'


SKILL_PATHS = "A relative path in a skill starts from the directory of its file."
"""What the model is told wherever a skill reaches it (`/NAME` and the skills list): a skill's own
`@path` or relative path is never resolved for it (attachments expand only what a person typed),
so the model reads the files itself."""


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
            block = f'<skill name="{quoted(self.name)}" path="{quoted(str(self.path))}">\n'
            block += f"{self.body}\n</skill>\n{SKILL_PATHS[:-1]}: {self.path.parent}."
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
            number = int(key.strip("{}"))
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


# -- reading a file: the frontmatter, the YAML a Claude Code skill writes


def _frontmatter(text: str, where: Path) -> tuple[dict[str, str], str]:
    """The keys between the `---` lines that open `text`, and the rest of it.

    The frontmatter is read as the block mapping of YAML that a Claude Code skill writes: plain,
    `'single'` and `"double"` quoted values (on one line or folded over several), `|` and `>` block
    scalars, and comments. Only the values of the four keys `_KNOWN` names are read, so any other
    key, whatever its value holds, never stops a file. Of those four, a list or a mapping
    (indented, or `- item` lines under it) is read as `""`, and a flow value (`[a, b]`) as its
    text, as an `argument-hint: [file]` needs."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].strip() != _FENCE:
        return {}, "\n".join(lines)
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == _FENCE), None)
    if end is None:
        raise SkillFileError(f"{where}: the frontmatter opens with --- and never closes")
    found = {
        key: _value(first, rest, f"{where}:{number}", key)
        for number, key, first, rest in _keys(lines[1:end], where)
        if key in _KNOWN
    }
    for key in _KNOWN:
        if key in found and not found[key]:
            raise SkillFileError(f"{where}: `{key}` must be text, not empty, a list or a mapping")
    return found, "\n".join(lines[end + 1 :])


def _keys(lines: list[str], where: Path) -> list[tuple[int, str, str, list[str]]]:
    """Each top-level key of a frontmatter's `lines`: its line number, the key, the text after its
    colon, and the lines that follow it (indented, blank, or `- item` under an empty value)."""
    found: list[tuple[int, str, str, list[str]]] = []
    open_ = False  # whether the lines that follow still belong to the last key
    for number, line in enumerate(lines, 2):
        if not line.strip() or line[0] in " \t" or (open_ and _item_of_empty(line, found)):
            if open_:
                found[-1][3].append(line)
            continue
        if line.startswith("#"):
            open_ = False
            continue
        key, colon, value = line.partition(":")
        if not colon or not key.strip():
            raise SkillFileError(f"{where}:{number}: expected `key: value`, got {line!r}")
        found.append((number, _unquoted(key.strip()), value.strip(), []))
        open_ = True
    return found


def _item_of_empty(line: str, found: list[tuple[int, str, str, list[str]]]) -> bool:
    """Whether `line` is a `- item` of a list written at the key's own indent (`key:` then
    `- a`), which YAML allows."""
    return line[0] == "-" and line[1:2] in ("", " ") and bool(found) and not found[-1][2]


def _value(first: str, rest: list[str], where: str, key: str) -> str:
    """The text of one key's value: `first`, what follows its colon, and `rest`, its lines."""
    if first[:1] in ("'", '"'):
        return _quoted(first, rest, where, key)
    header = _HEADER.fullmatch(first)
    if header:
        return _block(header.group(1), header.group(2), rest)
    if not first and _nested(rest):
        return ""
    return _plain([first, *rest])


def _nested(rest: list[str]) -> bool:
    """Whether the first line of `rest` with text opens a list item or a key: the value is a list
    or a mapping, not text that goes on from the line of the key."""
    first = next((s for s in (line.strip() for line in rest) if s and s[0] != "#"), "")
    return bool(first) and bool(_NESTED.match(first))


def _plain(parts: list[str]) -> str:
    """A plain value over one or more lines: folded, and ended by a comment."""
    lines: list[str] = []
    for part in parts:
        text = part.strip()
        comment = _COMMENT.search(text)
        if comment:
            lines.append(text[: comment.start()].rstrip())
            break
        lines.append(text)
    return _folded(lines).strip()


def _block(style: str, indicators: str, rest: list[str]) -> str:
    """A `|` (lines kept) or `>` (lines folded) block scalar of the lines `rest`. Its chomping
    indicator is read and has no effect, since the value is stripped."""
    texts = [line for line in rest if line.strip()]
    if not texts:
        return ""
    digits = [c for c in indicators if c.isdigit()]
    indent = int(digits[0]) if digits else len(texts[0]) - len(texts[0].lstrip(" "))
    lines = [line[min(indent, len(line) - len(line.lstrip(" "))) :] for line in rest]
    lines = [line if line.strip() else "" for line in lines]
    text = "\n".join(lines) if style == "|" else _folded(lines)
    return text.strip()


def _folded(lines: list[str]) -> str:
    """`lines` folded as YAML folds them: a line break between two lines of text is a space, each
    blank line is a line break, and a line that starts with a space keeps the breaks around it."""
    text, breaks, kept_before = "", 0, False
    for line in lines:
        if not line:
            breaks += 1
            continue
        kept = line[0] in " \t"
        if text:
            text += "\n" * (breaks + 1) if kept or kept_before else "\n" * breaks or " "
        text, breaks, kept_before = text + line, 0, kept
    return text


def _quoted(first: str, rest: list[str], where: str, key: str) -> str:
    """A `'single'` or `"double"` quoted value that opens in `first` and may go on into `rest`.
    What follows the closing quote is not read."""
    mark = first[0]
    text = "\n".join([first[1:], *rest])
    close = _closing(text, mark)
    if close is None:
        raise SkillFileError(f"{where}: the {mark} that opens `{key}` never closes")
    lines = text[:close].split("\n")
    if len(lines) > 1:
        joined: list[str] = []
        for line in (line.strip() for line in lines):
            if joined and joined[-1] and mark == '"' and _escapes_break(joined[-1]):
                joined[-1] = joined[-1][:-1] + line
            else:
                joined.append(line)
        lines = [_folded(joined)]
    if mark == "'":
        return lines[0].replace("''", "'")
    return _unescaped(lines[0], where, key)


def _closing(text: str, mark: str) -> int | None:
    """Where the quote `mark` that closes `text` is (`text` starts after the opening one)."""
    escape = "\\" if mark == '"' else mark  # `\x` in double quotes, `''` in single ones
    i = 0
    while i < len(text):
        if text[i] == escape and (mark == '"' or text[i + 1 : i + 2] == mark):
            i += 2
        elif text[i] == mark:
            return i
        else:
            i += 1
    return None


def _escapes_break(line: str) -> bool:
    """Whether a line of a double-quoted value ends with a `\\` that escapes its line break."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _unescaped(text: str, where: str, key: str) -> str:
    """A double-quoted value's escapes (`\\n`, `\\"`, `\\u00e9`, ...) replaced by what they are."""

    def one(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] in _HEX and len(code) > 1:
            try:
                return chr(int(code[1:], 16))
            except ValueError:
                pass
        elif code in _ESCAPES:
            return _ESCAPES[code]
        raise SkillFileError(f"{where}: `{key}` has an unknown escape, \\{code}")

    return _ESCAPE.sub(one, text)


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":  # noqa: PLR2004
        return value[1:-1]
    return value


def _flag(meta: Mapping[str, str], where: Path) -> bool:
    given = meta.get("disable-model-invocation", "false")
    if given not in _BOOLEANS:
        raise SkillFileError(f"{where}: `disable-model-invocation` is {given!r}, not true or false")
    return not _BOOLEANS[given]


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
    if parts[0] in FOREIGN_BASES:
        name = parts[2]
    else:
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
    if len(parts) == 4:  # noqa: PLR2004
        fix = f"move it to {SKILLS_DIR}/{parts[2]}/{SKILL_FILE}; subdirectories are not read"
        return Problem(f"{path}: misplaced skill: {fix}", parts[2])
    fix = f"rename it {SKILL_FILE}, in capitals"
    return Problem(f"{path}: misplaced skill: {fix}", parts[1])


def _parts(path: Path, base: Path, siblings: Path | None) -> tuple[str, ...]:
    """`path` below `base`, or else below `siblings` (where `.agents` and `.claude` are)."""
    if siblings is None or path.is_relative_to(base):
        return path.relative_to(base).parts
    return path.relative_to(siblings).parts


def project_paths(files: Mapping[str, bytes], tau: Path) -> dict[Path, bytes]:
    """The paths of `Trusted.prompt_files`: those starting with `.agents` or `.claude` are below
    the project, the others below its `.tau/` (`tau`)."""
    return {
        (tau.parent if relative.split("/")[0] in FOREIGN_BASES else tau) / relative: raw
        for relative, raw in files.items()
    }


def read_row(
    files: Mapping[Path, bytes], base: Path, siblings: Path | None = None
) -> tuple[dict[str, PromptFile], list[Problem]]:
    """The prompts among `files` (path to bytes; those under `base/prompts/` and `base/skills/`
    that `prompt_candidates` finds, and the skills under `siblings/.agents/skills/` and
    `siblings/.claude/skills/`), and every problem: a file that cannot be used, one in the wrong
    place, and a name that is both a template and a skill (neither is then a prompt). A skill of
    `.agents` or `.claude` gives way to one of the same name in `base/skills/`, and `.claude`'s to
    `.agents`'s, with no message: the same skill is often in both."""
    found: dict[str, PromptFile] = {}
    others: dict[str, PromptFile] = {}
    problems: list[Problem] = []
    clashing: set[str] = set()
    for path, raw in sorted(files.items()):
        parts = _parts(path, base, siblings)
        try:
            if parts[0] in FOREIGN_BASES:
                skill = _read_file("skill", parts[2], raw, path)
                others.setdefault(skill.name, skill)
                continue
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
    for name, skill in others.items():
        found.setdefault(name, skill)
    return {n: p for n, p in found.items() if n not in clashing}, problems


def _lister(base: Path) -> tuple[Callable[[str], list[str]], Callable[[str], bool]]:
    """`prompt_candidates`' two questions answered from the filesystem under `base`."""

    def names(relative: str) -> list[str]:
        try:
            return sorted(n for n in os.listdir(base / relative) if n[0] != ".")
        except (FileNotFoundError, NotADirectoryError):
            return []

    return names, lambda relative: (base / relative).is_dir()


def _candidates(base: Path, siblings: Path | None) -> list[tuple[Path, str]]:
    """Every file `prompt_candidates` finds under `base`, and `foreign_candidates` under
    `siblings`, with the directory each path is relative to."""
    found = [(base, r) for r in prompt_candidates(*_lister(base))]
    if siblings is not None:
        found += [(siblings, r) for r in foreign_candidates(*_lister(siblings))]
    return found


def user_files(home: Path, siblings: Path | None = None) -> dict[Path, bytes]:
    """The user's template and skill files in `home` (`TAU_HOME`) and the skills in `siblings`
    (their home directory), and the files that look like one in the wrong place."""
    found: dict[Path, bytes] = {}
    for base, relative in _candidates(home, siblings):
        if (base / relative).is_file():
            found[base / relative] = (base / relative).read_bytes()
    return found


def signature(base: Path, siblings: Path | None = None) -> tuple[tuple[str, int, int], ...]:
    """What a stat of every file `prompt_candidates` finds under `base` (and `foreign_candidates`
    under `siblings`) says (its path, modified time and size): it differs when a file is added,
    removed or edited."""
    found: list[tuple[str, int, int]] = []
    for where, relative in _candidates(base, siblings):
        try:
            info = (where / relative).stat()
        except OSError:
            found.append((relative, 0, -1))
        else:
            found.append((relative, info.st_mtime_ns, info.st_size))
    return tuple(found)


def problems_in(files: Mapping[str, bytes], tau: Path) -> list[str]:
    """Every problem with a project's prompt files (`Trusted.prompt_files`, relative to `tau`,
    the `.tau/` directory), as lines safe to print."""
    return [safe_text(p.message) for p in read_row(project_paths(files, tau), tau, tau.parent)[1]]
