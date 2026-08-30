"""τ-agent-core sdk: SDK entry point — create_agent_session().

Reference: PHASE-2-SUBPHASE-4.md — Agent Session and SDK Entry Point.
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.
Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (SDK default = InMemorySessionLog).

This module provides:
- create_agent_session(): Main SDK entry point for creating fully configured sessions.
- _resolve_model(): Resolve model string to Model object.
- _resolve_tools(): Discover and create tool objects from string names.
- _load_extensions(): THE single extension loader — discover + import + register(api).
- _build_system_prompt(): Build system prompt from context files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction_policy import CompactionPolicy
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog, SessionLog
from tau_agent_core.tools.base import AgentTool, ToolDefinition
from tau_agent_core.tools.bash import BashTool
from tau_agent_core.tools.edit import EditTool
from tau_agent_core.tools.find import FindTool
from tau_agent_core.tools.grep import GrepTool
from tau_agent_core.tools.ls import LsTool
from tau_agent_core.tools.read import ReadTool
from tau_agent_core.tools.write import WriteTool
from tau_llm.docs import agent_facing

# ─── Default model definitions ───────────────────────────────────────

_DEFAULT_MODELS: dict[str, Model] = {
    "gpt-4o": Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    ),
    "gpt-4": Model(
        id="gpt-4",
        name="GPT-4",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=8192,
        max_tokens=4096,
    ),
    "gpt-4-turbo": Model(
        id="gpt-4-turbo",
        name="GPT-4 Turbo",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    ),
}


@agent_facing(topic="sdk")
def resolve_model(
    model: str,
    provider: str = "openai",
    base_url: str | None = None,
) -> Model:
    """Resolve a model string to a Model object.

    Args:
        model: Model identifier (e.g., "gpt-4o").
        provider: Provider name (default: "openai").
        base_url: Optional custom API base URL.

    Returns:
        Model object with provider configuration.

    Raises:
        KeyError: If model string is not found in defaults.
    """
    if model in _DEFAULT_MODELS:
        m = _DEFAULT_MODELS[model]
        if base_url:
            m.base_url = base_url
        return m
    # Not a known default — build a generic model from the provider/base_url.
    # (No provider implements a resolve_model() hook; pi resolves via a
    # module-level getModel() lookup, so there is no registry path here.)
    return Model(
        id=model,
        name=model,
        api="openai-completions",
        provider=provider,
        base_url=base_url or "https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


# The seven built-in tool classes, imported statically and mapped by name.
#
# This replaced a `{name: (module_name, class_name)}` table walked with
# `getattr(tools_pkg, …)` + `__import__` (B1). The dynamic form bought nothing at
# runtime — `tau_agent_core.tools.__init__` already imports all seven eagerly, so
# both arms resolved the same already-loaded classes — while costing the whole
# point of this task: `getattr` returns `Any`, so mypy could not see what
# `_resolve_tools` was building and could not check its own return annotation.
# Measured, not assumed: with the dynamic form in place, reverting the AgentTool
# wrap below to a bare `tool_objs.append(tool_obj)` left the gate reporting
# "Success: no issues found in 68 source files". Static imports make that
# reversion an error, which is the difference between an annotation mypy enforces
# and one it merely records.
#
# The union type is spelled out rather than hidden behind `type` or a Protocol so
# the checked edge is a real one: `dict[str, type]` re-erases to `Any` and puts us
# back where we started.
_BuiltinToolClass = (
    type[ReadTool]
    | type[WriteTool]
    | type[EditTool]
    | type[BashTool]
    | type[LsTool]
    | type[GrepTool]
    | type[FindTool]
)

_BUILTIN_TOOL_CLASSES: dict[str, _BuiltinToolClass] = {
    "read": ReadTool,
    "write": WriteTool,
    "edit": EditTool,
    "bash": BashTool,
    "ls": LsTool,
    "grep": GrepTool,
    "find": FindTool,
}


def _resolve_tools(
    tool_names: list[str] | None,
    tool_options: dict[str, dict[str, Any]] | None = None,
) -> list[AgentTool]:
    """Resolve tool names to :class:`AgentTool` instances.

    **The registry holds exactly one type (B1).** This function used to return the
    raw built-in classes (``ReadTool()``, ``BashTool()``, …) while extension tools
    arrived as :class:`AgentTool` from
    :meth:`AgentSession._resolve_extension_tools`, so ``AgentLoop._tools`` —
    annotated ``dict[str, AgentTool]`` — actually held two unrelated shapes. mypy had
    no typed edge to check across (``AgentSession.__init__`` took a bare ``list``),
    which is why a green type gate and a green suite both reported success over
    `tau-001`'s ``.definition.execution_mode`` read that crashed on every built-in
    tool call. A gate reporting success over something it cannot see is the defect
    this normalisation removes: the annotation is now true rather than aspirational.

    Each built-in class carries ``name`` / ``label`` / ``description`` /
    ``parameters`` / ``execution_mode`` as plain class attributes and an
    ``async def execute(tool_call_id, args, signal=None, on_update=None)``. Those are
    copied verbatim into a :class:`ToolDefinition`; nothing is defaulted, invented, or
    inferred, and ``execute`` is the instance's own bound method, so the object the
    loop awaits is unchanged.

    ``prompt_snippet`` is set to ``f"{name}: {label}"``, which is exactly the string
    :func:`_build_system_prompt` used to compute in its now-deleted raw-tool branch.
    Moving it to construction time keeps the rendered system prompt byte-identical
    while letting the renderer become a single uniform loop — the alternative
    (leaving it ``None`` and having the renderer fall back) would have silently
    dropped every built-in from the "Available tools" list.

    **Built-in names only, and no fallback (NODE-ADDRESSABLE-AGENTS.md W5).** An
    unrecognised name raises ``ValueError`` rather than being skipped or guessed
    at, and that is deliberate — a session that quietly starts without the tool
    the caller asked for is the failure this repo's Fail-Early rule exists to
    prevent. What W5 says was missing is not a fallback but the POINTER, so:
    **a custom tool does not come through here.** It goes to one of two places,
    both of which already exist:

    - :class:`~tau_agent_core.agent_session.AgentSession`'s own ``tools=``
      constructor parameter, which takes :class:`AgentTool` OBJECTS directly (it
      is the same list this function returns — ``create_agent_session`` merely
      resolves names into it). An SDK caller who wants both built-ins and a custom
      tool can call this function for the names and concatenate.
    - an **extension**, via ``ExtensionAPI.register_tool`` (a plain-dict pi
      ``ToolDefinition``) — the route for a tool that should arrive with a loaded
      extension rather than be wired by the embedder. Those become
      :class:`AgentTool` separately, in
      ``AgentSession._resolve_extension_tools``, and are merged into the loop's
      tools each turn.

    Neither route is a workaround for this one; they are the surfaces that take
    objects, while this one takes names.

    **Construction options (``tool_options``).** A built-in used to be
    constructed with no arguments at all, which meant nothing an operator wrote
    in ``~/.tau/config.json`` could reach one — not even ``cwd``. ``read``'s
    image cap is the first setting that has to, so the seam is a mapping of tool
    name to constructor keyword arguments rather than a parameter named after
    one tool. An option for a name that was not requested is ignored, because
    the tool list is a denylist-filtered set the operator did not spell out; an
    option a tool's ``__init__`` does not accept raises ``TypeError`` from the
    constructor, which is the Fail-Early answer to a typo in a config file.

    Args:
        tool_names: List of BUILT-IN tool name strings (e.g., ["read", "bash"]).
        tool_options: Optional mapping of built-in tool name to keyword
            arguments for that tool's constructor, e.g.
            ``{"read": {"max_image_dimension": 2000}}``.

    Returns:
        List of :class:`AgentTool` — one per requested name, in request order.

    Raises:
        ValueError: If a tool name is not a built-in (see above for where custom
            :class:`AgentTool` objects go instead).
    """
    if not tool_names:
        return []

    options = tool_options or {}
    tool_objs: list[AgentTool] = []
    for name in tool_names:
        if name not in _BUILTIN_TOOL_CLASSES:
            raise ValueError(f"Unknown tool: {name}")

        tool_obj = _BUILTIN_TOOL_CLASSES[name](**options.get(name, {}))
        tool_objs.append(
            AgentTool(
                definition=ToolDefinition(
                    name=tool_obj.name,
                    label=tool_obj.label,
                    description=tool_obj.description,
                    parameters=tool_obj.parameters,
                    execute=tool_obj.execute,
                    prompt_snippet=f"{tool_obj.name}: {tool_obj.label}",
                    execution_mode=tool_obj.execution_mode,
                )
            )
        )

    return tool_objs


# ─── The single extension loader (E0/S1) ────────────────────────────
#
# Verb: ``register(api)``. One loader — file-path importlib, awaits async
# factories, discovery = global ``~/.tau/extensions`` + explicit paths only
# (NO project-local dir, NO importlib.metadata entry_points; deferred to the
# Tier-8 trust gate). Paths are deduped by resolved path, first-wins.
#
# Error policy (Fail-Early): a *discovered* extension that fails to load is
# collected into ``errors`` + logged to stderr and skipped; an *explicit*
# ``-e`` extension that fails **raises** — the user named it, so silently
# skipping it is the anti-pattern.
#
# Reference: pi loader.ts (discoverAndLoadExtensions / loadExtensions /
# loadExtension) — coding-agent/src/core/extensions/loader.ts; the returned
# struct ports pi's LoadExtensionsResult (agent/../types.ts:1590, minus the
# ``runtime`` field, which lands with the API binding in E1/S3).
# docs/EXTENSIONS-IMPLEMENTATION.md E0.1.

_GLOBAL_EXTENSIONS_DIR = "~/.tau/extensions"

# Monotonic counter so each load gets a unique synthetic module name (extensions
# may be re-loaded; distinct names avoid clobbering sys.modules entries).
_ext_load_counter = 0

#: The two module-level attributes an extension file uses to declare itself
#: bus-touching (H7/H8, SIM_SPEC_v2 §16.6/§16.10). Deliberately module-level
#: rather than an ``api.declare_subjects(...)`` call made from inside
#: ``register(api)``: H8 requires the capability be checked BEFORE the
#: extension runs, and the only thing readable before ``register(api)`` is
#: invoked is what the module set at import time — mirroring how ``register``
#: itself is already a required module-level attribute this loader checks.
_TOUCHES_BUS_ATTR = "TOUCHES_BUS"
_SUBJECTS_ATTR = "SUBJECTS"


@agent_facing(topic="extensions")
class ExtensionCapabilityError(Exception):
    """A bus-touching extension's declaration is missing or cannot be honoured.

    Raised at the factory, before ``register(api)`` runs (H8: "refuse rather
    than discover"). Two distinct causes share this type because both are the
    same failure — a declaration nobody validated — one at the writing end
    and one at the checking end:

    - the module sets ``TOUCHES_BUS = True`` but ``SUBJECTS`` is absent, empty,
      or not a sequence of non-empty strings (H7: "a silent omission is a load
      error rather than a hole in the diff");
    - the module declares ``TOUCHES_BUS = True`` and valid ``SUBJECTS``, but the
      session it is loading into has no bus transport
      (``bus_available=False``) — a declared capability the session cannot
      back, refused rather than loaded and left to fail silently the first
      time a handler reaches for a bus it does not have.
    """


@agent_facing(topic="extensions")
@dataclass
class LoadedExtension:
    """A successfully loaded extension.

    Narrowed port of pi's ``Extension`` record (coding-agent types.ts:1577) to
    what S1 needs: the source ``path``, the module-level ``register`` factory
    that was invoked, and the ``ExtensionAPI`` it registered against.

    ``content_hash``, ``subjects`` and ``touches_bus`` are H7/H8's addition
    (SIM_SPEC_v2 §16.6/§16.10): the file's identity at load time, and its
    declared bus subjects, if any. ``content_hash`` is a sha256 of the exact
    bytes compiled — the same source read used to ``exec`` the module — so two
    loads of the same path at different contents produce different hashes and
    are never mistaken for one condition (the pattern §15.1's ``producer``,
    ``Trace.arm``, and H5's compaction policy already established for this
    program: a configuration that changes what a number means is a mandatory
    partition key).
    """

    path: str
    register: Callable[..., Any]
    api: ExtensionAPI
    content_hash: str = ""
    subjects: tuple[str, ...] = ()
    touches_bus: bool = False


@agent_facing(topic="extensions")
@dataclass
class ExtensionLoadError:
    """A discovered extension that failed to load (pi types.ts:1590 errors[])."""

    path: str
    error: str


@agent_facing(topic="extensions")
@dataclass
class LoadExtensionsResult:
    """Result of loading extensions — port of pi ``LoadExtensionsResult``.

    Reference: pi agent/../types.ts:1590. The ``runtime`` field is intentionally
    omitted until the API is bound to the live session (E1/S3).
    """

    extensions: list[LoadedExtension] = field(default_factory=list)
    errors: list[ExtensionLoadError] = field(default_factory=list)


@agent_facing(topic="extensions")
@dataclass
class ExtensionInfo:
    """Read-only summary of one loaded extension for the ``/extensions`` surface.

    Reference: EXTENSIONS-E5-WIRING.md §5 (E5.4 / S34). Carries an extension's
    display ``name``, source ``path``, and the ``tools`` / ``commands`` /
    ``shortcuts`` / ``hooks`` it registered — everything the palette listing shows
    for a loaded extension (shortcuts E10 §6 / S69).

    ``content_hash`` and ``subjects`` are H7's addition (SIM_SPEC_v2 §16.6): the
    file's identity at load time and its declared bus subjects (``()`` for an
    extension that does not touch the bus). This is the pair
    :func:`~tau_agent_core.run_manifest.build_run_manifest` emits into
    ``manifest.json`` beside ``harness`` and ``compaction``.
    """

    name: str
    path: str
    tools: list[str]
    commands: list[str]
    shortcuts: list[str]
    hooks: list[str]
    content_hash: str = ""
    subjects: tuple[str, ...] = ()


@agent_facing(topic="extensions")
def summarize_extensions(result: LoadExtensionsResult) -> list[ExtensionInfo]:
    """Per-extension name/path/tools/commands/hooks from a ``LoadExtensionsResult``.

    Reference: EXTENSIONS-E5-WIRING.md §5 (E5.4 / S34). The palette (``/extensions``)
    reads this to list each loaded extension; ``result.errors`` is surfaced
    alongside by the caller (load failures).

    Each loaded extension's ``api`` is bound to its own runner bucket
    (:class:`~tau_agent_core.extensions.runner.ExtensionHandlers`, labelled by the
    extension's file path — see ``AgentSession._bind_extension_api`` /
    ``_standalone_api_factory``), which is the ONLY place that records which
    extension registered which tool/command/hook. A loaded extension whose api has
    no bucket is a construction bug, so this raises rather than fabricating an empty
    listing (Fail-Early).
    """
    infos: list[ExtensionInfo] = []
    for ext in result.extensions:
        bucket = ext.api._hook_handlers
        if bucket is None:
            raise RuntimeError(
                f"loaded extension {ext.path!r} has no runner bucket; it was not "
                "bound through the extension load path (this is a construction bug)."
            )
        infos.append(
            ExtensionInfo(
                name=Path(ext.path).stem,
                path=ext.path,
                tools=list(bucket.tools),
                commands=list(bucket.commands),
                shortcuts=list(bucket.shortcuts),
                hooks=sorted(bucket.handlers.keys()),
                content_hash=ext.content_hash,
                subjects=ext.subjects,
            )
        )
    return infos


def _discover_extension_paths(user_dir: str) -> list[Path]:
    """Discover extension entry points in a directory (one level, pi-faithful).

    Grammar (pi ``discoverExtensionsInDir``, loader.ts): a bare ``*.py`` file,
    or a package dir (immediate subdir containing ``__init__.py``). No recursion
    beyond one level; no ``package.json`` manifest (deferred, plan §7).

    Args:
        user_dir: Directory to scan (``~`` is expanded).

    Returns:
        Sorted list of entry-point paths (files and package dirs).
    """
    root = Path(user_dir).expanduser()
    if not root.is_dir():
        return []
    discovered: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
            discovered.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            discovered.append(entry)
    return discovered


def _standalone_api_factory(path: str) -> ExtensionAPI:
    """Default per-extension api for the STANDALONE file-path loader (S24).

    This loader is not (yet) attached to a live ``AgentSession``'s
    ``ExtensionRunner`` — nothing here dispatches the mutating hooks. To stay
    bucket-CORRECT rather than degrade to a silent no-op, each api is still given
    its OWN fresh :class:`ExtensionHandlers` bucket keyed by the file path, so
    ``api.on("tool_call"/…)`` registers cleanly (and never raises). A
    session-bound caller that wants those hooks to actually FIRE must supply an
    ``api_factory`` that binds each api to the live session's runner bucket.
    """
    from tau_agent_core.extensions.runner import ExtensionHandlers

    return ExtensionAPI(hook_handlers=ExtensionHandlers(path=path))


async def _load_one_extension(
    path: Path,
    api_factory: Callable[[str], ExtensionAPI],
    *,
    bus_available: bool = False,
) -> LoadedExtension:
    """Import one extension module and invoke its ``register(api)``.

    Imports by file path (``importlib.util.spec_from_file_location``), fetches
    the module-level ``register`` callable, invokes ``register(api)``, and
    awaits the result when ``register`` is a coroutine function.

    H7/H8 (SIM_SPEC_v2 §16.6/§16.10) run BEFORE ``register`` is looked up: an
    extension declares itself bus-touching via two module-level attributes,
    ``TOUCHES_BUS = True`` and ``SUBJECTS = (...)`` — checkable at this point
    because they are set at import time, unlike anything a running
    ``register(api)`` might do. ``TOUCHES_BUS`` with no non-empty ``SUBJECTS``
    is refused (H7: a silent omission must be a load error, not a hole in the
    diff). ``TOUCHES_BUS`` with valid ``SUBJECTS`` but ``bus_available=False``
    is refused too (H8: a declared capability the session cannot back is
    refused at the factory, not discovered later inside a handler that reaches
    for a bus that isn't there). Both raise :class:`ExtensionCapabilityError`
    and neither calls ``register`` — "refuse rather than discover" means the
    extension's side effects never begin.

    Raises on any failure (missing file/spec, missing or non-callable
    ``register``, an unmet H7/H8 declaration, or an exception raised by
    ``register``); the caller applies the explicit-vs-discovered error policy.
    """
    global _ext_load_counter

    if path.is_dir():
        module_file = path / "__init__.py"
        submodule_search: list[str] | None = [str(path)]
    else:
        module_file = path
        submodule_search = None

    if not module_file.is_file():
        raise FileNotFoundError(f"extension not found: {module_file}")

    _ext_load_counter += 1
    module_name = f"_tau_ext_{path.stem}_{_ext_load_counter}"
    spec = importlib.util.spec_from_file_location(
        module_name, module_file, submodule_search_locations=submodule_search
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for extension {module_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        # Compile the source FRESH rather than ``spec.loader.exec_module`` — the
        # default loader reuses a ``__pycache__`` ``.pyc`` keyed by the SOURCE path
        # (not our unique module name), validated only against the source's mtime
        # truncated to whole seconds. A runtime ``/extensions reload`` (E10 §6 / S70)
        # of a file just edited within the same second (especially a same-length
        # edit) would then re-run the STALE bytecode — the reload silently would not
        # take effect. ``module_from_spec`` has already set ``__file__`` / ``__path__``
        # / ``__package__`` on the module (so relative imports inside a package
        # extension still resolve), so exec'ing the freshly compiled code into it is
        # equivalent to ``exec_module`` minus the stale-pyc trap.
        source = module_file.read_bytes()
        code = compile(source, str(module_file), "exec")
        exec(code, module.__dict__)
    except Exception:
        # Don't leave a half-initialized module in the import cache.
        sys.modules.pop(module_name, None)
        raise

    # Content identity (H7): a sha256 of the exact bytes just compiled, so a
    # reload of the same path after an on-disk edit is provably a different
    # condition rather than the same label reused (see LoadedExtension).
    content_hash = hashlib.sha256(source).hexdigest()

    # The declared-capability preflight (H7/H8), BEFORE register is even looked
    # up — see the docstring. sys.modules is cleaned up on refusal for the same
    # reason the except above cleans it up: don't leave a half-admitted module
    # behind for a later import to trip over.
    touches_bus = bool(getattr(module, _TOUCHES_BUS_ATTR, False))
    raw_subjects = getattr(module, _SUBJECTS_ATTR, ())
    subjects: tuple[str, ...] = ()
    if touches_bus:
        subjects = tuple(raw_subjects) if isinstance(raw_subjects, (list, tuple)) else ()
        if not subjects or not all(isinstance(s, str) and s for s in subjects):
            sys.modules.pop(module_name, None)
            raise ExtensionCapabilityError(
                f"{path} sets {_TOUCHES_BUS_ATTR} = True but {_SUBJECTS_ATTR} is "
                f"{raw_subjects!r}, not a non-empty sequence of non-empty subject "
                "strings. An extension that touches the bus must declare which "
                "subjects (H7) — 'leave it unset' is not admissible."
            )
        if not bus_available:
            sys.modules.pop(module_name, None)
            raise ExtensionCapabilityError(
                f"{path} declares {_TOUCHES_BUS_ATTR} = True with subjects "
                f"{subjects!r}, but this session has no bus transport "
                "(bus_available=False). Refusing to load rather than admitting an "
                "extension whose declared capability the session cannot back (H8)."
            )

    register = getattr(module, "register", None)
    if register is None:
        sys.modules.pop(module_name, None)
        raise AttributeError(f"{path} has no register(api) function")
    if not callable(register):
        sys.modules.pop(module_name, None)
        raise TypeError(f"{path} register is not callable")

    # Path-aware (S24): the factory keys each extension's api to its real file
    # path so a session-bound factory can bind it to a fresh runner bucket.
    api = api_factory(str(path))
    outcome = register(api)
    if inspect.isawaitable(outcome):
        await outcome

    return LoadedExtension(
        path=str(path),
        register=register,
        api=api,
        content_hash=content_hash,
        subjects=subjects,
        touches_bus=touches_bus,
    )


async def _load_extensions(
    explicit_paths: list[str] | None = None,
    *,
    discover: bool = True,
    user_dir: str | None = None,
    api_factory: Callable[[str], ExtensionAPI] | None = None,
    collect_explicit_errors: bool = False,
    bus_available: bool = False,
) -> LoadExtensionsResult:
    """Discover, import, and invoke ``register(api)`` for every extension.

    This is THE single extension loader. Discovery is global + explicit only:
    when ``discover`` is True the global dir (``~/.tau/extensions`` unless
    overridden by ``user_dir``) is scanned; every explicit ``-e`` path is then
    appended. Paths are deduped by resolved path, first-wins. Each module is
    imported by file path and its module-level ``register(api)`` is invoked
    (awaited when async).

    Args:
        explicit_paths: Explicit ``-e`` extension paths. A failure here RAISES.
        discover: Whether to scan the global extensions dir. ``--no-extensions``
            (``-ne``) sets this False, which suppresses discovery while still
            loading ``explicit_paths``.
        user_dir: Override for the global extensions dir (tests inject a temp
            dir here). ``None`` means ``~/.tau/extensions``.
        api_factory: Produces a fresh ``ExtensionAPI`` per extension, keyed by the
            extension's file path (``Callable[[str], ExtensionAPI]``). Defaults to
            :func:`_standalone_api_factory` (see its note on the standalone status);
            a session-bound factory can instead bind each api to a live runner
            bucket keyed by that path (S24).
        collect_explicit_errors: When ``False`` (default) an explicit ``-e`` failure
            RAISES (Fail-Early — the user named it), which headless relies on to
            abort the run. When ``True`` an explicit failure is instead collected
            into ``result.errors`` — exactly like a discovered failure — and the
            loop keeps loading the rest, so the extensions that DID load stay bound
            and are returned. The TUI passes ``True`` (E5-loading split-brain fix,
            docs/EXTENSIONS-DEMO-ROADMAP.md): a launched Textual session can't
            cleanly abort mid-load, and dropping the partial result left
            ``/extensions`` empty while the good extensions' tools kept working.
        bus_available: Whether this load's session has a bus transport (H8,
            SIM_SPEC_v2 §16.10). Threaded to every :func:`_load_one_extension`
            call; an extension that declares ``TOUCHES_BUS = True`` is refused
            with :class:`ExtensionCapabilityError` when this is ``False``
            (default) — no bus wiring exists yet anywhere in this package, so
            the safe default is "no extension gets to claim it".

    Returns:
        ``LoadExtensionsResult`` with the loaded extensions and any load errors
        (discovered failures always; explicit failures too when
        ``collect_explicit_errors``).

    Raises:
        Exception: propagated from an explicit ``-e`` extension that fails to load
            (Fail-Early — the user named it), UNLESS ``collect_explicit_errors``.
    """
    if api_factory is None:
        api_factory = _standalone_api_factory

    result = LoadExtensionsResult()

    # Build the ordered (path, is_explicit) work list, deduped by resolved path.
    seen: set[str] = set()
    work: list[tuple[Path, bool]] = []

    def _add(path: Path, is_explicit: bool) -> None:
        resolved = str(path.expanduser().resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        work.append((path, is_explicit))

    if discover:
        discover_dir = user_dir if user_dir is not None else _GLOBAL_EXTENSIONS_DIR
        for p in _discover_extension_paths(discover_dir):
            _add(p, False)

    for raw in explicit_paths or []:
        _add(Path(raw), True)

    for path, is_explicit in work:
        try:
            loaded = await _load_one_extension(path, api_factory, bus_available=bus_available)
        except Exception as exc:
            if is_explicit and not collect_explicit_errors:
                # Fail-Early: the user named this path — surfacing it silently
                # is the anti-pattern, so re-raise. The TUI opts out via
                # collect_explicit_errors (it can't abort mid-load); headless
                # keeps this raise so an explicit failure aborts the run.
                raise
            # Discovered (or explicit under collect_explicit_errors): collect and
            # keep loading the rest. The error is RETURNED (never swallowed, and the
            # extensions that already loaded stay bound) for the caller to surface —
            # headless prints discovered errors to
            # stderr, the TUI shows a notice. The loader deliberately does NOT
            # print here: a stderr write during a live Textual render corrupts the
            # screen, and structured errors[] is the honest channel anyway (E5 §2.1).
            result.errors.append(ExtensionLoadError(path=str(path), error=str(exc)))
            continue
        result.extensions.append(loaded)

    return result


#: τ's default system prompt.
#:
#: Short on purpose. It buys context window on every single call, so each line
#: has to earn its place, and a model told to be terse by a wordy prompt has
#: already been shown which one to believe.
#:
#: The last line is where τ's voice lives. It is stated unconditionally and is
#: NOT wired to ``--fun``: that flag's entire blast radius is
#: ``tau_coding_agent.tagline`` by design, it belongs to the TUI rather than to
#: this package, and a joke that can change a turn's behaviour is not the same
#: kind of joke as a random tagline.
#: τ's default voice, and a worked example of the ``{{field}}`` slots
#: :func:`_build_system_prompt` fills. The two section slots at the bottom are in
#: the positions the builder would have appended them to anyway, so writing them
#: out changes nothing about the composition — it makes the composition legible
#: to anyone who copies this text into ``config.json`` as a starting point, and
#: movable by anyone who wants the tool list somewhere else.
BASE_SYSTEM_PROMPT = """\
You are Tau, a coding agent. Use tools to accomplish the user's goals. Write for \
a competent engineer who reads English as a second language. Optimize for \
one-pass comprehension, not for short word counts.

You are `{{model}}`, working in `{{cwd}}`. Every path you read, write, or run a \
command against is relative to that directory unless it says otherwise.

## Behavior

Be an effective, methodical developer. "Read files before editing", "describe \
how a problem occurs before you try to fix it", "admit mistakes and \
misunderstandings", and "guess what will happen before you try something to \
verify you're understanding properly" are the sort of rules you operate by. \
YAGNI, DRY, SOLID, KISS, and "can a junior developer follow this code while I'm \
on vacation" are important principles for you.

A follow-up question is preferrable to doing it over.

## Words

TL;DR: don't make stuff up.

Use the user's / project's vocabulary where it already exists. Variables, paths, \
and code objects should be referenced verbatim (that's coding for you - no style \
rules apply to source content being conveyed), but otherwise use simple language \
to refer to what's happening.

This is a technical conversation. We don't need variety. If there's a term for \
an object or topic, use that term for it (i.e. no nicknames).

When we build new stuff, we need to name it. For new modules, procedures, or \
subsystems: Let the user lead new terminology's creation and acceptance. You can \
suggest, but follow their lead.

Write accessibly, directly say what you mean, and no corporate jargon: \
"postpone", not "punt on". Write "internally", not "under the hood".

## Sentences

Active voice: "the scheduler writes the lock file", not "the lock file is \
written".

Keep sentences short.

## Voice

You're sharp and capable, a technical communicator. Helpful, but not in customer \
service. You can form and present technical opinions, to extend or refine the \
user's requests, but not argue with what the user goals are.

No warm-up. Don't open with "Great question", "You're absolutely right", or \
"Let's dive in". Get straight to it.

Don't end with a summary of what you just said. That's a summary of a summary, \
and neither of us needs it. If you're done, stop.

Say what you did and what it means, then stop. Don't narrate the steps. The user \
can watch the diff.

Examples of addressing mistakes:
- **My B.** I used the wrong flag (`-f` instead of `--force`). Retrying:
- **Whoops!** `..` is not the correct path.
- **Oof, yikes.** That did NOT work. Something different, then.
- **...aw man.** Traceback. I'll get to the root cause
- build failed 😅

Style: Sardonic, dry, brief.

{{project_context}}

{{tools}}"""


#: τ's agent directory — the home of the *global* context file, the one that
#: applies wherever τ is run from. pi's ``agentDir`` (``~/.pi``); τ's own
#: ``~/.tau``, the same directory ``_GLOBAL_EXTENSIONS_DIR`` lives under.
#: A string rather than a ``Path`` so tests (and a caller with a different
#: home) can pass their own, matching this module's existing convention.
_AGENT_DIR = "~/.tau"

#: The per-directory context-file candidates, in precedence order — pi
#: ``resource-loader.ts:72`` at ``5cd93f688``. A directory contributes **at most
#: one** file: the first of these that exists there wins, and the rest are not
#: read. ``AGENTS.override.md`` is first so a developer can shadow a checked-in
#: ``AGENTS.md`` without editing it.
CONTEXT_FILE_NAMES: tuple[str, ...] = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
)

#: τ's own context file, and deliberately **not** a member of
#: :data:`CONTEXT_FILE_NAMES`.
#:
#: Putting it in that tuple would make it *compete* with ``AGENTS.md`` under the
#: one-file-per-directory rule, so a project carrying both would silently lose
#: one — a regression for every τ user who already has this file, since τ has
#: always read it *alongside* ``AGENTS.md``. It is instead its own slot, read
#: from cwd only and appended last, i.e. as the most specific instruction in the
#: prompt. It is still a context file, so ``--no-context-files`` suppresses it.
TAU_SYSTEM_FILE = "SYSTEM.md"


@agent_facing(topic="sdk")
class ContextFileError(RuntimeError):
    """A context file was found but could not be used.

    Fail-Early: discovery deliberately *looked* for this file and located it, so
    a read failure (permissions, a truncated mount, non-UTF-8 bytes) is a real
    problem and not a reason to quietly prompt the model with less context than
    the user wrote. pi warns to stderr and continues here
    (``resource-loader.ts:82``); τ raises, and names the path plus the escape
    hatch, because a prompt silently missing its project instructions looks
    exactly like a model that ignored them.
    """


@agent_facing(topic="sdk")
@dataclass(frozen=True)
class ContextFile:
    """One discovered context file: its resolved path and its text."""

    path: Path
    content: str


def _read_context_file(path: Path) -> str:
    """Read one context file, or raise :class:`ContextFileError` naming it.

    ``utf-8-sig`` ports pi's ``stripBom`` (``resource-loader.ts:85``): it drops a
    leading BOM and is byte-identical to plain ``utf-8`` for everything else.
    Decoding is *strict* — the old ``errors="replace"`` turned a mis-encoded
    instruction file into U+FFFD soup that the model would have read as
    instructions.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ContextFileError(
            f"could not read context file {path}: {exc}. Fix it, or run with "
            f"--no-context-files to skip context discovery entirely."
        ) from exc
    except UnicodeDecodeError as exc:
        raise ContextFileError(
            f"context file {path} is not valid UTF-8: {exc}. Fix it, or run with "
            f"--no-context-files to skip context discovery entirely."
        ) from exc


def _find_context_file_in_dir(directory: Path) -> Path | None:
    """Return this directory's single context file, or ``None``.

    First match in :data:`CONTEXT_FILE_NAMES` wins (pi
    ``loadContextFileFromDir``, ``resource-loader.ts:71``). Only the *path* is
    returned: the shadowing check needs a name, not a body, and pi's version
    reads the file twice for that.

    ``is_file()`` follows symlinks and is False for a directory, so a stray
    ``CLAUDE.md/`` directory falls through to the next candidate rather than
    aborting the search (pi's ``statSync(...).isFile()`` ``continue``).
    """
    for name in CONTEXT_FILE_NAMES:
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # An unreadable *directory* is not a found file — `is_file()` on an
            # unstattable path is the question "is there one here", answered no.
            # A file we DID find and cannot READ still raises, in
            # `_read_context_file`. This is not a fallback: nothing is
            # substituted, the search simply continues.
            continue
    return None


def _find_git_paths(start: Path) -> tuple[Path, Path] | None:
    """Locate the repository containing ``start``: ``(repo_dir, common_git_dir)``.

    A port of pi's ``findGitPaths`` (``footer-data-provider.ts:16``), and only
    as much of it as :func:`_find_shadowed_context_file` needs. ``repo_dir`` is
    the directory holding ``.git``; ``common_git_dir`` is the *shared* git
    directory, which for a linked worktree resolves through its ``commondir``
    file back to the main repository's ``.git``. Returns ``None`` when there is
    no repository, or when what was found does not look like one.
    """
    directory = start
    while True:
        git_path = directory / ".git"
        try:
            if git_path.is_file():
                content = git_path.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir: "):
                    git_dir = (directory / content[len("gitdir: ") :].strip()).resolve()
                    if not (git_dir / "HEAD").exists():
                        return None
                    common_dir_file = git_dir / "commondir"
                    common_git_dir = (
                        (git_dir / common_dir_file.read_text(encoding="utf-8").strip()).resolve()
                        if common_dir_file.exists()
                        else git_dir
                    )
                    return (directory, common_git_dir)
            elif git_path.is_dir():
                if not (git_path / "HEAD").exists():
                    return None
                return (directory, git_path)
        except OSError:
            return None
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def _canonicalize(path: Path) -> Path:
    """``realpath`` without requiring the path to exist (pi ``canonicalizePath``)."""
    return path.resolve()


def _find_shadowed_context_file(cwd: Path) -> Path | None:
    """The main repo's context file that a *nested* linked worktree's own copy hides.

    A port of pi's ``findShadowedContextFile`` (``resource-loader.ts:101``).
    When cwd sits in a worktree created *inside* its own main repository —
    exactly this repo's ``.claude/worktrees/`` layout — the ancestor walk would
    otherwise reach both the worktree's ``CLAUDE.md`` and the main checkout's,
    and apply the same repository's instructions twice.

    It deliberately does nothing for an ordinary repo (the two dirs are the
    same), for a sibling worktree (``git worktree add ../feat``, whose main repo
    is not an ancestor), for a bare layout (``proj/.bare`` + ``proj/main``,
    where the dir holding the git dir tracks nothing), or for a submodule (whose
    git dir lands under ``.git/modules`` and has no ``commondir``).

    Paths are canonicalized because ``git worktree add`` writes the ``gitdir:``
    target as a realpath while cwd may still be reached through a symlink.
    """
    git_paths = _find_git_paths(cwd)
    if git_paths is None:
        return None
    worktree_root = _canonicalize(git_paths[0])
    common_git_dir = _canonicalize(git_paths[1])
    main_repo_root = common_git_dir.parent
    # Strictly *nested*: equal roots mean an ordinary repo, and a root that is
    # not below the main one means a sibling worktree. Neither shadows anything.
    if worktree_root == main_repo_root or not worktree_root.is_relative_to(main_repo_root):
        return None
    # The parent of the common git dir is the main worktree root only when that
    # directory is itself checked out from the same repository.
    if _canonicalize(main_repo_root / ".git") != common_git_dir:
        return None
    worktree_context_file = _find_context_file_in_dir(worktree_root)
    if worktree_context_file is None:
        return None
    return _canonicalize(main_repo_root / worktree_context_file.name)


@agent_facing(topic="sdk")
def load_project_context_files(
    cwd: str | Path | None = None,
    agent_dir: str | Path | None = None,
) -> list[ContextFile]:
    """Discover the context files that apply to ``cwd``, weakest first.

    A port of pi's ``loadProjectContextFiles`` (``resource-loader.ts:119`` at
    ``5cd93f688``), plus τ's own ``.tau/SYSTEM.md``. The returned order is the
    order they belong in a prompt — **general first, specific last** — because
    the nearest file is the one that should have the last word:

    1. ``~/.tau``'s context file (:data:`_AGENT_DIR`), the user's global one.
    2. Every ancestor of ``cwd`` that has one, **root-most first**, ending with
       ``cwd``'s own — so a repo's ``AGENTS.md`` is read after ``$HOME``'s and
       a package's after its monorepo's.
    3. ``cwd``'s ``.tau/SYSTEM.md``, τ's own slot (see :data:`TAU_SYSTEM_FILE`).

    Deduplicated by resolved path, so running τ from ``~`` does not load the
    same file as both the agent-dir entry and an ancestor.

    On walking all the way to ``/``: this is pi's behaviour and it is kept
    deliberately, because "put your standing instructions in ``~/AGENTS.md``" is
    a real workflow. The cost is a handful of ``stat`` calls per ancestor, once
    per session — not per turn. The *surprise* is answered by
    :func:`_build_system_prompt`, which labels every block with the absolute
    path it came from, so a prompt can never carry instructions whose origin it
    does not name; and by ``--no-context-files`` for a run that wants none.
    """
    resolved_cwd = _canonicalize(Path(cwd).expanduser() if cwd else Path(os.getcwd()))
    resolved_agent_dir = _canonicalize(Path(agent_dir or _AGENT_DIR).expanduser())

    context_files: list[ContextFile] = []
    seen: set[Path] = set()

    def _take(path: Path) -> ContextFile | None:
        resolved = _canonicalize(path)
        if resolved in seen:
            return None
        seen.add(resolved)
        return ContextFile(path=resolved, content=_read_context_file(resolved))

    global_context = _find_context_file_in_dir(resolved_agent_dir)
    if global_context is not None:
        taken = _take(global_context)
        if taken is not None:
            context_files.append(taken)

    shadowed = _find_shadowed_context_file(resolved_cwd)
    ancestors: list[ContextFile] = []
    directory = resolved_cwd
    while True:
        found = _find_context_file_in_dir(directory)
        if found is not None and _canonicalize(found) != shadowed:
            taken = _take(found)
            if taken is not None:
                # Root-most first: each ancestor goes in FRONT of the ones
                # already collected, so the nearest file ends up last.
                ancestors.insert(0, taken)
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    context_files.extend(ancestors)

    # τ's own slot, last: the most specific instruction in the prompt.
    tau_system = resolved_cwd / ".tau" / TAU_SYSTEM_FILE
    if tau_system.is_file():
        taken = _take(tau_system)
        if taken is not None:
            context_files.append(taken)

    return context_files


@agent_facing(topic="sdk")
def append_system_prompt(base: str, sections: list[str] | None) -> str:
    """Append ``--append-system-prompt`` sections to a base system prompt.

    Sections augment rather than replace the base (pi ``appendSystemPrompt``,
    system-prompt.ts:48), joined by blank lines. An empty/absent list returns the
    base unchanged; an empty base with sections yields just the sections.

    Lives here, beside :func:`_build_system_prompt`, because it composes the
    *base text* slot and nothing else — the appended sections land ahead of the
    project context and the tool list, not after them. It was previously a
    private helper in ``tau_coding_agent.headless`` that three frontends reached
    across for; ``headless`` re-exports this name so those imports keep working.
    """
    if not sections:
        return base
    parts = [base, *sections] if base else list(sections)
    return "\n\n".join(parts)


@agent_facing(topic="sdk")
class SystemPromptFieldError(ValueError):
    """A ``{{field}}`` in a system prompt names something τ cannot supply.

    Raised rather than rendered literally. A misspelled ``{{tols}}`` left in the
    text would reach the model as the four characters it is, say nothing, and
    look exactly like a prompt that worked — the silent failure the whole
    "loud failure over a quiet guess" rule exists to prevent.
    """


#: A system-prompt placeholder. Deliberately narrow — lowercase name, optional
#: inner spaces — so ordinary prose using braces (JSON examples, f-string
#: snippets, ``{{ anything With Caps }}``) passes through untouched and only a
#: thing that really looks like a field is held to the field list.
_PROMPT_FIELD = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

#: A placeholder that is the whole line. Such a line is a SECTION slot, and when
#: the section is empty the line — and the blank line separating it from what
#: came before — would otherwise survive as vertical whitespace in the prompt.
_LONE_PROMPT_FIELD_LINE = re.compile(r"^[ \t]*\{\{\s*([a-z][a-z0-9_]*)\s*\}\}[ \t]*$")


def _drop_empty_field_lines(template: str, fields: dict[str, str]) -> str:
    """Remove section slots that expand to nothing, with their leading blank line.

    A run with no ``AGENTS.md`` must not read as a prompt with a hole in it. Only
    a line that is *nothing but* a placeholder is eligible, and only when the
    field is known AND empty — an unknown field is left in place so
    :func:`_render_prompt_fields` still raises on it rather than being silently
    tidied away.
    """
    kept: list[str] = []
    for line in template.split("\n"):
        match = _LONE_PROMPT_FIELD_LINE.match(line)
        if match is not None and fields.get(match.group(1)) == "":
            if kept and kept[-1] == "":
                kept.pop()
            continue
        kept.append(line)
    return "\n".join(kept)


def _render_prompt_fields(template: str, fields: dict[str, str]) -> tuple[str, set[str]]:
    """Substitute ``{{field}}`` placeholders, returning the text and which were used.

    Substitution runs on the TEMPLATE ONLY, never on the assembled prompt: a
    project's ``AGENTS.md`` is user content, and a ``{{...}}`` inside it must
    reach the model as written rather than being rewritten by whatever field
    happens to share its name.

    Raises:
        SystemPromptFieldError: on a placeholder naming no known field.
    """
    template = _drop_empty_field_lines(template, fields)
    used: set[str] = set()

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in fields:
            known = ", ".join(f"{{{{{k}}}}}" for k in sorted(fields))
            extra = ""
            if name == "base_prompt":
                extra = (
                    " ``{{base_prompt}}`` is available only in a prompt that REPLACES "
                    "the base text; the base text cannot embed itself."
                )
            raise SystemPromptFieldError(
                f"system prompt names an unknown field {{{{{name}}}}}. Known fields: {known}.{extra}"
            )
        used.add(name)
        return fields[name]

    return _PROMPT_FIELD.sub(_substitute, template), used


def _render_project_context(cwd: str, agent_dir: str | Path | None) -> str:
    """The ``<project_context>`` block, or ``""`` when no context file applies.

    The ``path=`` attribute is not decoration: it is the only thing that makes an
    ancestor walk reaching ``/`` honest, since the prompt then names every file it
    is carrying (pi system-prompt.ts:54-61).
    """
    context_files = load_project_context_files(cwd, agent_dir=agent_dir)
    if not context_files:
        return ""
    lines = ["<project_context>", "Project-specific instructions and guidelines:"]
    for context_file in context_files:
        lines.append("")
        lines.append(f'<project_instructions path="{context_file.path}">')
        lines.append(context_file.content.rstrip("\n"))
        lines.append("</project_instructions>")
    lines.append("</project_context>")
    return "\n".join(lines)


def _render_tool_list(tools: list[AgentTool] | None) -> str:
    """The ``Available tools:`` block, or ``""`` when the model is offered none.

    One shape, one branch (B1): the former ``hasattr(tool, "definition")`` fork
    existed only because the built-ins arrived as raw classes. Its raw arm
    computed ``f"{tool.name}: {tool.label}"``, which ``_resolve_tools`` now writes
    into ``prompt_snippet`` at construction — so this renders byte-identically to
    the two-branch version for both tool sources.
    """
    if not tools:
        return ""
    lines = ["---", "Available tools:"]
    for tool in tools:
        snippet = tool.definition.prompt_snippet
        guidelines = tool.definition.prompt_guidelines

        if snippet:
            lines.append(f"- {snippet}")
        if guidelines:
            for guideline in guidelines:
                lines.append(f"  - {guideline}")
    return "\n".join(lines)


def _build_system_prompt(
    cwd: str | None = None,
    tools: list[AgentTool] | None = None,
    custom_prompt: str | None = None,
    no_context_files: bool = False,
    agent_dir: str | Path | None = None,
    model: str | None = None,
) -> str:
    """Build the system prompt from a base prompt, context files and tools.

    The system prompt is built from:

    1. ``custom_prompt`` if given, else :data:`BASE_SYSTEM_PROMPT`.
    2. Every context file :func:`load_project_context_files` discovers, unless
       ``no_context_files``.
    3. Tool definitions for ``prompt_snippet`` and ``prompt_guidelines``.

    **Context files COMPOSE with (2 follows 1); they never replace it and are
    never replaced by it.** This is the whole point of the change: a caller who
    sets ``system_prompt`` used to silently turn project context off, and
    nothing said so. pi does the same thing — ``buildSystemPrompt``
    (``system-prompt.ts:46-62``) appends ``<project_context>`` to a
    ``customPrompt`` exactly as it does to its own base text.

    **Placeholders.** The base text — τ's own, or a ``custom_prompt`` — may name
    fields as ``{{field}}``:

    ==================== =========================================================
    ``{{base_prompt}}``  :data:`BASE_SYSTEM_PROMPT`, already rendered. Lets a
                         custom prompt WRAP τ's voice instead of replacing it.
                         Available only in a ``custom_prompt``.
    ``{{project_context}}`` the ``<project_context>`` block.
    ``{{tools}}``        the ``Available tools:`` block.
    ``{{tool_names}}``   just the names, comma-separated, for a one-line mention.
    ``{{cwd}}``          the working directory the tools run in.
    ``{{model}}``        the model id going on the wire, when the caller knows it.
    ==================== =========================================================

    Naming ``{{project_context}}`` or ``{{tools}}`` MOVES that section to where
    the placeholder is; it is not also appended, or the prompt would carry it
    twice. A template naming neither composes exactly as it always did, so this
    is invisible to any prompt that does not ask for it. ``{{tool_names}}`` is a
    mention, not the section — using it does not suppress the block.

    An unknown field raises rather than rendering literally; see
    :class:`SystemPromptFieldError` for why that is not a convenience.

    Args:
        cwd: Directory the discovery walk starts from. Defaults to ``os.getcwd()``.
        tools: List of :class:`AgentTool` (one shape — see :func:`_resolve_tools`).
        custom_prompt: Replaces the base prompt only. Context files still load.
        no_context_files: Skip discovery entirely (``--no-context-files``/``-nc``).
        agent_dir: Override the ``~/.tau`` global-context directory.
        model: Model id for ``{{model}}``. Empty when the caller does not know it.

    Returns:
        Complete system prompt string.

    Raises:
        SystemPromptFieldError: on a ``{{field}}`` naming no known field.
    """
    cwd = cwd or os.getcwd()

    # Both movable sections are rendered up front, because a placeholder decides
    # only WHERE they go — not whether they are built.
    context_block = "" if no_context_files else _render_project_context(cwd, agent_dir)
    tools_block = _render_tool_list(tools)

    fields = {
        "project_context": context_block,
        "tools": tools_block,
        "tool_names": ", ".join(tool.name for tool in (tools or [])),
        "cwd": cwd,
        "model": model or "",
    }

    # τ's default voice, and the ONLY copy of it — the shipped
    # tau_default_config.json deliberately carries no ``system_prompt`` key, so a
    # default install reaches this text and its context files instead of a config
    # string that would shadow both. Rendered first so ``{{base_prompt}}`` hands a
    # custom prompt the finished text rather than a template it cannot expand.
    base_text, base_used = _render_prompt_fields(BASE_SYSTEM_PROMPT, fields)

    # A user who sets ``system_prompt`` is overriding on purpose — and overrides
    # exactly this much: the sections below still compose around it.
    if custom_prompt:
        text, used = _render_prompt_fields(custom_prompt, {**fields, "base_prompt": base_text})
        if "base_prompt" in used:
            # The base text was inlined, so whatever IT placed counts as placed.
            used |= base_used
    else:
        text, used = base_text, base_used

    lines = [text]
    if context_block and "project_context" not in used:
        lines.append("")
        lines.append(context_block)
    if tools_block and "tools" not in used:
        lines.append("")
        lines.append(tools_block)

    return "\n".join(lines)


@agent_facing(topic="sdk")
def create_agent_session(
    model: str | Model = "gpt-4o",
    provider: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    tools: list[str] | None = None,
    session_log: SessionLog | None = None,
    extensions: list[Callable] | None = None,
    system_prompt: str | None = None,
    no_context_files: bool = False,
    thinking_level: str = "off",
    cwd: str | None = None,
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel",
    compaction_policy: CompactionPolicy | None = None,
    bus_available: bool = False,
    no_tools: Literal["all", "builtin"] | None = None,
    max_turns: int | None = None,
    tool_options: dict[str, dict[str, Any]] | None = None,
) -> AgentSession:
    """Create an AgentSession with all defaults.

    This is the main SDK entry point. It handles:
    - Model resolution (string → Model object)
    - Tool discovery (string names → AgentTool objects)
    - Extension registration (inline factory callables invoked at construction)
    - System prompt building (base prompt + discovered context files + tools)

    It does NOT load ``~/.tau/settings.json``. Nothing in any package reads
    ``Settings`` outside tests; every value this factory uses is either passed
    in by the caller or defaulted here.

    There is deliberately **no** ``settings`` parameter (B5). One was accepted here
    and never read — a caller passing ``settings={...}`` got a session that ignored
    it and looked fine, which is the same "declared and not consulted" failure H1
    fixed for ``execution_mode``, and pi's agent package has no ``settings`` concept
    to port from. It is removed rather than made to raise, because a parameter whose
    only behaviour is rejection still advertises a capability that does not exist;
    an unexpected-keyword ``TypeError`` says the true thing. Deciding what such a
    dict would *mean* — how it composes with the ``~/.tau/config.json`` the CLI and
    TUI actually read — is the config-precedence design question (B2) and is not
    settled here.

    Args:
        model: Model identifier string or Model object (default: "gpt-4o").
        provider: Provider name for model resolution (default: "openai").
        base_url: Optional custom API base URL.
        api_key: Optional API key.
        tools: List of BUILT-IN tool name strings (e.g., ["read", "bash"]),
            resolved by :func:`_resolve_tools`, which raises ``ValueError`` on any
            name it does not recognize (NODE-ADDRESSABLE-AGENTS.md §5/W5). This
            parameter does not accept :class:`~tau_agent_core.tools.base.AgentTool`
            instances and gains no fallback that does — a custom tool must go
            through the :class:`~tau_agent_core.agent_session.AgentSession`
            constructor's ``tools=`` directly, or be registered by an extension
            (``ExtensionAPI`` tool registration, loaded via ``extensions=``/
            ``load_extensions``). Passing a non-empty list together with
            ``no_tools`` raises — see that parameter.
        session_log: Optional SessionLog to persist through (the coding-agent's
            file Session on the live path). Defaults to an in-memory log.
        extensions: List of extension factory callables.
        system_prompt: Optional custom system prompt. Replaces the base prompt
            ONLY — project context files still load and are appended after it.
            Pass ``no_context_files=True`` to suppress those as well.
        no_context_files: Skip context-file discovery entirely
            (``--no-context-files``/``-nc``). The prompt is then the base (or
            custom) text plus the tool list, and nothing read from disk.
        thinking_level: Thinking level ("off", "minimal", "low", "medium",
            "high", "xhigh"). A non-"off" level marks the model reasoning-capable
            and is forwarded to the provider as `reasoning_effort`.
        cwd: Current working directory.
        tool_execution_mode: Batch-level tool execution policy ("sequential" or
            "parallel", default "parallel") forwarded to AgentSession, which
            threads it into every AgentLoopConfig the session builds. A tool
            declaring a per-tool "sequential" execution_mode still forces its
            batch to run sequentially regardless of this setting.
        compaction_policy: Optional declared
            :class:`~tau_agent_core.compaction_policy.CompactionPolicy` (H5,
            SIM_SPEC_v2 §16.8). ``None`` — the default — is the shipped behaviour:
            auto-compaction on, summarising through this session's own model. A
            MEASUREMENT run declares one, because a compaction is a model call at
            the tail of a prompt and an undeclared one lands inside §5.2's headline
            latency number and on the far side of §11.1's partition. Declaring a
            policy never makes compaction quieter — it adds construction-time and
            runtime checks that raise.
        bus_available: Whether this session has a bus transport a loaded
            extension may declare against (H8, SIM_SPEC_v2 §16.10). ``False``
            — the default — refuses to load any file extension that declares
            ``TOUCHES_BUS = True`` (see :func:`_load_one_extension`); no NATS
            wiring exists in this package yet (tau-007), so there is nothing
            to back that capability with until a caller sets this ``True``.
        no_tools: Tool-suppression policy, the SDK equivalent of the CLI's
            ``--no-tools``/``-nt`` and ``--no-builtin-tools``/``-nbt``. One
            resolved value rather than two booleans, for the reason
            ``headless.resolve_no_tools`` gives: flags that only have meaning
            against each other become the same flag once each consumer re-derives
            the interaction.

            - ``"all"`` — the model is offered nothing: no built-ins, and no
              extension-registered tools either. Extensions still LOAD; hooks,
              event subscriptions, slash commands and message injections are
              untouched. Only callable tools are withheld.
            - ``"builtin"`` — built-ins only. Extension-registered tools survive
              and are offered.
            - ``None`` — the default — no suppression.

            Either value resolves zero built-ins here, which is what gives
            ``"builtin"`` behaviour inside this package rather than leaving it a
            display label that only the coding-agent's argv boundary honoured.
            An unrecognised value raises in ``AgentSession``.
        max_turns: Stop the loop after this many LLM calls. ``None`` — the default
            — is no ceiling, which is also ``AgentLoopConfig``'s default and pi's
            behaviour. This factory did not take the parameter at all until now,
            and neither did any CLI flag or config key, so the ceiling that did
            exist (a hardcoded 50) was unreachable from every caller τ ships. What
            bounds a runaway run without one is the abort signal: an extension's
            budget guard, or Escape in the TUI.
        tool_options: Keyword arguments for individual built-in tools, keyed by
            tool name — e.g. ``{"read": {"max_image_dimension": 2000}}``. Ignored
            for tools not in ``tools``, and irrelevant when ``no_tools`` is set,
            since then no built-in is constructed at all. See
            :func:`_resolve_tools`.

    Raises:
        ValueError: if ``no_tools`` is given together with a non-empty ``tools``
            (a contradictory request, refused rather than silently resolved), or
            if ``tools`` names a tool that is not a built-in (:func:`_resolve_tools`).

    Returns:
        Fully configured AgentSession instance.

    Example:
        >>> session = create_agent_session(model="gpt-4o", tools=["read", "bash"])
        >>> messages = await session.prompt("Hello, world!")
    """
    # 1. Resolve model
    if isinstance(model, str):
        model = resolve_model(model, provider=provider, base_url=base_url)

    # A non-"off" thinking level asserts the model is reasoning-capable (pi
    # model-resolver.ts:496 sets `reasoning: true` on an ad-hoc model when a
    # non-off level is requested). Without this the provider would clamp the
    # level to "off" and never send `reasoning_effort`.
    reasoning_arg = thinking_level if thinking_level != "off" else None
    if reasoning_arg is not None:
        model.reasoning = True

    # 2. Discover and create tools
    #
    # ``no_tools`` with a non-empty ``tools`` is a CONTRADICTORY request — "offer
    # `read`" and "offer no built-ins" — so it is refused rather than settled by
    # precedence. Neither parameter outranks the other at a call site, and picking a
    # silent winner is how a caller ends up with a session that quietly ignores half
    # of what it asked for. The CLI boundary never has to answer this because argv
    # cannot express both: ``--tools`` and ``--no-tools`` each write
    # ``config["tools"]``, and ``headless.resolve_no_tools`` collapses the flags
    # before ``backends.resolve_tool_names`` reads the result.
    #
    # ``tools=None`` (the default) and ``tools=[]`` both stay legal. Neither asks for
    # a built-in, so neither contradicts a suppression — and ``no_tools="all"`` is
    # MEANINGFUL on top of them, because it also withholds extension-registered
    # tools, which ``tools=`` cannot speak about at all.
    if no_tools is not None and tools:
        raise ValueError(
            f"create_agent_session() got tools={tools!r} together with "
            f"no_tools={no_tools!r}. Those ask for opposite things: a built-in tool, "
            "and no built-in tools. Drop `tools=` to suppress the built-ins, or drop "
            "`no_tools=` to offer them."
        )
    #
    # The empty list is set HERE rather than left to the caller, because
    # ``AgentSession._build_turn_tools`` documents the invariant it relies on — both
    # policies "arrive here with ``self._tools == []``" — and reads only
    # ``no_tools == "all"`` itself. Until this factory took the parameter, that
    # emptying existed solely at the coding-agent's argv boundary, so an SDK caller
    # who forwarded ``no_tools="builtin"`` alone got a label with no behaviour behind
    # it. The raise above already guarantees ``tools`` is falsy on this path; assigning
    # the empty list states the invariant rather than inferring it from that.
    #
    # An INVALID ``no_tools`` value is not checked here. ``AgentSession.__init__``
    # raises on one, and it stays the single validator — a second copy of the literal
    # list is a second thing to keep current.
    tool_objs: list[AgentTool] = [] if no_tools is not None else _resolve_tools(tools, tool_options)

    # 3. Extensions: inline factory callables are invoked by AgentSession at
    #    construction (pi's loadExtensionFromFactory analog). File-path discovery
    #    + loading is handled by the single async loader (_load_extensions),
    #    wired into the CLI/headless run path (E0/S2).
    ext_factories = list(extensions) if extensions else []

    # 4. Build system prompt. ``system_prompt`` is passed IN rather than short-
    #    circuiting the builder: it replaces the base text and nothing else, so a
    #    caller who sets it still gets the project's context files instead of
    #    silently turning discovery off (see :func:`_build_system_prompt`).
    sys_prompt = _build_system_prompt(
        cwd,
        tool_objs,
        custom_prompt=system_prompt,
        no_context_files=no_context_files,
        # ``{{model}}`` gets the id that goes on the wire, which after resolution
        # is ``Model.id`` — not the string the caller passed, which may have been
        # a config-entry name or a ``provider/id`` shorthand.
        model=model.id,
    )

    # 5. Default to an in-memory session log when the caller injects none. The
    #    live paths (TUI/headless) inject the coding-agent's file Session; the
    #    SDK default persists in RAM only (§2.6, Decision 4 option B).
    if session_log is None:
        session_log = InMemorySessionLog()

    # 6. Create and return AgentSession
    return AgentSession(
        session_log=session_log,
        model=model,
        system_prompt=sys_prompt,
        tools=tool_objs,
        extensions=ext_factories,
        api_key=api_key,
        reasoning=reasoning_arg,
        compaction_policy=compaction_policy,
        tool_execution_mode=tool_execution_mode,
        bus_available=bus_available,
        no_tools=no_tools,
        max_turns=max_turns,
    )
