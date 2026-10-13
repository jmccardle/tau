"""Prompt templates: ``/name args`` expands a Markdown file into the prompt.

Reference: docs/PROMPT-TEMPLATES-AND-SKILLS.md §4.

A template is ``<name>.md`` in ``~/.tau/prompts/``, ``<cwd>/.tau/prompts/`` or a
path the operator named. Its body is the prompt; ``$ARGUMENTS`` and ``$1``…``$N``
take what was typed after the name. The format is Claude Code's custom-command
format, so a directory of those loads as it is.

Expansion happens in :meth:`~tau_agent_core.agent_session.AgentSession.submit`
(through :func:`~tau_agent_core.commands.expand_prompt_command`), so the text the
model sees, the text persisted and the text every head renders are one string.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

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

TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
"""What a template's file stem must be: one word a reader can type after ``/``.

``:`` is excluded because ``skill:`` and ``ext:`` are prefixes, and ``.`` because
it separates an extension from its command.
"""

DESCRIPTION_FALLBACK_CHARS = 60
"""How much of the first body line stands in for a missing ``description``."""

_PLACEHOLDER = re.compile(
    r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class PromptTemplate:
    """One template, read and ready to expand.

    Attributes:
        name: The command word, the file stem.
        description: The frontmatter ``description``, else the first non-empty
            body line cut to :data:`DESCRIPTION_FALLBACK_CHARS`.
        argument_hint: The frontmatter ``argument-hint``, for a completion list.
        body: The text after the frontmatter, unexpanded.
        source: Where it was read from.
    """

    name: str
    description: str
    argument_hint: str | None
    body: str
    source: ResourceSource


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class PromptTemplateCatalog:
    """Every template a session answers, after first-wins resolution.

    Attributes:
        entries: One per name, strongest scope first; a :class:`BrokenResource`
            holds its name like a working template does.
        shadowed: Entries that lost their name to a stronger scope.
    """

    entries: tuple[PromptTemplate | BrokenResource, ...] = ()
    shadowed: tuple[PromptTemplate | BrokenResource, ...] = field(default=())

    def get(self, name: str) -> PromptTemplate | BrokenResource | None:
        """The entry that answers ``name``.

        Args:
            name: The command word, without the leading ``/``.

        Returns:
            The template or broken entry holding ``name``, or ``None``.
        """
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None


EMPTY_TEMPLATES = PromptTemplateCatalog()


def _read_template(path: Path, scope: ResourceScope) -> PromptTemplate | BrokenResource:
    """One ``.md`` file as a template, or as a broken entry holding its name."""
    source = ResourceSource(path=path, scope=scope)
    name = path.stem
    if TEMPLATE_NAME.match(name) is None:
        return BrokenResource(
            name=name,
            source=source,
            error=f"{name!r} is not a command name (letters, digits, '_' and '-' only)",
        )
    try:
        parsed = parse_frontmatter(read_resource_text(path))
        description = parsed.text("description")
        hint = parsed.text("argument-hint")
    except ResourceError as exc:
        return BrokenResource(name=name, source=source, error=str(exc))
    if not description:
        first = next((line.strip() for line in parsed.body.split("\n") if line.strip()), "")
        description = first[:DESCRIPTION_FALLBACK_CHARS] + (
            "..." if len(first) > DESCRIPTION_FALLBACK_CHARS else ""
        )
    return PromptTemplate(
        name=name,
        description=description,
        argument_hint=hint or None,
        body=parsed.body.strip("\n"),
        source=source,
    )


def _template_files(root: Path, scope: ResourceScope) -> list[Path]:
    """The template files under one location: the file itself, or a directory's ``*.md``.

    Non-recursive. A missing user or project directory is not an error, since
    discovery only says where to look; a missing EXPLICIT path is.

    Raises:
        ResourceError: An explicit path does not exist or is not a ``.md`` file
            or a directory.
    """
    if root.is_dir():
        return sorted(p for p in root.iterdir() if p.suffix == ".md" and p.is_file())
    if scope != "explicit":
        return []
    if root.is_file() and root.suffix == ".md":
        return [root]
    raise ResourceError(f"prompt template path {root} is not a .md file or a directory")


@agent_facing(topic="sdk")
def load_prompt_templates(
    locations: ResourceLocations | None = None,
    *,
    cwd: str | Path | None = None,
    paths: Iterable[str | Path] = (),
    discover: bool = True,
) -> PromptTemplateCatalog:
    """Discover and read every prompt template, first name wins.

    Order: explicit paths, then ``~/.tau/prompts/``, then ``<cwd>/.tau/prompts/``
    when :data:`~tau_agent_core.resources.PROJECT_SCOPE_ADMITTED` admits it.
    Within a directory, files load in name order. A file that cannot be read
    becomes a :class:`~tau_agent_core.resources.BrokenResource` and keeps its
    name, so one bad file costs that template and nothing else.

    Args:
        locations: Where to look. ``None`` builds them from the other arguments.
        cwd: The working directory, when ``locations`` is ``None``.
        paths: Explicit files or directories, when ``locations`` is ``None``.
        discover: Whether to read the user and project directories.

    Raises:
        ResourceError: An explicit path does not exist. The operator named it,
            so it is not skipped.

    Returns:
        The catalog, one entry per name.
    """
    if locations is None:
        locations = resource_locations("prompts", cwd=cwd, paths=paths, discover=discover)
    entries: list[PromptTemplate | BrokenResource] = []
    shadowed: list[PromptTemplate | BrokenResource] = []
    seen_names: set[str] = set()
    seen_files: set[Path] = set()
    for root, scope in locations.roots():
        for path in _template_files(root, scope):
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            entry = _read_template(resolved, scope)
            if entry.name in seen_names:
                shadowed.append(entry)
            else:
                seen_names.add(entry.name)
                entries.append(entry)
    return PromptTemplateCatalog(entries=tuple(entries), shadowed=tuple(shadowed))


def _uses_positional(body: str) -> bool:
    """Whether ``body`` reads any argument by position."""
    for match in _PLACEHOLDER.finditer(body):
        default_target, _default, slice_start, _length, simple = match.groups()
        if slice_start is not None:
            return True
        if (default_target or simple or "").isdigit():
            return True
    return False


@agent_facing(topic="sdk")
def expand_prompt_template(template: PromptTemplate, args: str) -> str:
    """``template``'s body with ``args`` substituted.

    Placeholders: ``$ARGUMENTS`` and ``$@`` are ``args`` verbatim; ``$1``…``$N``
    are shell-style words of it; ``${N:-default}`` and ``${@:-default}`` supply a
    default for an empty value; ``${@:N}`` and ``${@:N:L}`` are a run of words.
    Substitution reads the template only, so a ``$1`` inside ``args`` stays as typed.

    When the body has no placeholder at all and ``args`` is not empty, the args
    are appended as a final ``ARGUMENTS:`` line rather than dropped — Claude
    Code's rule (docs/PROMPT-TEMPLATES-AND-SKILLS.md §4.2).

    Args:
        template: The template to expand.
        args: Everything typed after the command word.

    Returns:
        The prompt text.

    Raises:
        ResourceError: A bare ``$N`` names a word that was not typed, or a
            positional placeholder meets an unbalanced quote. Sending the
            template with a hole in it would be a prompt nobody wrote.
    """
    words: list[str] = []
    if _uses_positional(template.body):
        try:
            words = shlex.split(args)
        except ValueError as exc:
            raise ResourceError(
                f"/{template.name} reads arguments by position and {args!r} cannot be "
                f"split into words: {exc}"
            ) from exc

    def substitute(match: re.Match[str]) -> str:
        default_target, default_value, slice_start, slice_length, simple = match.groups()
        if default_target is not None:
            if default_target in ("ARGUMENTS", "@"):
                return args or default_value
            index = int(default_target) - 1
            value = words[index] if 0 <= index < len(words) else ""
            return value or default_value
        if slice_start is not None:
            start = max(int(slice_start) - 1, 0)
            stop = start + int(slice_length) if slice_length is not None else None
            return " ".join(words[start:stop])
        if simple in ("ARGUMENTS", "@"):
            return args
        index = int(simple) - 1
        if not 0 <= index < len(words):
            raise ResourceError(
                f"/{template.name} uses ${simple} but was given {len(words)} argument(s). "
                f"Write ${{{simple}:-default}} in the template to make it optional."
            )
        return words[index]

    if _PLACEHOLDER.search(template.body) is None:
        return f"{template.body}\n\nARGUMENTS: {args}" if args else template.body
    return _PLACEHOLDER.sub(substitute, template.body)
