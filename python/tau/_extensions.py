"""Loading extensions (D19): the sources, their order, `load`, and what is said when one fails.

A **source** is one place extension code comes from, in five rows: a built-in, an installed
package's registration (group `tau.extensions`), a user drop-in (`$TAU_HOME/extensions/`), a
project drop-in (`.tau/extensions/`, from the bytes trust approved) and `-e`. The rows are the
load order, and for one **key** the nearest row wins: the others are `shadowed` and never
imported. Every source but a built-in is read by one rule (`_exports`, C3's): a module with a
`str` `name` is the extension, else its `extensions` is a non-empty list or tuple of extension
objects.

`inspect_extensions` does the whole job: it plans the sources, imports each that is on, runs the
driver's definition checks on each so a failure is pinned to its source, then the checks across
them, and reports every failure. `load_extensions` raises them as one `ExtensionLoadError`;
`list_extensions` shows them as rows; `tau config check` and `set` use `inspect_extensions`.
"""

import hashlib
import importlib
import importlib.metadata
import importlib.util
import os
import shlex
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

from tau._attachments import Attachments
from tau._base_tools import BaseTools
from tau._builtin import SESSION_NAME, Tau
from tau._commands import assemble_commands
from tau._config import ConfigError
from tau._config_layers import ModelCatalog, read_models
from tau._options import option_fields
from tau._project_code import ApprovedFiles, forget
from tau._project_read import TrustError
from tau._protocols import EXTENSION_NAME, Extension, check_extensions, check_tools
from tau._skills import SkillFileError, Skills
from tau._startup import LoadedConfig
from tau._step_limit import StepLimit
from tau._tools import assemble_tools
from tau._trust import (
    Trusted,
    UntrustedProject,
    ensure_trusted,
    find_project,
    project_environment,
    require_private,
    tau_home,
    user_config_path,
)
from tau._usage import Usage
from tau._user_config import UserConfig

SourceState = Literal["always", "on", "off", "shadowed", "failed", "untrusted"]
"""Whether a source's extensions are loaded: `always` (`tau` and `session_name`), `on`, `off`,
`shadowed` by another source, `failed`, or `untrusted`."""
SourceKind = Literal["built-in", "package", "user", "project", "explicit", "load"]
"""Where a source comes from: `built-in`, `package`, `user`, `project`, `explicit` (`-e`), or
`load` (a `load` key no source has)."""

GROUP = "tau.extensions"
POINT = "entry point"  # rules: allow(entry): what Python packaging calls a registration
RULE = (
    "a module with a `name` (a str) is the extension; otherwise it must define "
    "`extensions = [...]`, a non-empty list of extension instances"
)
RETRY = "retry"
"""The `load` key of the provider wrapper `RetryProvider` (D24 section 4): not an extension, but
it is switched on and off, and listed, like one."""


def _skills(loaded: LoadedConfig) -> Skills:
    """`Skills` for the files `loaded` reads; a file it cannot use stops the start."""
    try:
        return Skills.of(loaded)
    except SkillFileError as e:
        raise ExtensionLoadError(str(e)) from None


_BUILTINS: Mapping[str, Callable[[LoadedConfig], object]] = {
    Attachments.name: lambda loaded: Attachments(Path.cwd()),
    BaseTools.name: lambda loaded: BaseTools(),
    Skills.name: _skills,
    StepLimit.name: lambda loaded: StepLimit(),
    Usage.name: lambda loaded: Usage(),
}
_DISCOVERED = ("package", "user", "project")  # the rows `--no-extensions` turns off
_ROW: Mapping[str, int] = {"built-in": 1, "package": 2, "user": 3, "project": 4, "explicit": 5}
_BEYOND = "(or: tau --no-extensions)"
_PRIVATE_INIT = "__init__.py"


class ExtensionLoadError(ImportError):
    """One or more extension sources did not load: the message is every failure (what failed,
    its source, the underlying error, and the command that gets past it), and `sources` their
    `ExtensionSource` records. The first underlying exception is `__cause__`."""

    def __init__(self, message: str, sources: Sequence["ExtensionSource"] = ()) -> None:
        super().__init__(message)
        self.sources = tuple(sources)


@dataclass(frozen=True)
class ExtensionSource:
    """One row of `tau ext list`: the source's `key`, its `state` (`always`: `tau` and
    `session_name`; `on`; `off`; `shadowed`, by what; `failed`; `untrusted`), its `kind`
    (`built-in`, `package`, `user`, `project`, `explicit` for `-e`, or `load` for a `load` key
    that no source has), `origin` (where it is) and `detail` (why it is off, shadowed, untrusted
    or failed). `extensions` are the names of the extensions it provides, `commands` their
    commands' qualified names and `tools` their tools' names; all empty unless it is on."""

    key: str
    state: SourceState
    kind: SourceKind
    origin: str
    detail: str | None = None
    extensions: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    """A source found, not yet imported. `importer` makes its extension objects (`None`: it
    cannot be: `problem` says why, or it is `untrusted`). `where` is how a failure names it,
    `tell` the line that gets past a failure, `own` where its files are and `top` the top-level
    module of an installed package (both for the traceback)."""

    key: str
    kind: SourceKind
    origin: str
    where: str
    tell: str
    importer: Callable[[], list[object]] | None = None
    problem: str | None = None
    untrusted: bool = False
    own: Path | None = None
    top: str | None = None

    @property
    def row(self) -> int:
        return _ROW[self.kind]


@dataclass(frozen=True)
class Inspection:
    """What `inspect_extensions` found: a row per source in load order (`tau` and `session_name`,
    which the startup adds, first), the `extensions` of those that loaded in hook order (neither
    of those two), and each failure's message with the exceptions behind them."""

    sources: tuple[ExtensionSource, ...]
    extensions: tuple[Extension, ...]
    errors: tuple[str, ...]
    causes: tuple[BaseException, ...]

    def raise_failures(self) -> None:
        """`ExtensionLoadError` carrying every failure, if there is one."""
        if self.errors:
            failed = tuple(s for s in self.sources if s.state == "failed")
            raise ExtensionLoadError("\n\n".join(self.errors), failed) from (
                self.causes[0] if self.causes else None
            )


@dataclass(frozen=True)
class _Inputs:
    home: Path
    catalog: ModelCatalog
    project: Trusted | None
    untrusted: Path | None  # the root of a project that is not trusted (`tau ext list` only)
    loaded: LoadedConfig  # what a built-in is built with (D24 M9)


# -- the C3 rule and the checks on one source


def _exports(module: object, where: str) -> list[object]:
    """The extensions a module provides: itself if it has a `str` `name`, else the objects of
    its `extensions`. `ExtensionLoadError` naming the source and the rule for anything else."""
    if isinstance(getattr(module, "name", None), str):
        return [module]
    listed = getattr(module, "extensions", None)
    if not isinstance(listed, list | tuple) or not listed:
        seen = f" (its `extensions` is {listed!r})" if hasattr(module, "extensions") else ""
        raise ExtensionLoadError(f"{where} provides no extension: {RULE}{seen}")
    return list(cast("Sequence[object]", listed))


@dataclass(frozen=True)
class _Provides:
    extensions: list[Extension]
    commands: list[str]  # qualified names
    tools: list[str]


def _checked(provided: list[object]) -> _Provides:
    """`check_extensions`, the tools, the commands and the options types of one source's
    extensions: the checks the driver makes, here so a failure names its source."""
    checked = check_extensions(provided)
    listed, _ = assemble_tools((), checked)
    tools, _ = check_tools(listed)
    commands = assemble_commands(checked)
    for extension in checked:
        options = getattr(extension, "options", None)
        if options is not None:
            option_fields(extension.name, options)
    return _Provides(list(checked), list(commands), list(tools))


# -- importing


def _module_name(path: Path) -> str:
    """The private module name of a file or directory: derived from where it is, so two files
    with one stem are two modules."""
    return "_tau_extension_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def _import_path(path: Path) -> ModuleType:
    """Import the file `path` as a module, or the directory `path` as a package (relative
    imports work), under its private name; what a failed import left is forgotten."""
    package = path.is_dir()
    name = _module_name(path)
    forget(name)
    spec = importlib.util.spec_from_file_location(
        name,
        path / _PRIVATE_INIT if package else path,
        submodule_search_locations=[str(path)] if package else None,
    )
    if spec is None or spec.loader is None:
        raise ExtensionLoadError(f"{path} is not a Python file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass or a type hint looks its module up here
    try:
        spec.loader.exec_module(module)
    except BaseException:
        forget(name)
        raise
    return module


# -- finding the sources


def _bad_key(key: str) -> str | None:
    if EXTENSION_NAME.fullmatch(key):
        return None
    return f"the key {key!r} is not letters, digits, `_` and `-`: rename it"


def _off_switch(key: str, *, discovered: bool = True) -> str:
    """The line that gets past a failed source. `--no-extensions` skips only discovered sources,
    so it is offered for those and not for a built-in."""
    return f"to start without it: tau ext disable {key}" + (f"   {_BEYOND}" if discovered else "")


def _builtins(loaded: LoadedConfig) -> list[_Candidate]:
    made = [
        _Candidate(
            key,
            "built-in",
            "built-in",
            f"built-in {key}",
            _off_switch(key, discovered=False),
            lambda m=make: [m(loaded)],
        )
        for key, make in _BUILTINS.items()
    ]
    wrapper = _Candidate(
        RETRY,
        "built-in",
        "built-in (provider wrapper)",
        f"built-in {RETRY}",
        _off_switch(RETRY, discovered=False),
        list,
    )
    return sorted([*made, wrapper], key=lambda c: c.key)


def _packages() -> list[_Candidate]:
    found: list[_Candidate] = []
    points = importlib.metadata.entry_points(group=GROUP)  # rules: allow(entry): packaging API
    for point in sorted(points, key=lambda p: p.name):
        dist = point.dist
        who = "package" if dist is None else f"package {dist.metadata['Name']} {dist.version}"
        where = f"{who}, {POINT} {point.name} = {point.value}"
        problem = _bad_key(point.name)
        if ":" in point.value:
            problem = f"{POINT} {point.name} = {point.value}: name a module, not module:attr"

        def make(value: str = point.value, source: str = where) -> list[object]:
            return _exports(importlib.import_module(value), source)

        found.append(
            _Candidate(
                point.name,
                "package",
                f"{who} ({point.value})",
                where,
                _off_switch(point.name),
                None if problem else make,
                problem,
                top=point.module,
            )
        )
    return found


def _dropin(
    key: str, kind: SourceKind, path: Path, importer: Callable[[], list[object]]
) -> _Candidate:
    shown = f"project {path}" if kind == "project" else str(path)
    remove = ", or remove the file" if kind == "project" else ""
    problem = _bad_key(key)
    tell = f"to start without it: tau ext disable {key}{remove}   {_BEYOND}"
    own = path.parent if kind == "project" else path
    return _Candidate(
        key, kind, shown, shown, tell, None if problem else importer, problem, own=own
    )


def _listing(directory: Path) -> list[tuple[str, Path]]:
    """The drop-ins in `directory`: (key, path) for each `.py` file and each directory with an
    `__init__.py`; names starting with `.` or `_` and anything else are ignored. Nothing is
    opened."""
    try:
        names = sorted(os.listdir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    found: list[tuple[str, Path]] = []
    for name in names:
        path = directory / name
        if name.startswith((".", "_")):
            continue
        if path.is_dir() and (path / _PRIVATE_INIT).is_file():
            found.append((name, path))
        elif name.endswith(".py") and path.is_file():
            found.append((path.stem, path))
    return found


def _user(home: Path) -> list[_Candidate]:
    directory = home / "extensions"

    def importer(path: Path) -> Callable[[], list[object]]:
        def run() -> list[object]:
            try:
                require_private(directory, "chmod go-w")
                require_private(path, "chmod go-w")
            except TrustError as e:
                raise ExtensionLoadError(str(e)) from None
            return _exports(_import_path(path), str(path))

        return run

    return [_dropin(key, "user", path, importer(path)) for key, path in _listing(directory)]


def _approved(files: Mapping[str, bytes]) -> list[tuple[str, str]]:
    """The sources among a project's approved files: (key, layout) where the layout is
    `ralph.py`, or the package directory `ralph`. Names starting with `.` or `_` are ignored."""
    found: list[tuple[str, str]] = []
    for relative in sorted(files):
        top, slash, rest = relative.partition("/")
        if top.startswith((".", "_")):
            continue
        if not slash and top.endswith(".py"):
            found.append((top.removesuffix(".py"), top))
        elif rest == _PRIVATE_INIT:
            found.append((top, top))
    return found


def _project(inputs: _Inputs) -> list[_Candidate]:
    project = inputs.project
    if project is None or project.extension_files is None:
        return [] if inputs.untrusted is None else _untrusted(inputs.untrusted)
    directory = project.root / ".tau" / "extensions"
    files = project.extension_files

    def importer(layout: str) -> Callable[[], list[object]]:
        path = directory / layout
        prefix = _module_name(path)

        def run() -> list[object]:
            ApprovedFiles(prefix, directory, files, layout).install()
            return _exports(importlib.import_module(prefix), f"project {path}")

        return run

    return [
        _dropin(key, "project", directory / layout, importer(layout))
        for key, layout in _approved(files)
    ]


def _untrusted(root: Path) -> list[_Candidate]:
    """The names a project that is not trusted has under `.tau/extensions/`, from the directory
    listing alone: nothing is read or imported."""
    directory = root / ".tau" / "extensions"
    if directory.is_symlink():
        return []
    return [
        _Candidate(
            key,
            "project",
            f"project {path}",
            f"project {path}",
            f"to use it: tau trust {shlex.quote(str(root))}",
            untrusted=True,
        )
        for key, path in _listing(directory)
        if not path.is_symlink()
    ]


def absolute_explicit(values: Sequence[str], cwd: Path) -> tuple[str, ...]:
    """`-e` values with each path made absolute against `cwd` (a key is left as it is). A
    program that changes directory calls this first, so `-e ./x.py` means the file the user
    typed it beside (D18 section 5, step 0)."""
    return tuple(os.path.abspath(cwd / v) if _is_path(v) else v for v in values)


def _is_path(value: str) -> bool:
    return "/" in value or value.endswith(".py")


def _explicit(values: Sequence[str]) -> tuple[list[_Candidate], set[str]]:
    """The `-e` sources in argument order, and the keys of discovered sources it names (those
    load whatever `load` says). A relative path is taken against the working directory."""
    found: list[_Candidate] = []
    named: set[str] = set()
    for value in values:
        if value.endswith(".pyc"):
            problem = f"-e {value}: a compiled file is not a source; name the .py file"
            found.append(_explicit_problem(value, value, problem))
        elif _is_path(value):
            found.append(_explicit_path(Path(os.path.abspath(value)), value))
        else:
            named.add(value)
    return found, named


def _explicit_problem(key: str, value: str, problem: str) -> _Candidate:
    tell = f"fix it or drop the flag: -e {value}"
    return _Candidate(key, "explicit", f"-e {value}", f"-e {value}", tell, problem=problem)


def _explicit_path(path: Path, value: str) -> _Candidate:
    package = path.is_dir()
    key = path.name if package else path.stem
    problem = _bad_key(key)
    if not path.exists():
        problem = f"-e {value}: no such file or directory"
    elif package and not (path / _PRIVATE_INIT).is_file():
        problem = f"-e {value}: a directory needs an __init__.py to be a package"
    elif not package and path.suffix != ".py":
        problem = f"-e {value}: not a Python file (.py) or a package directory"
    where = f"-e {path}"
    return _Candidate(
        key,
        "explicit",
        where,
        where,
        f"fix it or drop the flag: -e {value}",
        None if problem else (lambda: _exports(_import_path(path), where)),
        problem,
        own=path,
    )


# -- deciding what is on, and saying why not


def _reason_off(candidate: _Candidate, catalog: ModelCatalog, discover: bool) -> str | None:
    if candidate.kind in _DISCOVERED and not discover:
        return "--no-extensions"
    if catalog.load.get(candidate.key) is False:
        return f"load.{candidate.key} = false in {catalog.load_sources[candidate.key]}"
    return None


def _own_lines(error: BaseException, candidate: _Candidate) -> list[tuple[Path, int, str]]:
    """The (file, line number, code) of each step of the traceback that is in the source's own
    files."""
    root = candidate.own
    if root is None and candidate.top is not None:
        root = _package_root(candidate.top)
    if root is None:
        return []
    steps = traceback.extract_tb(error.__traceback__)
    return [
        (Path(s.filename), s.lineno or 0, s.line or "")
        for s in steps
        if Path(s.filename).is_relative_to(root)
    ]


def _package_root(top: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(top.split(".", maxsplit=1)[0])
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    return None if spec.origin is None else Path(spec.origin)


def _describe(error: Exception, candidate: _Candidate) -> tuple[list[str], str]:
    """The lines of a source's failure (its own traceback lines, then the exception) and the
    one line `tau ext list` shows."""
    own = isinstance(error, ExtensionLoadError)
    text = str(error) if own else f"{type(error).__name__}: {error}"
    own_lines = _own_lines(error, candidate)
    lines = [f"{file}:{number}: {code}" for file, number, code in own_lines if code]
    first, *rest = text.split("\n")
    at = f" ({own_lines[-1][0].name}:{own_lines[-1][1]})" if own_lines else ""
    return [*lines, first, *rest], first + at


def _block(key: str, sources: Sequence[str], lines: Sequence[str], tell: str) -> str:
    head = f'extension "{key}" failed to load; tau did not start.'
    body = [*(f"  source: {s}" for s in sources), *(f"  {line}" for line in lines)]
    return "\n".join([head, *body, f"  {tell}"])


@dataclass
class _Run:
    """The outcome being built, in load order."""

    sources: list[ExtensionSource] = field(default_factory=lambda: [])
    extensions: list[Extension] = field(default_factory=lambda: [])
    errors: list[str] = field(default_factory=lambda: [])
    causes: list[BaseException] = field(default_factory=lambda: [])
    names: dict[str, str] = field(default_factory=lambda: {})
    tools: dict[str, str] = field(default_factory=lambda: {})

    def row(self, c: _Candidate, state: SourceState, detail: str | None = None) -> None:
        self.sources.append(ExtensionSource(c.key, state, c.kind, c.origin, detail))

    def fail(self, c: _Candidate, lines: Sequence[str], detail: str) -> None:
        self.errors.append(_block(c.key, [c.where], lines, c.tell))
        self.row(c, "failed", detail)

    def load(self, c: _Candidate) -> None:
        assert c.importer is not None
        try:
            provided = _checked(c.importer())
        except Exception as e:
            lines, detail = _describe(e, c)
            self.causes.append(e)
            self.fail(c, lines, detail)
            return
        clash = self._clash(c, provided)
        if clash is not None:
            self.fail(c, [clash], clash)
            return
        self.extensions += provided.extensions
        names = tuple(e.name for e in provided.extensions)
        self.sources.append(
            ExtensionSource(
                c.key,
                "on",
                c.kind,
                c.origin,
                None,
                names,
                tuple(provided.commands),
                tuple(provided.tools),
            )
        )

    def _clash(self, c: _Candidate, provided: _Provides) -> str | None:
        """Two sources providing one extension name or one tool name (names, not commands:
        those are qualified), naming both."""
        for extension in provided.extensions:
            before = self.names.setdefault(extension.name, c.where)
            if before != c.where:
                return f"two extensions are named {extension.name!r}: {before} and {c.where}"
        for tool in provided.tools:
            before = self.tools.setdefault(tool, c.where)
            if before != c.where:
                return f"two tools are named {tool!r}: {before} and {c.where}"
        return None


def _winners(candidates: Sequence[_Candidate]) -> dict[str, list[_Candidate]]:
    """For each key, the candidates in the nearest row that has it."""
    best: dict[str, list[_Candidate]] = {}
    for c in candidates:
        held = best.get(c.key)
        if held is None or c.row > held[0].row:
            best[c.key] = [c]
        elif c.row == held[0].row:
            held.append(c)
    return best


def _settle(
    candidates: Sequence[_Candidate], inputs: _Inputs, discover: bool, run: _Run, named: set[str]
) -> None:
    winners = _winners(candidates)
    for c in candidates:
        won = winners[c.key]
        if c.row < won[0].row:
            run.row(c, "shadowed", f"by {won[0].origin}")
        elif len(won) > 1:
            others = ", ".join(w.where for w in won if w is not c)
            if c is won[0]:
                lines = [f"{len(won)} sources provide the key {c.key!r}"]
                run.errors.append(
                    _block(c.key, [w.where for w in won], lines, "uninstall or remove one of them")
                )
            run.row(c, "failed", f"the key {c.key!r} is also provided by {others}")
        elif c.problem is not None:
            run.fail(c, [c.problem], c.problem)
        elif c.untrusted:
            run.row(c, "untrusted", c.tell)
        elif (off := _off(c, inputs, discover, named)) is not None:
            run.row(c, "off", off)
        else:
            run.load(c)


def _off(c: _Candidate, inputs: _Inputs, discover: bool, named: set[str]) -> str | None:
    if c.kind == "explicit" or c.key in named:
        return None  # naming a source is the consent
    return _reason_off(c, inputs.catalog, discover)


def _load_keys(candidates: Sequence[_Candidate], inputs: _Inputs, run: _Run) -> None:
    """A `load` key that no source has: `true` stops the start, `false` is a row that warns."""
    catalog = inputs.catalog
    for key in sorted(set(catalog.load) - {c.key for c in candidates}):
        on = catalog.load[key]
        origin = f"load.{key} = {str(on).lower()} in {catalog.load_sources[key]}"
        if on:
            note = f"no extension source has the key {key!r}"
            run.errors.append(
                f"{origin}: {note}; tau did not start.\n  fix the key, or remove it from `load`"
            )
            run.sources.append(ExtensionSource(key, "failed", "load", origin, note))
        else:
            note = f"no extension source has the key {key!r} (a typo?)"
            run.sources.append(ExtensionSource(key, "off", "load", origin, note))


def _inspect(inputs: _Inputs, explicit: Sequence[str], discover: bool) -> Inspection:
    given, named = _explicit(explicit)
    candidates = [
        *_builtins(inputs.loaded),
        *_packages(),
        *_user(inputs.home),
        *_project(inputs),
        *given,
    ]
    keys = {c.key for c in candidates}
    for key in sorted(named - keys):  # `-e KEY` for a key no discovered source has
        problem = f"-e {key}: no extension source has this key (a path has a / or ends in .py)"
        candidates.append(_explicit_problem(key, key, problem))
    run = _Run()
    tau = Tau()
    run.names[tau.name] = "built-in tau"
    run.names[SESSION_NAME] = f"built-in {SESSION_NAME}"
    _settle(candidates, inputs, discover, run, named)
    _load_keys(candidates, inputs, run)
    first = ExtensionSource(
        tau.name, "always", "built-in", "built-in", None, (tau.name,), ("tau:model",)
    )
    added = ExtensionSource(
        SESSION_NAME,
        "always",
        "built-in",
        "built-in (added by the startup)",
        None,
        (SESSION_NAME,),
        (f"{SESSION_NAME}:name",),
    )
    return Inspection(
        (first, added, *run.sources), tuple(run.extensions), tuple(run.errors), tuple(run.causes)
    )


# -- the public functions


def inspect_extensions(
    loaded: LoadedConfig, *, explicit: Sequence[str] = (), discover: bool = True
) -> Inspection:
    """Plan, import and check the sources of `loaded` (the arguments of `load_extensions`);
    every failure is in the result, none is raised."""
    return _inspect(
        _Inputs(loaded.home, loaded.catalog, loaded.project, None, loaded), explicit, discover
    )


def load_extensions(
    loaded: LoadedConfig, *, explicit: Sequence[str] = (), discover: bool = True
) -> list[Extension]:
    """The extensions to run, in hook order (the driver puts `tau` first; a program adds
    `session_name`, D18, after this returns).

    The sources are the built-ins that `load` leaves on and, with `discover`, the installed
    packages and the user's and the trusted project's drop-ins that it leaves on; then
    `explicit` (`-e` values: paths, or the keys of sources, discovered or built-in, which load
    whatever `load` says; relative paths are taken against the working directory, see
    `absolute_explicit`). Without `discover` (`--no-extensions`) the packages and drop-ins are
    skipped, `explicit` still loads, and the built-ins (`base_tools`, `step_limit`) stay as `load`
    says. `base_tools` is built when this runs and roots its tools at the process's working
    directory then, so a program that calls this must change directory first, as `start_session`
    does.
    `ExtensionLoadError` naming every source that failed, with the one command that gets past
    each: nothing is started or written by then."""
    found = inspect_extensions(loaded, explicit=explicit, discover=discover)
    found.raise_failures()
    return list(found.extensions)


def _inputs_for(cwd: Path, environ: Mapping[str, str]) -> _Inputs:
    """The home, config and project a start in `cwd` would load from, never asking about trust:
    a project that is not trusted is `untrusted` and its config is not read."""
    home = tau_home(environ)
    root = find_project(cwd, environ).root
    project: Trusted | None = None
    untrusted: Path | None = None
    if root is not None:
        try:
            project = ensure_trusted(root, home, ask=None)
        except UntrustedProject:
            untrusted = root
    layer = None if project is None else project.layer()
    catalog = read_models(user_config_path(home), layer)
    loaded = LoadedConfig(
        catalog, project_environment(project, environ), project, home, UserConfig(home, layer)
    )
    return _Inputs(home, catalog, project, untrusted, loaded)


def list_extensions(
    cwd: Path,
    environ: Mapping[str, str],
    *,
    explicit: Sequence[str] = (),
    discover: bool = True,
) -> list[ExtensionSource]:
    """One `ExtensionSource` per source in load order, `tau` and `session_name` first: what a start
    in `cwd` would import (it imports the same, and shows a failure as a row instead of raising
    it). It
    never asks about trust: a project that is not trusted shows its sources as `untrusted`.
    `ConfigError` or `TrustError` for config or trust files the loader refuses."""
    return list(_inspect(_inputs_for(cwd, environ), explicit, discover).sources)


def switch_source(cwd: Path, environ: Mapping[str, str], key: str, on: bool) -> str | None:
    """Write `load.<key> = on` into the user's config file (`tau ext enable` and `disable`).
    `ConfigError` naming the keys there are, writing nothing, if no source has `key`: the
    sources are found by name, none is imported. Returns the note that a nearer layer (the
    project's) sets the key the other way and wins, else `None`."""
    inputs = _inputs_for(cwd, environ)
    candidates = [*_builtins(inputs.loaded), *_packages(), *_user(inputs.home), *_project(inputs)]
    keys = sorted({c.key for c in candidates})
    if key not in keys:
        raise ConfigError(f"no extension source has the key {key!r}; the keys are {keys}")
    layer = None if inputs.project is None else inputs.project.layer()
    catalog = UserConfig(inputs.home, layer).save_load(key, on)
    if catalog.load.get(key) == on:
        return None
    return (
        f"the project's {catalog.load_sources[key]} sets load.{key} = {str(not on).lower()} "
        "and wins here"
    )
