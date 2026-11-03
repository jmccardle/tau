"""`Skills`: the built-in extension for prompt templates and skills (D24 section 6).

A **template** is a file `NAME.md`: the command `/NAME args` sends its text, with `$ARGUMENTS` (the
whole argument text) and `$1`...`$9` (its shell words) replaced. A body with no placeholder gets
the arguments after a blank line. A **skill** is a directory `NAME/SKILL.md`: `/NAME args` sends
`<skill name="NAME" path="/abs/NAME/SKILL.md">` and the body, then the arguments. Either goes to
the cursor as input authored by `Extension("skills")`, so `pre_input` hooks (and `attachments`)
see it. A generated command gives its bare name up (`yields_names`): when any other extension
also has a command `/NAME`, `/NAME` is that one, and `/skills:NAME` is always the file.

Where they are read: `$TAU_HOME/prompts/*.md` and `$TAU_HOME/skills/*/SKILL.md`, and the same
under the trusted project's `.tau/`, from the bytes trust approved (`Trusted.prompt_files`). They
are read once, when the extensions load. For one name the project wins over the user. A template
and a skill with one name in one directory are an error naming both, and so is a name that is not
letters, digits, `_` and `-`.

A file may start with frontmatter, `key: value` lines between two `---` lines (no YAML): `name`
(if given, it must be the file's name), `description` (a template's default is its first body
line; a skill must have one), `argument-hint` (what the argument is, shown in the command's form)
and `disable-model-invocation` (`true`: the skill is left out of the list below). Other keys are
ignored, and so are indented lines, so a Claude Code skill loads as it is.

For the model: when a session's first step begins (its walk is the prefix and nothing else) and a
`read` tool is offered, `pre_step` writes one `System` node listing each skill's name,
description and path. The list is then part of the prefix, so the cache stays stable; a skill
added later reaches new sessions only. The option `advertise` (default true) turns it off.
"""

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from xml.sax.saxutils import escape

from tau._attachments import quoted
from tau._core import Body
from tau._handles import Cursor, Subject
from tau._project_read import PROMPTS_DIR, SKILL_FILE, SKILLS_DIR
from tau._protocols import EXTENSION_NAME
from tau._startup import LoadedConfig
from tau._trust import TAU_DIR

_PLACEHOLDER = re.compile(r"\$(ARGUMENTS|[1-9])")
_DEFAULT_HINT = "arguments"
_FENCE = "---"
_KNOWN = ("name", "description", "argument-hint", "disable-model-invocation")
_BLOCK = ("|", ">")
LIST_HEAD = "Skills are instructions for specific tasks. When a task matches a skill's description,"
"""The start of the list's text; a walk that holds a `System` node starting so has been told."""


class SkillFileError(ValueError):
    """A prompt template or skill file that cannot be used; the message names the file."""


@dataclass(frozen=True)
class Options:
    """`Skills`' options (`extensions.skills`; `/config skills`)."""

    advertise: Annotated[bool, "write the skills list for the model at a session's first step"] = (
        True
    )


@dataclass(frozen=True)
class PromptFile:
    """One template or skill, read: its `kind`, `name`, the `description` that is the command's
    help, the `hint` about its argument, its `body`, the `path` of its file, and whether the model
    is told about it (`listed`, skills only)."""

    kind: Literal["template", "skill"]
    name: str
    description: str
    hint: str
    body: str
    path: Path
    listed: bool

    def expand(self, args: str) -> str:
        """The text `/name args` sends. `ValueError` for a template that wants a `$N` the
        arguments do not have, or arguments a `$N` cannot be read from (unbalanced quotes)."""
        if self.kind == "skill":
            block = f'<skill name="{quoted(self.name)}" path="{quoted(str(self.path))}">\n'
            block += f"{self.body}\n</skill>"
            return f"{block}\n\n{args}" if args else block
        if not _PLACEHOLDER.search(self.body):
            return f"{self.body}\n\n{args}" if args else self.body
        return _PLACEHOLDER.sub(_filler(self.name, args), self.body)


def _filler(name: str, args: str) -> Callable[[re.Match[str]], str]:
    """What each placeholder becomes; the arguments are split into words at the first `$N`."""
    words: list[str] | None = None

    def fill(found: re.Match[str]) -> str:
        nonlocal words
        key = found.group(1)
        if key == "ARGUMENTS":
            return args
        if words is None:
            try:
                words = shlex.split(args)
            except ValueError as e:
                raise ValueError(f"/{name}: its arguments cannot be split into words: {e}") from e
        number = int(key)
        if number > len(words):
            raise ValueError(
                f"/{name} uses ${number}, but the arguments have {len(words)} "
                f"word{'' if len(words) == 1 else 's'}: {args!r}"
            )
        return words[number - 1]

    return fill


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
            f"{path}: the name {name!r} is not letters, digits, `_` and `-`: rename it"
        )
    try:
        text = raw.decode("utf-8")
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
    hint = meta.get("argument-hint", _DEFAULT_HINT)
    return PromptFile(
        kind, name, description, hint, body, path, kind == "skill" and _flag(meta, path)
    )


# -- finding them


def _read_row(files: Mapping[Path, bytes], base: Path) -> dict[str, PromptFile]:
    """The prompts among `files` (path to bytes), those under `base/prompts/` and
    `base/skills/`; one name in the row is one prompt or an error naming both files."""
    found: dict[str, PromptFile] = {}
    for path, raw in sorted(files.items()):
        relative = path.relative_to(base).parts
        if relative[0] == PROMPTS_DIR:
            prompt = _read_file("template", path.stem, raw, path)
        else:
            prompt = _read_file("skill", relative[1], raw, path)
        before = found.setdefault(prompt.name, prompt)
        if before is not prompt:
            raise SkillFileError(
                f"/{prompt.name} is both a {before.kind} ({before.path}) and a {prompt.kind} "
                f"({prompt.path}): rename one"
            )
    return found


def _user_files(home: Path) -> dict[Path, bytes]:
    """The user's template and skill files that exist."""
    found: dict[Path, bytes] = {}
    for path in sorted((home / PROMPTS_DIR).glob("*.md")):
        if not path.name.startswith(".") and path.is_file():
            found[path] = path.read_bytes()
    for path in sorted((home / SKILLS_DIR).glob(f"*/{SKILL_FILE}")):
        if not path.parent.name.startswith(".") and path.is_file():
            found[path] = path.read_bytes()
    return found


def read_prompts(loaded: LoadedConfig) -> list[PromptFile]:
    """Every template and skill of the user and the trusted project, the project's winning a name
    both have, by name. `SkillFileError` for a file that cannot be used."""
    prompts = _read_row(_user_files(loaded.home), loaded.home)
    project = loaded.project
    if project is not None and project.prompt_files:
        base = project.root / TAU_DIR
        row = {base / relative: raw for relative, raw in project.prompt_files.items()}
        prompts.update(_read_row(row, base))
    return [prompts[name] for name in sorted(prompts)]


# -- the extension


def _command(prompt: PromptFile) -> Callable[..., object]:
    """The command `/name` of `prompt`: sends the expanded text to the cursor it runs on."""

    async def run(cursor: Cursor, args: str = "") -> str | None:
        cursor.send(cursor.id, prompt.expand(args))
        return None

    run.__name__ = run.__qualname__ = prompt.name
    run.__doc__ = prompt.description
    run.__annotations__ = {
        "cursor": Cursor,
        "args": Annotated[str, prompt.hint],
        "return": str | None,
    }
    return run


def listing(skills: Sequence[PromptFile]) -> str:
    """The text of the `System` node that tells the model about `skills`."""
    lines = [
        f"{LIST_HEAD} read its file with the read tool and follow it. "
        "A relative path in a skill starts from the directory of its file.",
        "",
        "<available_skills>",
    ]
    for skill in skills:
        lines += [
            "  <skill>",
            f"    <name>{escape(skill.name)}</name>",
            f"    <description>{escape(skill.description)}</description>",
            f"    <path>{escape(str(skill.path))}</path>",
            "  </skill>",
        ]
    return "\n".join([*lines, "</available_skills>"])


class Skills:
    """The built-in extension `skills`: a command for each template and skill, and the skills list
    for the model (the module docstring). `prompts` are what `read_prompts` found."""

    name = "skills"
    options = Options
    yields_names = True

    def __init__(self, prompts: Sequence[PromptFile]) -> None:
        self.prompts = tuple(prompts)
        self.commands = tuple(_command(e) for e in prompts)
        self._text = (
            listing([e for e in prompts if e.listed]) if any(e.listed for e in prompts) else ""
        )

    @classmethod
    def of(cls, loaded: LoadedConfig) -> "Skills":
        """The extension for the files `loaded` can read. `SkillFileError` for one it cannot use."""
        return cls(read_prompts(loaded))

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """At a session's first step, write the skills list (the module docstring)."""
        if not self._text or not cursor.options().advertise or "read" not in cursor.tools():
            return None
        node = cursor.node()  # up from the cursor: the first message ends the search
        while True:
            body = node.body
            if not isinstance(body, Body.Config | Body.System):
                return None
            if isinstance(body, Body.System) and body.text.startswith(LIST_HEAD):
                return None
            if node.parent is None:
                break
            node = cursor.node(node.parent)
        cursor.write(Body.System(self._text))
        return None
