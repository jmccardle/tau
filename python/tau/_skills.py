"""`Skills`: the built-in extension for prompt templates and skills (D24 section 6).

A **template** is a file `NAME.md`: the command `/NAME args` sends its text, with `$ARGUMENTS` (the
whole argument text) and `$N` (its words: whitespace splits, `"` groups, `'` is a character; a
missing one is empty, `$$` is `$`) replaced. A body with no placeholder gets the arguments after a
blank line. A **skill** is a directory `NAME/SKILL.md`: `/NAME args` sends
`<skill name="NAME" path="/abs/NAME/SKILL.md">`, the body and `</skill>`, then a line telling the
model that a relative path in the skill starts from the skill's directory (the skills list says
the same; nothing in the body is attached for it), then the arguments. Either goes to the cursor
as input authored by `Extension("skills")`, so `pre_input` hooks see it and `attachments` does not
expand it (only what a person typed); a command's `stdin`, which
`tau -p` fills, follows it after a blank line. A generated command gives its bare name up
(`yields_names`): when any other extension also has a command `/NAME`, `/NAME` is that one, and
`/skills:NAME` is always the file.

Where they are read: `$TAU_HOME/prompts/*.md` and `$TAU_HOME/skills/*/SKILL.md`, and the same
under the trusted project's `.tau/`, from the bytes trust approved (`Trusted.prompt_files`). They
are read when the extensions load and again, by `rescan`, when a stat says a file was added,
removed or edited: a running `tau` or `tau serve` sees them before it runs or lists a command.
A project file that is no longer what trust approved is not used; its command says to run
`tau trust`. For one name the project wins over the user. Every problem (a template and a skill
with one name in one directory, a name that is not ASCII letters, digits, `_` and `-`, a file in
the wrong place) is listed at once, and stops the start.

A file may start with frontmatter, `key: value` lines between two `---` lines (no YAML): `name`
(if given, it must be the file's name), `description` (a template's default is its first body
line; a skill must have one), `argument-hint` (what the argument is, shown in the command's form)
and `disable-model-invocation` (`true`: the skill is left out of the list below). Other keys are
ignored, and so are indented lines, so a Claude Code skill loads as it is. A byte order mark
before the frontmatter is ignored.

For the model: when a session's first step begins (its walk is the prefix and nothing else) and a
`read` tool is offered, `pre_step` writes one `System` node listing each skill's name,
description and path. The list is then part of the prefix, so the cache stays stable; a skill
added later reaches new sessions only (though `/name` runs it at once). The option `advertise`
(default true) turns it off.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated
from xml.sax.saxutils import escape

from tau._commands import CommandFailed
from tau._core import Body
from tau._handles import Cursor, Subject
from tau._project_read import TrustError
from tau._prompt_files import (
    SKILL_PATHS,
    Problem,
    PromptFile,
    SkillFileError,
    command_name,
    read_row,
    signature,
    user_files,
)
from tau._startup import LoadedConfig
from tau._trust import TAU_DIR, ensure_trusted

LIST_HEAD = "Skills are instructions for specific tasks. When a task matches a skill's description,"
"""The start of the list's text; a walk that holds a `System` node starting so has been told."""
_DEFAULT_HINT = "arguments"

__all__ = ["LIST_HEAD", "Options", "PromptFile", "SkillFileError", "Skills", "listing"]


@dataclass(frozen=True)
class Options:
    """`Skills`' options (`extensions.skills`; `/config skills`)."""

    advertise: Annotated[bool, "write the skills list for the model at a session's first step"] = (
        True
    )


def _read(
    loaded: LoadedConfig, *, recheck: bool
) -> tuple[dict[str, PromptFile | str], list[Problem], bool]:
    """What each command name is, from the user's files and the trusted project's: its prompt, or
    the message of why it cannot be run (for a name whose file is broken). The project's files
    are the bytes trust approved; with `recheck`, if what is on disk is not them, the project's
    names are all the message that says to run `tau trust` (the last item says so: it is not
    cached, since running `tau trust` changes nothing in the project's files)."""
    by_name: dict[str, PromptFile | str] = {}
    user, problems = read_row(user_files(loaded.home), loaded.home)
    by_name.update(user)
    by_name.update({p.name: p.message for p in problems if p.name})
    project = loaded.project
    if project is None:
        return by_name, problems, False
    base = project.root / TAU_DIR
    try:
        files = (
            ensure_trusted(project.root, loaded.home, ask=None).prompt_files
            if recheck
            else project.prompt_files
        )
    except TrustError as e:
        stale = str(e)
        approved = {base / relative: raw for relative, raw in project.prompt_files.items()}
        names = {command_name(p.relative_to(base).parts) for p in approved}
        names |= {command_name(tuple(r.split("/"))) for r, _, _ in signature(base)}
        by_name.update({n: stale for n in names if n})
        return by_name, [*problems, Problem(stale, None)], True
    row, found = read_row({base / relative: raw for relative, raw in files.items()}, base)
    by_name.update(row)
    by_name.update({p.name: p.message for p in found if p.name})
    return by_name, [*problems, *found], False


def _command(name: str, item: PromptFile | str) -> Callable[..., object]:
    """The command `/name`: sends the expanded text of a prompt to the cursor it runs on, or, for
    a name whose file cannot be used, refuses with why."""

    async def run(cursor: Cursor, args: str = "", stdin: str = "") -> str | None:
        if isinstance(item, str):
            raise CommandFailed(item)
        try:
            text = item.expand(args)
        except ValueError as e:
            raise CommandFailed(str(e)) from None
        cursor.send(cursor.id, f"{text}\n\n{stdin}" if stdin else text)
        return None

    run.__name__ = run.__qualname__ = name
    run.__doc__ = f"unusable: {item.splitlines()[0]}" if isinstance(item, str) else item.description
    hint = _DEFAULT_HINT if isinstance(item, str) or not item.hint else item.hint
    run.__annotations__ = {
        "cursor": Cursor,
        "args": Annotated[str, hint],
        "stdin": Annotated[str, "text that follows the expansion (`tau -p` standard input)"],
        "return": str | None,
    }
    return run


def listing(skills: Sequence[PromptFile]) -> str:
    """The text of the `System` node that tells the model about `skills`."""
    lines = [
        f"{LIST_HEAD} read its file with the read tool and follow it. {SKILL_PATHS}",
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
    for the model (the module docstring). `prompts` are what `Skills.of` found; an extension made
    of `prompts` alone reads no files again."""

    name = "skills"
    options = Options
    yields_names = True

    def __init__(
        self, prompts: Sequence[PromptFile], *, loaded: LoadedConfig | None = None
    ) -> None:
        self.prompts = tuple(prompts)
        self.commands: tuple[Callable[..., object], ...] = ()
        self._by_name: Mapping[str, PromptFile | str] = {e.name: e for e in prompts}
        self._text = (
            listing([e for e in prompts if e.listed]) if any(e.listed for e in prompts) else ""
        )
        self._loaded = loaded
        self._seen: tuple[object, ...] | None = self._stat()
        self._make_commands()

    @classmethod
    def of(cls, loaded: LoadedConfig) -> "Skills":
        """The extension for the files `loaded` can read. `SkillFileError`, naming every problem
        with a file, if any cannot be used."""
        by_name, problems, _ = _read(loaded, recheck=False)
        if problems:
            raise SkillFileError("\n".join(p.message for p in problems))
        found = cls(
            [p for _, p in sorted(by_name.items()) if isinstance(p, PromptFile)], loaded=loaded
        )
        return found

    def _stat(self) -> tuple[object, ...]:
        loaded = self._loaded
        if loaded is None:
            return ()
        project = () if loaded.project is None else signature(loaded.project.root / TAU_DIR)
        return (signature(loaded.home), project)

    def _make_commands(self) -> None:
        self.commands = tuple(_command(n, item) for n, item in sorted(self._by_name.items()))

    def rescan(self) -> bool:
        """Read the template and skill files again if a stat says one was added, removed or
        edited since the last time; whether the commands changed. A file that cannot be used is
        a command that says why when it is run, and the trusted project's files, if what is on
        disk is no longer what trust approved, all say to run `tau trust`. The skills list the
        model sees is not rewritten (D24 section 6)."""
        loaded = self._loaded
        if loaded is None:
            return False
        now = self._stat()
        if now == self._seen:
            return False
        self._by_name, _, stale = _read(loaded, recheck=True)
        self._seen = None if stale else now
        self._make_commands()
        return True

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
