"""Skills: ``<name>/SKILL.md`` directories in the Agent Skills format.

Reference: docs/PROMPT-TEMPLATES-AND-SKILLS.md §5.

A skill reaches the model two ways. The system prompt lists every skill's name,
description and file (:func:`render_skills_block`) and the model reads the file
when a task matches — progressive disclosure, so only the descriptions are paid
for on every request. Or the user types ``/skill:<name> args`` and the file's
body is put in the prompt (:func:`expand_skill`).

The format is the one Claude Code and the Agent Skills specification use, so a
``~/.claude/skills`` directory loads as it is when named as a path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from tau_llm.docs import agent_facing

from tau_agent_core.resources import (
    BrokenResource,
    ResourceError,
    ResourceLocations,
    ResourceScope,
    ResourceSource,
    parse_frontmatter,
    read_resource_text,
    resource_locations,
)

SKILL_FILE = "SKILL.md"
"""The file that makes a directory a skill."""

SKILL_PREFIX = "skill:"
"""What a skill's command word begins with: ``/skill:pdf`` invokes the ``pdf`` skill."""

SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""The Agent Skills name rule: lowercase words joined by single hyphens."""

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024

SKILLS_PREAMBLE = (
    "The following skills provide specialized instructions for specific tasks. "
    "Use the read tool to load a skill's file when the task matches its description. "
    "When a skill file references a relative path, resolve it against the directory "
    "that holds the skill file and use that absolute path in tool commands."
)
"""The sentences ahead of the ``<available_skills>`` list in the system prompt."""


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class Skill:
    """One skill, read and ready to list or invoke.

    Attributes:
        name: The frontmatter ``name``, else the directory name.
        description: The frontmatter ``description``; required.
        body: ``SKILL.md`` after its frontmatter.
        model_invocable: ``False`` when the frontmatter says
            ``disable-model-invocation: true``; such a skill is left out of the
            system prompt and runs only as ``/skill:<name>``.
        source: Where ``SKILL.md`` was read from.
    """

    name: str
    description: str
    body: str
    model_invocable: bool
    source: ResourceSource

    @property
    def directory(self) -> Path:
        """The directory relative references in the skill resolve against."""
        return self.source.path.parent


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class SkillCatalog:
    """Every skill a session answers, after first-wins resolution.

    Attributes:
        entries: One per name, strongest scope first.
        shadowed: Entries that lost their name to a stronger scope.
    """

    entries: tuple[Skill | BrokenResource, ...] = ()
    shadowed: tuple[Skill | BrokenResource, ...] = field(default=())

    def get(self, name: str) -> Skill | BrokenResource | None:
        """The entry that answers ``name``.

        Args:
            name: The skill's name, without the ``skill:`` prefix.

        Returns:
            The skill or broken entry holding ``name``, or ``None``.
        """
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def listed(self) -> list[Skill]:
        """The skills the system prompt lists.

        Returns:
            Every working skill that is model-invocable, in catalog order.
        """
        return [e for e in self.entries if isinstance(e, Skill) and e.model_invocable]


EMPTY_SKILLS = SkillCatalog()


def _read_skill(path: Path, scope: ResourceScope) -> Skill | BrokenResource:
    """One ``SKILL.md`` as a skill, or as a broken entry holding the directory's name."""
    source = ResourceSource(path=path, scope=scope)
    fallback = path.parent.name
    try:
        parsed = parse_frontmatter(read_resource_text(path))
        name = parsed.text("name") or fallback
        description = parsed.text("description") or ""
        disable = (parsed.text("disable-model-invocation") or "false").strip().lower()
    except ResourceError as exc:
        return BrokenResource(name=fallback, source=source, error=str(exc))
    problem = None
    if len(name) > MAX_NAME_CHARS or SKILL_NAME.match(name) is None:
        problem = f"name {name!r} is not lowercase words joined by single hyphens, 1-64 chars"
    elif not description.strip():
        problem = "frontmatter has no description, which is what the model chooses a skill by"
    elif len(description) > MAX_DESCRIPTION_CHARS:
        problem = f"description is {len(description)} chars, over {MAX_DESCRIPTION_CHARS}"
    elif disable not in ("true", "false"):
        problem = f"disable-model-invocation is {disable!r}, not true or false"
    if problem is not None:
        return BrokenResource(name=name, source=source, error=problem)
    return Skill(
        name=name,
        description=description.strip(),
        body=parsed.body.strip("\n"),
        model_invocable=disable != "true",
        source=source,
    )


def _skill_files(root: Path, scope: ResourceScope) -> list[Path]:
    """The ``SKILL.md`` files under one location.

    A location is one skill when it holds ``SKILL.md`` itself (or is that file),
    and otherwise a directory of skills, each an immediate subdirectory holding
    one. Not recursive.

    Raises:
        ResourceError: An explicit path does not exist, or names neither a skill
            nor a directory of skills.
    """
    if root.is_file() and root.name == SKILL_FILE:
        return [root]
    if root.is_dir():
        if (root / SKILL_FILE).is_file():
            return [root / SKILL_FILE]
        return sorted(p / SKILL_FILE for p in root.iterdir() if (p / SKILL_FILE).is_file())
    if scope != "explicit":
        return []
    raise ResourceError(f"skill path {root} is not a {SKILL_FILE} or a directory")


@agent_facing(topic="sdk")
def load_skills(
    locations: ResourceLocations | None = None,
    *,
    cwd: str | Path | None = None,
    paths: Iterable[str | Path] = (),
    discover: bool = True,
) -> SkillCatalog:
    """Discover and read every skill, first name wins.

    Order: explicit paths, then ``<τ home>/skills/``, then ``<cwd>/.tau/skills/``
    when :data:`~tau_agent_core.resources.PROJECT_SCOPE_ADMITTED` admits it —
    which, until the trust gate exists, it does not.

    Args:
        locations: Where to look. ``None`` builds them from the other arguments.
        cwd: The working directory, when ``locations`` is ``None``.
        paths: Explicit skill directories, ``SKILL.md`` files or directories of
            skills, when ``locations`` is ``None``.
        discover: Whether to read the user and project directories.

    Raises:
        ResourceError: An explicit path does not exist.

    Returns:
        The catalog, one entry per name.
    """
    if locations is None:
        locations = resource_locations("skills", cwd=cwd, paths=paths, discover=discover)
    entries: list[Skill | BrokenResource] = []
    shadowed: list[Skill | BrokenResource] = []
    seen_names: set[str] = set()
    seen_files: set[Path] = set()
    for root, scope in locations.roots():
        for path in _skill_files(root, scope):
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            entry = _read_skill(resolved, scope)
            if entry.name in seen_names:
                shadowed.append(entry)
            else:
                seen_names.add(entry.name)
                entries.append(entry)
    return SkillCatalog(entries=tuple(entries), shadowed=tuple(shadowed))


@agent_facing(topic="sdk")
def render_skills_block(skills: Iterable[Skill]) -> str:
    """The system prompt's skill listing, or ``""`` when there is nothing to list.

    Names each skill's file by absolute path, the way ``<project_context>`` names
    every context file, so the model can ``read`` it.

    Args:
        skills: The skills to list, normally ``SkillCatalog.listed()``.

    Returns:
        The block, or ``""`` for no skills.
    """
    listed = list(skills)
    if not listed:
        return ""
    lines = [SKILLS_PREAMBLE, "", "<available_skills>"]
    for skill in listed:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape(skill.name)}</name>")
        lines.append(f"    <description>{escape(skill.description)}</description>")
        lines.append(f"    <location>{escape(str(skill.source.path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


@agent_facing(topic="sdk")
def expand_skill(skill: Skill, args: str) -> str:
    """The prompt ``/skill:<name> args`` sends: the skill's body, then ``args``.

    The body is wrapped in a ``<skill>`` element naming its file and directory, so
    the model can resolve the skill's relative references.

    Args:
        skill: The skill invoked.
        args: Everything typed after ``/skill:<name>``.

    Returns:
        The prompt text.
    """
    block = (
        f"<skill name={quoteattr(skill.name)} location={quoteattr(str(skill.source.path))}>\n"
        f"References are relative to {skill.directory}.\n\n"
        f"{skill.body}\n"
        "</skill>"
    )
    return f"{block}\n\n{args}" if args else block
