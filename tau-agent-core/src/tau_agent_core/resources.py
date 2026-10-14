"""Markdown resources τ discovers on disk: where they live, and how one is read.

Reference: docs/PROMPT-TEMPLATES-AND-SKILLS.md §2.

Prompt templates (:mod:`tau_agent_core.prompt_templates`) and skills
(:mod:`tau_agent_core.skills`) are both Markdown with an optional frontmatter
block, discovered in the same three scopes, resolved by the same first-wins rule,
and broken the same way. This module is the part they share; neither knows the
other exists.

The project scope is the seam the trust gate plugs into (§3):
:data:`PROJECT_SCOPE_ADMITTED` is the one table that decides whether a kind's
``<cwd>/.tau/<kind>/`` directory is read at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from tau_llm.docs import agent_facing

from tau_agent_core.tau_home import resolve_tau_home

ResourceScope = Literal["explicit", "user", "project"]
"""Where a resource was found, strongest first.

``explicit`` is a path the operator named (a CLI flag or a config list), ``user``
is ``<τ home>/<kind>/`` (:func:`~tau_agent_core.tau_home.resolve_tau_home`), and ``project`` is ``<cwd>/.tau/<kind>/``. Two resources
with one name resolve to the one found first in this order (§2.2).
"""

PROJECT_DIR_NAME = ".tau"
"""The per-project directory under the working directory."""

PROJECT_SCOPE_ADMITTED: Mapping[str, bool] = MappingProxyType({"prompts": True, "skills": False})
"""Whether ``<cwd>/.tau/<kind>/`` is read for each kind, until the trust gate decides.

The trust gate (ROADMAP Tier 8) replaces this table with a per-project decision.
Until then: project prompt templates load, because a template enters the context
only when the user types its name, and τ already loads a project's ``CLAUDE.md``
and ``.tau/SYSTEM.md`` with no gate at all; project skills do not, because the
ROADMAP orders the skills leg behind the gate (docs/PROMPT-TEMPLATES-AND-SKILLS.md §3).
"""


@agent_facing(topic="sdk")
class ResourceError(ValueError):
    """A resource could not be read, or an invocation of it could not be expanded.

    Raised from the submission that invoked it, so every head reports it through
    the path it already uses for a failed submission.
    """


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class ResourceSource:
    """The file a resource was read from, and the scope it was found in.

    Attributes:
        path: The file, resolved to an absolute path.
        scope: Which discovery scope found it.
    """

    path: Path
    scope: ResourceScope


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class BrokenResource:
    """A resource whose file was found and could not be read.

    It keeps its name, so it still occupies that name: invoking it raises
    :class:`ResourceError` carrying ``error``, and a completion list shows it as
    broken rather than leaving the reader to wonder why the name is missing.

    Attributes:
        name: The name the resource would have had.
        source: Where it was found.
        error: What is wrong with it, for the reader.
    """

    name: str
    source: ResourceSource
    error: str


@dataclass(frozen=True)
class Frontmatter:
    """A Markdown file split into its frontmatter fields and its body.

    Attributes:
        fields: Top-level scalar keys. A key whose value is a nested mapping or
            list is absent here and named in ``nested``.
        nested: Keys whose value is a block τ does not read as a scalar.
        body: Everything after the closing ``---``, or the whole text when there
            is no frontmatter.
    """

    fields: dict[str, str]
    nested: frozenset[str]
    body: str

    def text(self, key: str) -> str | None:
        """The scalar value of ``key``, or ``None`` when absent.

        Raises:
            ResourceError: ``key`` holds a nested block, so reading it as text
                would return something the author did not write.
        """
        if key in self.nested:
            raise ResourceError(f"frontmatter key {key!r} holds a nested block, not text")
        return self.fields.get(key)


_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?$")
_BLOCK_INDICATOR = re.compile(r"^([|>])([+-]?)$")


def _unquote(value: str, line_number: int) -> str:
    """A scalar's value with YAML single or double quoting removed."""
    if value[:1] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise ResourceError(f"frontmatter line {line_number}: unterminated single quote")
        return value[1:-1].replace("''", "'")
    if value[:1] == '"':
        if len(value) < 2 or value[-1] != '"':
            raise ResourceError(f"frontmatter line {line_number}: unterminated double quote")
        inner = value[1:-1]
        return (
            inner.replace("\\\\", "\0").replace('\\"', '"').replace("\\n", "\n").replace("\0", "\\")
        )
    return value


def _block_scalar(lines: list[str], style: str, chomp: str) -> str:
    """Join the indented lines of a ``|`` or ``>`` block scalar."""
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    stripped = [line[cut:] if line.strip() else "" for line in lines]
    if style == "|":
        text = "\n".join(stripped)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for line in stripped:
            if line:
                current.append(line)
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    text = text.rstrip("\n")
    if chomp == "-":
        return text
    return text + "\n" if chomp == "+" or style == "|" else text


@agent_facing(topic="sdk")
def parse_frontmatter(text: str) -> Frontmatter:
    """Split ``text`` into frontmatter and body, reading the YAML subset skills use.

    Read: ``key: value`` (plain, ``'single'`` or ``"double"`` quoted), a plain
    value continued on more-indented lines, ``|`` and ``>`` block scalars with
    ``-``/``+`` chomping, and ``#`` comment lines. A key with nothing after the
    colon and an indented block under it (a mapping or a list, such as the Agent
    Skills ``metadata:`` key) is recorded in ``nested`` and not parsed. Anything
    else raises, because a field read wrongly is worse than a file refused.
    PyYAML is not a dependency of this package (docs/PROMPT-TEMPLATES-AND-SKILLS.md §2.3).

    Args:
        text: The whole file.

    Returns:
        The fields and the body.

    Raises:
        ResourceError: The block is opened and never closed, or a line is not one
            of the shapes above.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return Frontmatter(fields={}, nested=frozenset(), body=text)
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "..."))
    except StopIteration:
        raise ResourceError("frontmatter opened with '---' and never closed") from None

    fields: dict[str, str] = {}
    nested: set[str] = set()
    block = lines[1:close]
    i = 0
    while i < len(block):
        line = block[i].rstrip()
        number = i + 2
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        match = _KEY_LINE.match(line)
        if match is None:
            raise ResourceError(f"frontmatter line {number}: expected 'key: value', got {line!r}")
        key, raw = match.group(1), (match.group(2) or "").strip()
        i += 1
        continuation: list[str] = []
        while i < len(block) and (not block[i].strip() or block[i][:1] in (" ", "\t")):
            continuation.append(block[i].rstrip())
            i += 1
        while continuation and not continuation[-1].strip():
            continuation.pop()
        indicator = _BLOCK_INDICATOR.match(raw)
        if indicator is not None:
            fields[key] = _block_scalar(continuation, indicator.group(1), indicator.group(2))
        elif not raw:
            if continuation:
                nested.add(key)
            else:
                fields[key] = ""
        elif raw[:1] in ("'", '"'):
            if continuation:
                raise ResourceError(f"frontmatter line {number}: a quoted value must fit one line")
            fields[key] = _unquote(raw, number)
        else:
            if raw.startswith(("[", "{", "&", "*", "!")):
                raise ResourceError(
                    f"frontmatter line {number}: {key!r} uses YAML syntax τ does not read"
                )
            fields[key] = " ".join([raw, *(part.strip() for part in continuation if part.strip())])
    body = "\n".join(lines[close + 1 :])
    return Frontmatter(fields=fields, nested=frozenset(nested), body=body)


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class ResourceLocations:
    """Where one kind of resource is looked for, in precedence order.

    Attributes:
        explicit: Paths the operator named, in the order named.
        user: ``<τ home>/<kind>/``, or ``None`` when discovery is off.
        project: ``<cwd>/.tau/<kind>/``, or ``None`` when discovery is off or the
            project scope is not admitted (:data:`PROJECT_SCOPE_ADMITTED`).
    """

    explicit: tuple[Path, ...]
    user: Path | None
    project: Path | None

    def roots(self) -> list[tuple[Path, ResourceScope]]:
        """Every location with its scope.

        Returns:
            ``(path, scope)`` pairs, strongest scope first.
        """
        out: list[tuple[Path, ResourceScope]] = [(path, "explicit") for path in self.explicit]
        if self.user is not None:
            out.append((self.user, "user"))
        if self.project is not None:
            out.append((self.project, "project"))
        return out


@agent_facing(topic="sdk")
def resource_locations(
    kind: str,
    *,
    cwd: str | Path | None = None,
    agent_dir: str | Path | None = None,
    paths: Iterable[str | Path] = (),
    discover: bool = True,
    project: bool | None = None,
) -> ResourceLocations:
    """The locations for ``kind`` (``"prompts"`` or ``"skills"``).

    Args:
        kind: The subdirectory name under τ home and ``<cwd>/.tau``.
        cwd: The working directory. ``None`` is the process cwd.
        agent_dir: Override for τ home.
        paths: Explicit paths, relative ones resolved against ``cwd``. They load
            even when ``discover`` is False, the rule ``-e`` follows for extensions.
        discover: Whether to read the user and project directories at all.
        project: Whether the project directory is admitted. ``None`` reads
            :data:`PROJECT_SCOPE_ADMITTED`, which is the trust gate's seam.

    Returns:
        The locations, with ``user`` and ``project`` left ``None`` where not read.

    Raises:
        KeyError: ``project`` is ``None`` and ``kind`` is not in
            :data:`PROJECT_SCOPE_ADMITTED`.
    """
    base = Path(cwd).expanduser() if cwd is not None else Path.cwd()
    explicit = tuple((base / Path(p).expanduser()).resolve() for p in paths)
    if not discover:
        return ResourceLocations(explicit=explicit, user=None, project=None)
    admitted = PROJECT_SCOPE_ADMITTED[kind] if project is None else project
    user = ((Path(agent_dir).expanduser() if agent_dir else resolve_tau_home()) / kind).resolve()
    project_dir = (base / PROJECT_DIR_NAME / kind).resolve() if admitted else None
    return ResourceLocations(explicit=explicit, user=user, project=project_dir)


def read_resource_text(path: Path) -> str:
    """A resource file's text.

    Raises:
        ResourceError: The file cannot be read or is not UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ResourceError(f"cannot read {path}: {exc}") from exc
