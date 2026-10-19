"""Where an extension comes from, and the one name it goes by.

Rationale: docs/EXTENSION-PACKAGES.md. §2 makes the identifier the Python
module name for an installed package and the file stem, verbatim, for a file;
§3 makes ``:`` the only delimiter any parser splits on.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePath
from typing import Literal
from urllib.parse import unquote, urlparse

from tau_llm.docs import agent_facing

ENTRY_POINT_GROUP = "tau.extensions"
"""The entry-point group τ reads installed extensions from (§5; final name is a §9 proposal)."""

SourceKind = Literal["package", "module", "dropin", "path"]
"""``package``: an entry point. ``module``: an importable module named with ``-e``.
``dropin``: a file or directory in the extensions directory. ``path``: a file named with ``-e``."""

ExtensionState = Literal[
    "loaded", "disabled", "failed", "authorized", "unauthorized", "unusable", "collision"
]
"""One record's state in ``tau ext list`` (§7). ``authorized`` means it would load;
only a listing made without a session reports it."""

UNUSABLE_CHARACTERS = ":"
"""Characters that make a file stem unusable as an identifier (§2.1); whitespace is the other case."""


@agent_facing(topic="extensions")
class ExplicitExtensionError(ValueError):
    """An ``-e`` value names nothing loadable; a head reports it as a usage error (exit 2)."""


@agent_facing(topic="extensions")
def extension_identifier(label: str) -> str:
    """The identifier of the extension a runner bucket label names.

    This is the one derivation (EXTENSION-PACKAGES.md §2, #40). The config
    key, topic emitter, command owner and ``/extensions`` label all read it.

    A label is one of three things:

    - a file path: ``…/my-ext.py`` is ``my-ext`` and a package directory
      ``…/my.ext`` is ``my.ext``, the stem verbatim;
    - an installed module name, ``tau_router.planner``, returned as is;
    - an inline SDK factory's ``module:qualname``, which becomes
      ``module.qualname``, its fully qualified Python name.

    Args:
        label: The bucket label the loader bound the extension under.

    Returns:
        The identifier.

    Raises:
        ValueError: The label yields an empty identifier.
    """
    if label.endswith(".py"):
        ident = PurePath(label).name[: -len(".py")]
    elif "/" in label:
        ident = PurePath(label).name
    else:
        module, sep, qualname = label.partition(":")
        ident = f"{module}.{qualname}" if sep else label
    if not ident:
        raise ValueError(f"extension label {label!r} yields an empty identifier")
    return ident


@agent_facing(topic="extensions")
def unusable_reason(identifier: str) -> str | None:
    """Why ``identifier`` cannot name an extension, or ``None`` when it can.

    A ``:`` breaks ``--ext-config`` and topic parsing, and whitespace breaks
    commands (EXTENSION-PACKAGES.md §2.1). Such a file is listed, not loaded.

    Args:
        identifier: A candidate identifier, usually a file stem.

    Returns:
        A sentence naming the character and saying to rename the file, or
        ``None``.
    """
    for char in identifier:
        if char in UNUSABLE_CHARACTERS:
            return f"its name contains {char!r}, which τ uses as a delimiter; rename the file"
        if char.isspace():
            return f"its name contains whitespace ({char!r}); rename the file"
    return None


@agent_facing(topic="extensions")
def shorthand_segment(identifier: str) -> str:
    """The last dotted segment of ``identifier``, the shorthand a user may type (§4).

    Args:
        identifier: A full identifier.

    Returns:
        ``planner`` for ``tau_router.planner``; the identifier itself when it
        has no dot.
    """
    return identifier.rsplit(".", 1)[-1]


@agent_facing(topic="extensions")
def resolve_shorthand(token: str, identifiers: list[str]) -> str | None:
    """Resolve a typed extension name against the given identifiers, fresh (§4).

    An exact identifier wins. Otherwise ``token`` matches the identifiers whose
    last dotted segment equals it. Nothing is cached, so a new install cannot
    re-point a shorthand silently.

    Args:
        token: What the user typed.
        identifiers: The candidates, usually the enabled extensions.

    Returns:
        The one matching identifier, or ``None`` when nothing matches.

    Raises:
        ValueError: Two or more identifiers share the segment; the message
            names all of them.
    """
    if token in identifiers:
        return token
    matches = sorted({ident for ident in identifiers if shorthand_segment(ident) == token})
    if len(matches) > 1:
        raise ValueError(
            f"{token!r} is ambiguous: it matches {', '.join(matches)}. Type the full name."
        )
    return matches[0] if matches else None


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ExtensionSource:
    """One extension τ found or was told about, before anything is imported.

    Attributes:
        identifier: Its identifier (§2).
        kind: Where it came from (:data:`SourceKind`).
        target: The module name for ``package``/``module``, the path otherwise.
        distribution: The installing distribution's name; packages only.
        version: That distribution's version; packages only.
        entry_point: The entry point's name; packages only.
        digest: ``sha256:`` over the distribution's RECORD hashes for the
            module's top-level package; ``None`` when RECORD carries none or
            the install is editable (§5).
        editable_path: For an editable install, the source directory
            ``direct_url.json`` names.
        unusable: Why it cannot load at all, or ``None``.
    """

    identifier: str
    kind: SourceKind
    target: str
    distribution: str | None = None
    version: str | None = None
    entry_point: str | None = None
    digest: str | None = None
    editable_path: str | None = None
    unusable: str | None = None

    @property
    def label(self) -> str:
        """The runner bucket label: the module name, or the path."""
        return self.target


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ExtensionRecord:
    """One row of ``tau ext list``: an extension and its state (§7).

    Attributes:
        identifier: Its identifier.
        kind: Where it came from.
        target: Module name or path.
        state: :data:`ExtensionState`.
        reason: Why it is not loaded (``failed``, ``unauthorized``,
            ``unusable``, ``collision``), else ``None``.
        distribution: The installing distribution, for a package.
        version: Its version, for a package.
        entry_point: The entry point's name, for a package.
        authorization: ``explicit`` (named with ``-e``), ``authorized``,
            ``new`` or ``update``; ``None`` for an unusable source.
        previous_version: For an ``update``, the version that was authorized.
        orphan_topics: Topics this extension subscribed to whose emitter is no
            loaded extension (#44).
    """

    identifier: str
    kind: SourceKind
    target: str
    state: ExtensionState
    reason: str | None = None
    distribution: str | None = None
    version: str | None = None
    entry_point: str | None = None
    authorization: str | None = None
    previous_version: str | None = None
    orphan_topics: tuple[str, ...] = field(default_factory=tuple)


@agent_facing(topic="extensions")
def default_extensions_dir() -> Path:
    """The drop-in directory: ``<tau home>/extensions``."""
    from tau_agent_core.tau_home import resolve_tau_home

    return resolve_tau_home() / "extensions"


def _source_for_file(path: Path, kind: SourceKind) -> ExtensionSource:
    """A drop-in or explicit-path source for ``path``, unusable when its stem is."""
    ident = extension_identifier(str(path))
    return ExtensionSource(
        identifier=ident, kind=kind, target=str(path), unusable=unusable_reason(ident)
    )


@agent_facing(topic="extensions")
def discover_dropins(directory: str | Path) -> list[ExtensionSource]:
    """List the drop-ins in ``directory``, one level deep, importing nothing (§6).

    A drop-in is a ``*.py`` file other than ``__init__.py``, or a
    subdirectory holding an ``__init__.py``.

    Args:
        directory: The extensions directory (``~`` is expanded).

    Returns:
        One source per drop-in, sorted by name; a missing directory yields none.
    """
    root = Path(directory).expanduser()
    if not root.is_dir():
        return []
    found: list[ExtensionSource] = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
            found.append(_source_for_file(entry, "dropin"))
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.append(_source_for_file(entry, "dropin"))
    return found


def _editable_path(dist: metadata.Distribution) -> str | None:
    """The source directory of an editable install, from PEP 610's ``direct_url.json``."""
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    info = json.loads(raw)
    if not info.get("dir_info", {}).get("editable"):
        return None
    return unquote(urlparse(str(info.get("url", ""))).path) or None


def _package_digest(dist: metadata.Distribution, module: str) -> str | None:
    """``sha256:`` over RECORD's hashes for the files of ``module``'s top-level package."""
    top = module.split(".", 1)[0]
    lines = sorted(
        f"{f.as_posix()}={f.hash.mode}:{f.hash.value}"
        for f in (dist.files or [])
        if f.hash is not None and f.parts and f.parts[0] in (top, f"{top}.py")
    )
    if not lines:
        return None
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


@agent_facing(topic="extensions")
def discover_packages() -> list[ExtensionSource]:
    """List the installed extensions declared in :data:`ENTRY_POINT_GROUP`, importing nothing.

    Reads distribution metadata only: the entry points, RECORD and
    ``direct_url.json`` (§5). An entry point must name a module; one naming
    ``module:attr`` is listed as unusable.

    Returns:
        One source per entry point, sorted by identifier.
    """
    found: list[ExtensionSource] = []
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = ep.dist
        unusable = None
        if ep.attr:
            unusable = (
                f"entry point {ep.name} = {ep.value!r} names an attribute; τ loads a module "
                f"exposing register(api), so declare {ep.module!r}"
            )
        elif dist is None:
            unusable = f"entry point {ep.name} = {ep.value!r} has no distribution metadata"
        editable = None if dist is None else _editable_path(dist)
        digest = None if dist is None or editable else _package_digest(dist, ep.module)
        found.append(
            ExtensionSource(
                identifier=ep.module,
                kind="package",
                target=ep.module,
                distribution=None if dist is None else dist.metadata["Name"],
                version=None if dist is None else dist.version,
                entry_point=ep.name,
                digest=digest,
                editable_path=editable,
                unusable=unusable,
            )
        )
    return sorted(found, key=lambda source: source.identifier)


class _UnreadableLayout(Exception):
    """An editable install records a finder or ``.pth`` whose layout cannot be read.

    Attributes:
        hook_only: True when the only thing recorded is an import-hook ``.pth``
            (Hatchling's ``editables``), whose redirector keeps module names;
            False for a finder or ``.pth`` that could not be read at all.
    """

    def __init__(self, message: str, *, hook_only: bool = False) -> None:
        """Carry the reason and whether only an import hook was found."""
        super().__init__(message)
        self.hook_only = hook_only


def _finder_mapping(tree: ast.Module) -> dict[str, Path] | None:
    """The literal ``MAPPING`` a setuptools finder assigns, annotated or not; ``None`` if absent."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            named = any(isinstance(t, ast.Name) and t.id == "MAPPING" for t in node.targets)
        elif isinstance(node, ast.AnnAssign):
            named = isinstance(node.target, ast.Name) and node.target.id == "MAPPING"
        else:
            continue
        if not named or node.value is None:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            return None
        if not isinstance(value, dict):
            return None
        return {str(k): Path(str(v)) for k, v in value.items()}
    return None


def _editable_layout(dist: metadata.Distribution) -> tuple[dict[str, Path], list[Path]] | None:
    """Where an editable install puts its modules, read from its RECORD without importing it.

    Two shapes, both listed in RECORD: a setuptools ``__editable___*_finder.py``
    whose literal ``MAPPING`` (``MAPPING = …`` or ``MAPPING: dict[str, str] = …``)
    maps a top-level name to its directory, and a ``.pth`` whose non-``import``
    lines are ``sys.path`` entries (a source root, or the link tree of
    ``editable_mode=strict``). A ``.pth`` of only ``import`` lines is a finder's hook.

    Returns:
        ``(mapping, path_entries)``, or ``None`` when RECORD lists neither shape.

    Raises:
        _UnreadableLayout: A finder cannot be read, parsed, or holds no literal
            ``MAPPING``; a ``.pth`` cannot be read; or the files RECORD lists
            yield no layout at all. Guessing instead is how a layout went unread.
    """
    mapping: dict[str, Path] = {}
    entries: list[Path] = []
    seen: list[str] = []
    for f in dist.files or []:
        name = f.name
        located = Path(str(dist.locate_file(f)))
        if name.startswith("__editable__") and name.endswith("_finder.py"):
            seen.append(name)
            try:
                tree = ast.parse(located.read_text())
            except (OSError, SyntaxError, ValueError) as exc:
                raise _UnreadableLayout(f"its finder {located} cannot be parsed ({exc})") from exc
            found = _finder_mapping(tree)
            if found is None:
                raise _UnreadableLayout(f"its finder {located} holds no literal MAPPING")
            mapping.update(found)
        elif name.endswith(".pth"):
            seen.append(name)
            try:
                lines = located.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                raise _UnreadableLayout(f"its {located} cannot be read ({exc})") from exc
            for line in (raw.strip() for raw in lines):
                if line and not line.startswith(("#", "import ", "import\t")):
                    entries.append(located.parent / line)
    if not seen:
        return None
    if not mapping and not entries:
        names = ", ".join(seen)
        verb = "names" if len(seen) == 1 else "name"
        raise _UnreadableLayout(
            f"its {names} {verb} no source directory τ can read (an import hook)",
            hook_only=True,
        )
    return mapping, entries


def _editable_candidates(module: str, mapping: dict[str, Path], entries: list[Path]) -> list[Path]:
    """The files ``module`` would load from under an editable layout (:func:`_editable_layout`)."""
    found: list[Path] = []
    for key in sorted(mapping, key=len, reverse=True):
        if module == key or module.startswith(key + "."):
            rest = [part for part in module[len(key) :].split(".") if part]
            base = mapping[key].joinpath(*rest)
            found += [base / "__init__.py", base.with_name(base.name + ".py")]
            break
    stem = module.replace(".", "/")
    for entry in entries:
        found += [entry / f"{stem}.py", entry / stem / "__init__.py"]
    return found


def _module_files(source: ExtensionSource) -> set[Path]:
    """The installed files that are ``source``'s module, read from metadata only.

    ``a.b.c`` is ``a/b/c.py`` or ``a/b/c/__init__.py``: located through RECORD for
    an ordinary install. An editable install's RECORD lists only its finder or
    ``.pth``, so its real layout is read from those (:func:`_editable_layout`);
    only an editable install whose RECORD lists neither is probed at ``<root>``
    and ``<root>/src`` of its ``direct_url.json`` directory.

    Raises:
        _UnreadableLayout: The editable install records a layout that cannot be read.
    """
    stem = source.target.replace(".", "/")
    rels = {f"{stem}.py", f"{stem}/__init__.py"}
    found: set[Path] = set()
    dist: metadata.Distribution | None = None
    if source.distribution:
        try:
            dist = metadata.distribution(source.distribution)
        except metadata.PackageNotFoundError:
            dist = None
    if source.editable_path:
        layout = _editable_layout(dist) if dist is not None else None
        if layout is not None:
            candidates = _editable_candidates(source.target, *layout)
        else:
            root = Path(source.editable_path)
            candidates = [base / rel for base in (root, root / "src") for rel in rels]
        found.update(c.resolve() for c in candidates if c.is_file())
    if dist is not None:
        for f in dist.files or []:
            if f.as_posix() in rels:
                found.add(Path(str(dist.locate_file(f))).resolve())
    return found


def _could_be_module(target: Path, module: str, *, hook_only: bool) -> bool:
    """Whether file ``target`` could be ``module`` when the layout cannot be read.

    Under an import hook that keeps names (Hatchling's ``editables``, which
    refuses a prefix-changing source in dev mode), ``a.b.c`` can only be a
    ``c.py`` or a ``c/__init__.py``. A setuptools finder can map any package,
    nested ones included, to a directory of any name, so under an unreadable
    finder every package directory could be the module, and so could ``c.py``.
    """
    last = module.rsplit(".", 1)[-1]
    if target.name == "__init__.py":
        return not hook_only or target.parent.name == last
    return target.name == f"{last}.py"


def _package_owning(path: Path, packages: list[ExtensionSource]) -> ExtensionSource | None:
    """The discovered package whose module file ``path`` is, else ``None``.

    Raises:
        ExplicitExtensionError: ``path`` is under an editable package's source
            directory, is named so it could be that package's module
            (:func:`_could_be_module`), and the package's layout cannot be read,
            so whether it is cannot be told. Any other path, and any unreadable
            package elsewhere, is not refused on that account.
    """
    target = path.resolve()
    if target.is_dir():
        target = target / "__init__.py"
    for source in packages:
        if source.kind != "package":
            continue
        try:
            files = _module_files(source)
        except _UnreadableLayout as exc:
            root = Path(source.editable_path or "/").resolve()
            if (
                source.editable_path
                and target.is_relative_to(root)
                and _could_be_module(target, source.target, hook_only=exc.hook_only)
            ):
                raise ExplicitExtensionError(
                    f"-e {path}: can't tell whether this is the module {source.target} of the "
                    f"editable package {source.distribution} {source.version}, because {exc}. "
                    f"Name the module instead (-e {source.target})."
                ) from exc
            continue
        if target in files:
            return source
    return None


BYTECODE_SUFFIXES = (".pyc", ".pyo")
"""``-e`` values refused outright: a bytecode file hides its source from the two-identities check."""


@agent_facing(topic="extensions")
def is_path_spec(raw: str) -> bool:
    """Whether an ``-e`` value names a path: it holds a ``/`` or ends in ``.py``."""
    return "/" in raw or os.sep in raw or raw.endswith(".py")


@agent_facing(topic="extensions")
def resolve_explicit(raw: str, packages: list[ExtensionSource]) -> ExtensionSource:
    """The source an ``-e`` value names; being named is its authorization (§5).

    A value holding ``/`` or ending in ``.py`` is a path. Anything else is a
    module name, tried as an entry point's module, then as the last dotted
    segment of one (§4), then as any importable module.

    Args:
        raw: The ``-e`` value.
        packages: The discovered packages (:func:`discover_packages`).

    Returns:
        The source.

    Raises:
        ExplicitExtensionError: The value names compiled bytecode (``.pyc``), the
            path is an installed package's module file (it would load twice under
            two identities), its stem is unusable, the shorthand is ambiguous, or
            nothing by that name is installed.
    """
    if raw.endswith(BYTECODE_SUFFIXES):
        raise ExplicitExtensionError(
            f"-e {raw}: that is compiled bytecode, which τ does not load; "
            "load the .py source or name the module"
        )
    if is_path_spec(raw):
        expanded = os.path.expanduser(raw)
        if not is_path_spec(expanded.rstrip("/")):
            expanded = os.path.abspath(expanded)
        owner = _package_owning(Path(expanded), packages)
        if owner is not None:
            raise ExplicitExtensionError(
                f"-e {raw}: that is the module {owner.identifier} of the installed package "
                f"{owner.distribution} {owner.version}. Name it by module (-e {owner.identifier}) "
                f"or authorize it (tau ext authorize {owner.identifier}); loading it by path "
                "would load the same code a second time under another identity."
            )
        source = _source_for_file(Path(expanded), "path")
        if source.unusable is not None:
            raise ExplicitExtensionError(f"-e {raw}: cannot load it, because {source.unusable}")
        return source
    usable = {p.identifier: p for p in packages if p.unusable is None}
    try:
        resolved = resolve_shorthand(raw, list(usable))
    except ValueError as err:
        raise ExplicitExtensionError(f"-e {raw}: {err}") from None
    if resolved is not None:
        return usable[resolved]
    try:
        spec = importlib.util.find_spec(raw)
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return ExtensionSource(identifier=raw, kind="module", target=raw)
    raise ExplicitExtensionError(
        f"-e {raw}: no installed module or {ENTRY_POINT_GROUP} entry point is named {raw!r}. "
        "A path needs a '/' or a '.py' suffix (e.g. ./my_ext)."
    )
