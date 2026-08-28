"""Build the generated reference section of τ's agent-facing documentation library.

Reference: docs/AGENT-DOCS.md

This module reads the source trees **statically**, with griffe, and turns every
object marked ``@agent_facing`` (:mod:`tau_llm.docs`) into a markdown reference
page plus a coverage report. It imports none of the code it documents.

That is the whole reason griffe is here rather than ``inspect``. τ's Anthropic
and Google providers import their SDKs lazily so the suite runs without them,
and ``tau_coding_agent`` pulls in Textual on import. A runtime decorator
registry would need every one of those modules imported to find its markers.
griffe reads the decorator out of the AST, so a tree that cannot be imported in
this environment still documents correctly.

Like ``tau_agent_core.rpc.protocol_doc``, everything here is pure: it takes
paths, returns strings and dataclasses, and writes nothing. The two thin CLIs
that write to disk are ``scripts/build_agent_docs.py`` and
``scripts/check_docs_coverage.py``.

griffe is a ``[dev]`` dependency, not a runtime one, and this module is
deliberately absent from ``tau_agent_core.__init__``. Importing it in an install
without the ``[dev]`` extra raises ``ModuleNotFoundError`` at the griffe import
below, which is correct: nothing on the request path reaches this file.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import griffe

from tau_llm.docs import DECORATOR_PATH

__all__ = [
    "AGENT_MARKER",
    "GENERATED_BANNER",
    "PACKAGES",
    "SRC_TREES",
    "TOPICS",
    "source_paths",
    "CoverageReport",
    "DocsBuildError",
    "ObjectDoc",
    "ParamDoc",
    "TopicPage",
    "UnknownTopicError",
    "collect",
    "coverage",
    "pages",
    "render_index",
    "render_topic",
]


class DocsBuildError(Exception):
    """A fault in the documentation source that the build refuses to paper over."""


class UnknownTopicError(DocsBuildError):
    """An object claims a topic that has no library page.

    A new topic is a decision about the shape of the library, so it is made by
    adding an entry to :data:`TOPICS` and writing the prose page. It is not made
    by typing a new string into a decorator and having a page appear.
    """


# The library's reference sections, in reading order. A topic is a page stem;
# the value is the page title. This mapping is the only place a topic is
# declared -- `@agent_facing(topic=...)` selects from it and cannot extend it.
TOPICS: dict[str, str] = {
    "messages": "Messages and content",
    "providers": "Providers and the client",
    "constraints": "Constrained decoding",
    "tools": "Tools",
    "agent-loop": "The agent loop",
    "sessions": "Sessions and the conversation tree",
    "events": "Events",
    "sdk": "The SDK entry point",
    "extensions": "Extensions",
    "compaction": "Compaction",
    "rpc": "The RPC protocol",
    "export": "Export",
    "runs": "Run manifests and latency",
}

# Parameters that carry no information for a reader of the reference. They are
# not counted against coverage and are not published.
_IMPLICIT_PARAMETERS = frozenset({"self", "cls"})

# The packages the reference is generated from. Headless only, for now: every
# topic in TOPICS is a headless one, and `tau_coding_agent` is a Textual app
# whose surface an extension author reaches through `ExtensionUI` rather than
# directly. Adding it means adding TUI topics first.
PACKAGES = ("tau_llm", "tau_agent_core")

# Every src tree, including the ones not being documented. griffe resolves
# annotations across packages, so a tree left out here turns a resolvable type
# into an unresolved alias.
SRC_TREES = (
    "tau-llm/src",
    "tau-agent-core/src",
    "tau-coding-agent/src",
    "tau-jmfts/src",
)


def source_paths(repo_root: Path) -> list[Path]:
    """Absolute :data:`SRC_TREES` for a checkout.

    Args:
        repo_root: The repository root.

    Returns:
        One path per src tree, in :data:`SRC_TREES` order.
    """
    return [repo_root / tree for tree in SRC_TREES]


@dataclass(frozen=True)
class ParamDoc:
    """One parameter of a documented callable.

    Attributes:
        name: The parameter name as written in the signature.
        annotation: The annotation as source text, or ``None`` if unannotated.
        default: The default as source text, or ``None`` if the parameter is
            required.
        kind: griffe's parameter kind -- ``"positional or keyword"``,
            ``"keyword-only"``, ``"variadic positional"`` and so on.
        description: The prose from the docstring's ``Args:`` section, or
            ``None`` when the docstring does not mention this parameter. A
            ``None`` here is what :func:`coverage` counts.
    """

    name: str
    annotation: str | None
    default: str | None
    kind: str
    description: str | None


@dataclass(frozen=True)
class ObjectDoc:
    """One object marked ``@agent_facing``, and everything the build knows about it.

    Attributes:
        path: The canonical dotted path, e.g. ``tau_agent_core.sdk.create_agent_session``.
        name: The bare name.
        kind: ``"function"``, ``"class"``, ``"attribute"``.
        topic: The :data:`TOPICS` key this object was marked with.
        since: The version from the marker, or ``None``.
        marked_directly: ``True`` if the object carries its own decorator,
            ``False`` if it was pulled in as a public member of a marked class.
        parent: The dotted path of the marked class that pulled this member in,
            or ``None`` for a directly marked object.
        summary: The docstring's first line, or ``None``.
        body: The rest of the docstring's prose, with the structured sections
            removed, or ``None``.
        params: One entry per published parameter, in signature order.
        returns: The prose of the ``Returns:`` section, or ``None``.
        raises: ``(exception, prose)`` pairs from the ``Raises:`` section.
        examples: ``(label, prose)`` for each ``Examples:`` section and each
            google-style admonition (``Note:``, ``Warning:``, ``See Also:``).
        annotation: For an attribute, its annotation as source text.
        filepath: The file the object is defined in.
        lineno: The line the definition starts on.
        members: Public members of a marked class, already resolved into their
            own :class:`ObjectDoc` records. Empty for anything but a class.
    """

    path: str
    name: str
    kind: str
    topic: str
    since: str | None
    marked_directly: bool
    parent: str | None
    summary: str | None
    body: str | None
    params: tuple[ParamDoc, ...]
    returns: str | None
    raises: tuple[tuple[str, str], ...]
    examples: tuple[tuple[str, str], ...]
    annotation: str | None
    filepath: str
    lineno: int
    members: tuple["ObjectDoc", ...] = ()

    @property
    def has_docstring(self) -> bool:
        """Whether the object carries any docstring at all."""
        return self.summary is not None

    @property
    def undocumented_params(self) -> tuple[str, ...]:
        """Published parameters the docstring never describes."""
        return tuple(p.name for p in self.params if p.description is None)

    def walk(self) -> Iterator["ObjectDoc"]:
        """Yield this object and, depth-first, every member it pulled in."""
        yield self
        for member in self.members:
            yield from member.walk()


@dataclass(frozen=True)
class TopicPage:
    """The documented objects belonging to one topic.

    Attributes:
        topic: The :data:`TOPICS` key.
        title: The human title from :data:`TOPICS`.
        objects: Top-level marked objects, sorted by name.
    """

    topic: str
    title: str
    objects: tuple[ObjectDoc, ...]


@dataclass
class CoverageReport:
    """What fraction of the marked surface is actually described.

    The denominator is the marked set, not every object in the tree. That is the
    metric ``interrogate`` and ``docstr-coverage`` cannot express: they measure
    all objects, which for τ would report on hundreds of internals nobody asked
    the reference to cover.

    Attributes:
        objects: Every documented object, flattened, including class members.
        missing_docstring: Objects with no docstring at all.
        missing_params: ``(object, parameter names)`` for objects whose docstring
            omits at least one parameter.
        missing_returns: Callables that return something other than ``None`` and
            have no ``Returns:`` section.
        drift: griffe's own warnings, one string each. A docstring naming a
            parameter the signature does not have lands here.
    """

    objects: list[ObjectDoc] = field(default_factory=list)
    missing_docstring: list[ObjectDoc] = field(default_factory=list)
    missing_params: list[tuple[ObjectDoc, tuple[str, ...]]] = field(default_factory=list)
    missing_returns: list[ObjectDoc] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """How many marked objects the build found."""
        return len(self.objects)

    @property
    def complete(self) -> int:
        """How many marked objects have nothing missing."""
        incomplete = {id(o) for o in self.missing_docstring}
        incomplete |= {id(o) for o, _ in self.missing_params}
        incomplete |= {id(o) for o in self.missing_returns}
        return self.total - len(incomplete)

    @property
    def fraction(self) -> float:
        """:attr:`complete` over :attr:`total`, or ``1.0`` for an empty set."""
        return 1.0 if not self.total else self.complete / self.total

    @property
    def clean(self) -> bool:
        """Whether every marked object is fully documented and nothing drifted."""
        return self.complete == self.total and not self.drift


class _WarningCollector(logging.Handler):
    """Catch griffe's docstring warnings instead of letting them print and vanish.

    griffe already detects the failure mode a separate linter would be installed
    for -- a docstring that names a parameter the signature does not have. It
    reports it as a log record. Collecting those records is how that check gets
    into the gate.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _decorator_marker(obj: griffe.Object) -> tuple[str, str | None] | None:
    """Return ``(topic, since)`` if ``obj`` carries ``@agent_facing``, else ``None``.

    Args:
        obj: A griffe function or class.

    Returns:
        The marker arguments, or ``None`` when the object is unmarked.

    Raises:
        DocsBuildError: If the decorator is present but its ``topic`` argument
            is missing or is not a literal string. The build cannot evaluate an
            expression it never imports, and guessing a topic is worse than
            stopping.
    """
    for decorator in getattr(obj, "decorators", ()):
        if decorator.callable_path != DECORATOR_PATH:
            continue
        arguments: dict[str, str] = {}
        for argument in getattr(decorator.value, "arguments", ()):
            name = getattr(argument, "name", None)
            if name is None:
                raise DocsBuildError(
                    f"{obj.path}: @agent_facing takes keyword arguments only; "
                    f"got a positional at {obj.filepath}:{obj.lineno}"
                )
            try:
                arguments[name] = ast.literal_eval(str(argument.value))
            except (ValueError, SyntaxError) as exc:
                raise DocsBuildError(
                    f"{obj.path}: @agent_facing({name}=...) must be a literal, "
                    f"not an expression the build would have to import to evaluate "
                    f"({obj.filepath}:{obj.lineno})"
                ) from exc
        if "topic" not in arguments:
            raise DocsBuildError(
                f"{obj.path}: @agent_facing requires topic= ({obj.filepath}:{obj.lineno})"
            )
        topic = arguments["topic"]
        if topic not in TOPICS:
            raise UnknownTopicError(
                f"{obj.path}: unknown topic {topic!r} ({obj.filepath}:{obj.lineno}). "
                f"Known topics: {', '.join(sorted(TOPICS))}. "
                f"A new topic needs an entry in docs_build.TOPICS and a library page."
            )
        return topic, arguments.get("since")
    return None


def _docstring_sections(obj: griffe.Object) -> dict[str, object]:
    """Parse ``obj``'s docstring into the pieces the reference renders.

    Args:
        obj: Any griffe object.

    Returns:
        A mapping with keys ``summary``, ``body``, ``params``, ``returns``,
        ``raises`` and ``examples``. Every value is ``None`` or empty when the
        object has no docstring.
    """
    empty: dict[str, object] = {
        "summary": None,
        "body": None,
        "params": {},
        "returns": None,
        "raises": (),
        "examples": (),
    }
    if obj.docstring is None:
        return empty

    text_blocks: list[str] = []
    params: dict[str, str] = {}
    returns: str | None = None
    raises: list[tuple[str, str]] = []
    examples: list[tuple[str, str]] = []

    for section in obj.docstring.parsed:
        kind = section.kind.value
        if kind == "text":
            text_blocks.append(section.value)
        elif kind in ("parameters", "other parameters", "keyword arguments"):
            for item in section.value:
                params[item.name] = item.description
        elif kind in ("attributes",):
            for item in section.value:
                params.setdefault(item.name, item.description)
        elif kind == "returns":
            returns = "\n\n".join(item.description for item in section.value) or None
        elif kind == "raises":
            raises.extend((str(item.annotation), item.description) for item in section.value)
        elif kind == "examples":
            # A google-style `Examples:` section parses to (kind, text) pairs,
            # where kind separates prose from a code block. The reference keeps
            # both, in order, under one "Example" heading.
            examples.append(("Example", "\n\n".join(text for _, text in section.value)))
        elif kind == "admonition":
            # Anything else google style recognises by a `Word:` prefix --
            # `Note:`, `Warning:`, `See Also:`. griffe gives the label as the
            # section title and the prose as `.contents`.
            examples.append((str(section.title or "Note"), str(section.value.contents)))

    prose = "\n\n".join(text_blocks).strip()
    summary, _, body = prose.partition("\n\n")
    return {
        "summary": summary.strip() or None,
        "body": body.strip() or None,
        "params": params,
        "returns": returns,
        "raises": tuple(raises),
        "examples": tuple(examples),
    }


def _parameters(obj: griffe.Object, described: dict[str, str]) -> tuple[ParamDoc, ...]:
    """Build the published parameter list for a callable.

    Every parameter in the signature is published, described or not. The name,
    annotation and default come from the signature, so an undocumented argument
    is still visible to a reader -- it just has no prose. That is the behaviour
    the marker was asked for: an object with no docstring at all still conveys
    its parameters.

    Args:
        obj: A griffe function, or a class whose ``__init__`` is read.
        described: Parameter name to prose, from the docstring.

    Returns:
        One :class:`ParamDoc` per signature parameter, minus ``self``/``cls``.
    """
    parameters = getattr(obj, "parameters", None)
    if parameters is None:
        return ()
    out: list[ParamDoc] = []
    for parameter in parameters:
        if parameter.name in _IMPLICIT_PARAMETERS:
            continue
        out.append(
            ParamDoc(
                name=parameter.name,
                annotation=None if parameter.annotation is None else str(parameter.annotation),
                default=None if parameter.default is None else str(parameter.default),
                kind="" if parameter.kind is None else parameter.kind.value,
                description=described.get(parameter.name),
            )
        )
    return tuple(out)


# The dunders a marked class publishes. An allowlist, not "every dunder": these
# are the ones that define how a caller USES the object -- what `with`, `for`,
# `await` and `()` do to it. `__post_init__`, `__repr__`, `__eq__` and friends
# change nothing a reader needs, and documenting them is noise that also drags
# the coverage number down for no gain.
_PUBLISHED_DUNDERS = frozenset(
    {
        "__call__",
        "__enter__",
        "__exit__",
        "__aenter__",
        "__aexit__",
        "__iter__",
        "__aiter__",
        "__next__",
        "__anext__",
        "__len__",
        "__contains__",
        "__getitem__",
        "__setitem__",
    }
)


def _is_public(name: str) -> bool:
    """Whether a member name is part of the surface a marked class publishes.

    Args:
        name: The member's bare name.

    Returns:
        ``True`` for a name that does not start with an underscore, and for the
        dunders in :data:`_PUBLISHED_DUNDERS`.
    """
    if name.startswith("_"):
        return name in _PUBLISHED_DUNDERS
    return True


def _build(
    obj: griffe.Object,
    topic: str,
    since: str | None,
    *,
    marked_directly: bool,
    parent: str | None,
    inherited_summary: str | None = None,
) -> ObjectDoc:
    """Turn one griffe object into an :class:`ObjectDoc`, recursing into a class.

    Args:
        obj: The griffe function, class or attribute to convert.
        topic: The topic this object is filed under.
        since: The version from the marker, if any.
        marked_directly: Whether ``obj`` carries its own ``@agent_facing``.
        parent: The dotted path of the marked class that pulled ``obj`` in.
        inherited_summary: Prose for ``obj`` taken from its parent class's
            ``Attributes:`` section. A dataclass field is documented there, not
            by a docstring of its own, and counting it as undocumented would
            make the coverage gate report a fault that is not one.

    Returns:
        The finished record, with class members already resolved.
    """
    sections = _docstring_sections(obj)
    described = sections["params"]
    assert isinstance(described, dict)

    signature_source: griffe.Object = obj
    if obj.kind is griffe.Kind.CLASS and "__init__" in obj.members:
        init = obj.members["__init__"]
        if isinstance(init, griffe.Object):
            signature_source = init
            init_described = _docstring_sections(init)["params"]
            assert isinstance(init_described, dict)
            described = {**init_described, **described}

    members: list[ObjectDoc] = []
    if obj.kind is griffe.Kind.CLASS:
        for member_name, member in obj.members.items():
            if not isinstance(member, griffe.Object) or not _is_public(member_name):
                continue
            if member_name == "__init__":
                continue
            member_marker = _decorator_marker(member)
            member_topic, member_since = member_marker or (topic, since)
            members.append(
                _build(
                    member,
                    member_topic,
                    member_since,
                    marked_directly=member_marker is not None,
                    parent=obj.path,
                    inherited_summary=described.get(member_name),
                )
            )

    # `annotation` is an attribute's type on a griffe Attribute and the RETURN
    # annotation on a griffe Function -- the two are the same field name and
    # this record keeps them in one place, so a reader of the reference sees a
    # type either way.
    _annotation = getattr(obj, "annotation", None)

    return ObjectDoc(
        path=obj.path,
        name=obj.name,
        kind=obj.kind.value,
        topic=topic,
        since=since,
        marked_directly=marked_directly,
        parent=parent,
        summary=sections["summary"] or inherited_summary,  # type: ignore[arg-type]
        body=sections["body"],  # type: ignore[arg-type]
        params=_parameters(signature_source, described),
        returns=sections["returns"],  # type: ignore[arg-type]
        raises=sections["raises"],  # type: ignore[arg-type]
        examples=sections["examples"],  # type: ignore[arg-type]
        annotation=(None if _annotation is None else str(_annotation)),
        filepath=str(obj.filepath),
        lineno=obj.lineno or 0,
        members=tuple(sorted(members, key=lambda m: m.name)),
    )


def collect(
    packages: Sequence[str],
    search_paths: Sequence[Path | str],
) -> tuple[list[ObjectDoc], list[str]]:
    """Find every ``@agent_facing`` object in ``packages``, without importing them.

    Args:
        packages: Top-level package names to load, e.g. ``["tau_llm",
            "tau_agent_core"]``. Submodules are loaded recursively.
        search_paths: The ``src/`` trees to resolve those packages against. Pass
            every tree, not only the ones being documented, so that cross-package
            annotations resolve.

    Returns:
        ``(objects, warnings)`` -- the marked objects sorted by dotted path, and
        griffe's own docstring warnings as plain strings.

    Raises:
        DocsBuildError: On a malformed or unknown marker. See
            :func:`_decorator_marker`.
    """
    collector = _WarningCollector()
    logger = logging.getLogger("griffe")
    logger.addHandler(collector)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        found: list[ObjectDoc] = []
        for package in packages:
            module = griffe.load(
                package,
                search_paths=[str(p) for p in search_paths],
                docstring_parser="google",
                allow_inspection=False,
                store_source=False,
            )
            _scan(module, found)
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)

    found.sort(key=lambda o: o.path)
    return found, collector.messages


def _scan(container: griffe.Object | griffe.Alias, found: list[ObjectDoc]) -> None:
    """Append every marked object under ``container`` to ``found``.

    The walk descends into submodules always, and into an **unmarked** class as
    well. Descending into an unmarked class is what stops a marked method from
    being silently dropped because nobody marked the class around it. A marked
    class is not descended into here, because :func:`_build` already pulls in
    its public members and doing both would publish them twice.

    Aliases are skipped, so a name re-exported through an ``__init__`` is
    visited once, at the module that defines it.

    Args:
        container: A griffe module or class.
        found: The accumulator, appended to in place.
    """
    for member in container.members.values():
        if not isinstance(member, griffe.Object):
            continue
        if member.kind is griffe.Kind.MODULE:
            _scan(member, found)
            continue
        if member.kind not in (griffe.Kind.FUNCTION, griffe.Kind.CLASS):
            continue
        marker = _decorator_marker(member)
        if marker is not None:
            topic, since = marker
            found.append(_build(member, topic, since, marked_directly=True, parent=None))
        elif member.kind is griffe.Kind.CLASS:
            _scan(member, found)


def coverage(objects: Iterable[ObjectDoc], warnings: Sequence[str] = ()) -> CoverageReport:
    """Measure how much of the marked surface carries real prose.

    Args:
        objects: The result of :func:`collect`.
        warnings: griffe's warnings, also from :func:`collect`. They become
            :attr:`CoverageReport.drift`.

    Returns:
        A :class:`CoverageReport` over every marked object and every public
        member a marked class pulled in.
    """
    report = CoverageReport(drift=list(warnings))
    for top in objects:
        for obj in top.walk():
            report.objects.append(obj)
            if not obj.has_docstring:
                report.missing_docstring.append(obj)
            missing = obj.undocumented_params
            if missing:
                report.missing_params.append((obj, missing))
            if obj.kind == "function" and obj.returns is None and _returns_something(obj):
                report.missing_returns.append(obj)
    return report


GENERATED_BANNER = "<!-- generated by scripts/build_agent_docs.py — do not edit by hand -->"

# The per-heading audience marker the whole library uses. The prose pages carry
# it by hand; the reference emits it. One filter serves both renders: the site
# build strips these lines, and the shipped agent copy keeps only the sections
# marked `yes`.
AGENT_MARKER = "<!-- agent: yes -->"


# griffe hands a `**kwargs` parameter the default `{}` and a `*args` parameter
# the default `()`. Those are descriptions of the empty call, not defaults a
# caller can write, and printing `**fields: Any = {}` invites someone to try.
_VARIADIC_PREFIX = {"variadic positional": "*", "variadic keyword": "**"}


def _param_text(param: ParamDoc) -> str:
    """Render one parameter as it appears in a signature or a bullet.

    Args:
        param: The parameter to render.

    Returns:
        ``name``, with any ``*``/``**`` prefix, its annotation, and its default
        -- the default omitted for a variadic, which does not have one.
    """
    text = _VARIADIC_PREFIX.get(param.kind, "") + param.name
    if param.annotation:
        text += f": {param.annotation}"
    if param.default is not None and param.kind not in _VARIADIC_PREFIX:
        text += f" = {param.default}"
    return text


def _signature(obj: ObjectDoc) -> str:
    """Render a copy-pasteable signature line for a callable.

    Args:
        obj: The callable to render.

    Returns:
        One line, with the bare ``*`` separator inserted before the first
        keyword-only parameter when no ``*args`` already supplies it.
    """
    parts: list[str] = []
    seen_star = False
    for param in obj.params:
        if param.kind == "variadic positional":
            seen_star = True
        elif param.kind == "keyword-only" and not seen_star:
            parts.append("*")
            seen_star = True
        parts.append(_param_text(param))
    rendered = ", ".join(parts)
    prefix = "class " if obj.kind == "class" else ""
    suffix = f" -> {obj.annotation}" if obj.kind == "function" and obj.annotation else ""
    return f"{prefix}{obj.name}({rendered}){suffix}"


def _render_object(
    obj: ObjectDoc, level: int, *, ambiguous: frozenset[str] = frozenset()
) -> list[str]:
    """Render one object as markdown lines, at heading depth ``level``.

    Args:
        obj: The object to render.
        level: Markdown heading depth. ``2`` is a page's top-level entry and is
            the only depth that carries the audience marker.
        ambiguous: Bare names that more than one object on this page uses. A
            name in this set is rendered as its full dotted path instead, so
            ``tau_llm.tools.ToolDefinition`` and
            ``tau_agent_core.tools.base.ToolDefinition`` do not become two
            identical headings on the same page.

    Returns:
        The markdown lines, without a trailing blank.
    """
    heading = obj.path if obj.name in ambiguous else obj.name
    lines: list[str] = [f"{'#' * level} {heading}"]
    if level == 2:
        lines.append(AGENT_MARKER)
    lines.append("")

    if obj.kind == "attribute" or (obj.kind == "class" and not obj.params):
        # A pydantic model declares no `__init__`, so there is no constructor
        # signature to show. Printing `class Foo()` would say the opposite of
        # what is true -- the fields below are exactly what it takes.
        annotation = f": {obj.annotation}" if obj.annotation else ""
        lines.append(f"`{obj.path}{annotation}`")
    else:
        lines.append("```python")
        lines.append(_signature(obj))
        lines.append("```")
        lines.append("")
        lines.append(f"`{obj.path}`")
    if obj.since:
        lines[-1] += f" · since {obj.since}"
    lines.append("")

    if obj.summary:
        lines.extend([obj.summary, ""])
    else:
        # Stated, not hidden. The signature above is still published, so the
        # reader is not left guessing at the arguments -- only at the intent.
        lines.extend(["*No description. This object is marked but undocumented.*", ""])
    if obj.body:
        lines.extend([obj.body, ""])

    if obj.params:
        heading = "**Constructor parameters**" if obj.kind == "class" else "**Parameters**"
        lines.extend([heading, ""])
        for param in obj.params:
            description = param.description or "*(no description)*"
            lines.append(f"- `{_param_text(param)}` — {' '.join(description.split())}")
        lines.append("")

    if obj.returns:
        lines.extend(["**Returns**", "", " ".join(obj.returns.split()), ""])

    if obj.raises:
        lines.extend(["**Raises**", ""])
        for exception, why in obj.raises:
            lines.append(f"- `{exception}` — {' '.join(why.split())}")
        lines.append("")

    for label, prose in obj.examples:
        lines.extend([f"**{label}**", "", prose.strip(), ""])

    # A dataclass field is already published above, as a constructor parameter
    # with its prose from the class's `Attributes:` section. Rendering it again
    # as its own subsection says nothing new and costs an agent context window.
    published = {param.name for param in obj.params}
    for member in obj.members:
        if member.kind == "attribute" and member.name in published:
            continue
        lines.extend(_render_object(member, level + 1))

    return lines


def render_topic(page: TopicPage) -> str:
    """Render one topic's reference page as markdown.

    Args:
        page: The topic and its objects, from :func:`pages`.

    Returns:
        The complete markdown file, ending in a newline.
    """
    seen: dict[str, int] = {}
    for obj in page.objects:
        seen[obj.name] = seen.get(obj.name, 0) + 1
    ambiguous = frozenset(name for name, count in seen.items() if count > 1)

    lines = [f"# {page.title} — reference", "", GENERATED_BANNER, ""]
    for obj in page.objects:
        lines.extend(_render_object(obj, 2, ambiguous=ambiguous))
    return "\n".join(lines).rstrip() + "\n"


def pages(objects: Iterable[ObjectDoc]) -> list[TopicPage]:
    """Group marked objects into topic pages, in :data:`TOPICS` order.

    Args:
        objects: The result of :func:`collect`.

    Returns:
        One :class:`TopicPage` per topic that has at least one object. A topic
        with no marked objects yields no page, because an empty reference
        section is worse than none -- it reads as "nothing here to call".
    """
    grouped: dict[str, list[ObjectDoc]] = {topic: [] for topic in TOPICS}
    for obj in objects:
        grouped[obj.topic].append(obj)
    return [
        TopicPage(topic=topic, title=title, objects=tuple(sorted(grouped[topic], key=_sort_key)))
        for topic, title in TOPICS.items()
        if grouped[topic]
    ]


def _sort_key(obj: ObjectDoc) -> tuple[int, str]:
    """Classes first, then functions, each alphabetically."""
    return (0 if obj.kind == "class" else 1, obj.name)


def render_index(built: Sequence[TopicPage]) -> str:
    """Render the reference index that routes an agent to the right page.

    Args:
        built: The pages produced by :func:`pages`.

    Returns:
        A markdown index listing each topic, its file, and the names it defines.
    """
    lines = ["# Reference index", "", GENERATED_BANNER, ""]
    for page in built:
        # Bare names, because the index's job is routing -- "which file mentions
        # AgentSession". A name two objects share is written out in full, since
        # `SessionInfo, SessionInfo, SessionInfo` routes nowhere.
        counts: dict[str, int] = {}
        for obj in page.objects:
            counts[obj.name] = counts.get(obj.name, 0) + 1
        names = ", ".join(
            f"`{obj.path if counts[obj.name] > 1 else obj.name}`" for obj in page.objects
        )
        lines.append(f"- **{page.title}** — `reference/{page.topic}.md` — {names}")
    lines.append("")
    return "\n".join(lines)


def _returns_something(obj: ObjectDoc) -> bool:
    """Whether a callable's annotation promises a value worth describing.

    ``None`` and ``NoneType`` do not; an unannotated function does not either,
    because the build has no evidence and inventing a requirement from missing
    information is how a gate becomes noise.
    """
    return obj.annotation not in (None, "None", "NoneType")
