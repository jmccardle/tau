"""τ-agent-core sdk: SDK entry point — create_agent_session().

Reference: PHASE-2-SUBPHASE-4.md — Agent Session and SDK Entry Point.
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.
Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (SDK default = InMemorySessionLog).

This module provides:
- create_agent_session(): Main SDK entry point for creating fully configured sessions.
- _resolve_model(): Resolve model string to Model object.
- _resolve_tools(): Discover and create tool objects from string names.
- plan_extensions(): what loads, decided from metadata and the authorization store.
- _load_extensions(): THE single extension loader — plan + import + register(api).
- _build_system_prompt(): Build system prompt from context files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal

from tau_llm.providers import get_provider_spec, registered_providers
from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.compaction_policy import CompactionPolicy
from tau_agent_core import extension_sources
from tau_agent_core.extension_auth import AuthorizationStore, store_path_for
from tau_agent_core.extension_sources import (
    ExtensionRecord,
    ExtensionSource,
    extension_identifier,
)
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.prompt_templates import load_prompt_templates
from tau_agent_core.session_log import InMemorySessionLog, SessionLog
from tau_agent_core.tau_home import resolve_tau_home
from tau_agent_core.tool_schema import output_schema_of
from tau_agent_core.skills import Skill, load_skills, render_skills_block
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

#: Output cap for a model whose config or id states none (docs/TRUNCATED-TOOL-CALLS.md §7).
DEFAULT_MAX_TOKENS = 32 * 1024

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

    A built-in id (``"gpt-4o"``) returns a copy of its default. Any other id gets
    ``provider``'s registered wire protocol and endpoint, with ``base_url``
    taking precedence over the endpoint.

    Args:
        model: Model identifier (e.g., "gpt-4o").
        provider: Registered vendor id (default: "openai").
        base_url: Optional custom API base URL.

    Returns:
        A new Model; the shared built-in defaults are never mutated. A
        non-built-in id gets ``context_window=128000`` and ``DEFAULT_MAX_TOKENS``,
        and speaks ``openai-completions`` when ``provider`` is unregistered.

    Raises:
        ValueError: No ``base_url`` and ``provider`` is not registered with a
            default endpoint (docs/EXTENSION-PROVIDERS.md §2).
    """
    if model in _DEFAULT_MODELS:
        default = _DEFAULT_MODELS[model]
        return default.model_copy(update={"base_url": base_url} if base_url else {})
    spec = get_provider_spec(provider)
    endpoint = base_url or (spec.base_url if spec else None)
    if not endpoint:
        raise ValueError(
            f"resolve_model({model!r}): provider {provider!r} is not registered with a "
            f"default endpoint and no base_url was given. Registered providers: "
            f"{', '.join(registered_providers())}."
        )
    return Model(
        id=model,
        name=model,
        api=spec.api if spec else "openai-completions",
        provider=provider,
        base_url=endpoint,
        context_window=128000,
        max_tokens=DEFAULT_MAX_TOKENS,
    )


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
    cwd: str | None = None,
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
        cwd: The directory every built-in resolves paths and runs commands in;
            ``None`` leaves each at the process cwd. A ``tool_options`` entry
            naming ``cwd`` wins for that tool.

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

        kwargs: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
        kwargs.update(options.get(name, {}))
        tool_obj = _BUILTIN_TOOL_CLASSES[name](**kwargs)
        typed_run = getattr(tool_obj, "run", None)
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
                    namespace="core",
                    output_schema=None if typed_run is None else output_schema_of(typed_run),
                )
            )
        )

    return tool_objs


_ext_load_counter = 0

_TOUCHES_BUS_ATTR = "TOUCHES_BUS"
_SUBJECTS_ATTR = "SUBJECTS"
_CONFIG_SCHEMA_ATTR = "CONFIG_SCHEMA"


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
    - the module declares ``CONFIG_SCHEMA`` and it is not a valid form spec.
      Same discipline: a schema nobody can render is a load error, not a
      settings screen that comes up empty.
    """


@agent_facing(topic="extensions")
class ExtensionStartupError(RuntimeError):
    """An extension named with ``-e`` failed to load or to start (#47).

    Being named is a clear signal the extension is intended
    (docs/EXTENSION-PACKAGES.md §12), so its failure stops startup instead of
    warning. The original exception is ``__cause__``.

    Attributes:
        identifier: The extension's identifier.
        label: Its path or module name.
        phase: ``"load"`` (import or ``register``) or ``"session_start"``.
    """

    def __init__(self, identifier: str, label: str, phase: str, error: str) -> None:
        """Name the extension, the phase and the error in the message."""
        where = label if label == identifier else f"{identifier} ({label})"
        super().__init__(f"extension {where}, named with -e, failed at {phase}: {error}")
        self.identifier = identifier
        self.label = label
        self.phase = phase


@agent_facing(topic="extensions")
@dataclass
class LoadedExtension:
    """A successfully loaded extension.

    Narrowed port of pi's ``Extension`` record (coding-agent types.ts:1577).
    The hash and bus fields are H7/H8's addition (SIM_SPEC_v2 §16.6/§16.10).

    Attributes:
        path: Its label: the source file, or the module name for a package.
        register: The module-level ``register`` function that was invoked.
        api: The ``ExtensionAPI`` it registered against.
        content_hash: Hex sha256 of the exact bytes compiled, so two loads of
            one path at different contents are never mistaken for one
            condition.
        subjects: The bus subjects the module's ``SUBJECTS`` declared; ``()``
            unless ``touches_bus``.
        touches_bus: The module's ``TOUCHES_BUS`` declaration.
        config_schema: The module's ``CONFIG_SCHEMA`` normalized by
            :func:`~tau_agent_core.extension_types.validate_form_spec` to
            ``{"title", "fields"}``, so a head can render a settings screen.
            ``None`` if the module declared none.
        source: Where it came from (docs/EXTENSION-PACKAGES.md §2); ``None``
            for a record built outside the loader, read as a file path.
        explicit: Whether it was named with ``-e``, which authorizes it (§5).
    """

    path: str
    register: Callable[..., Any]
    api: ExtensionAPI
    content_hash: str = ""
    subjects: tuple[str, ...] = ()
    touches_bus: bool = False
    config_schema: dict[str, Any] | None = None
    source: ExtensionSource | None = None
    explicit: bool = False


@agent_facing(topic="extensions")
@dataclass
class ExtensionLoadError:
    """An extension that failed to load (pi types.ts:1590 errors[]).

    Attributes:
        path: Its label: the file path, or the module name for a package.
        error: The exception's message.
        explicit: Whether it was named with ``-e`` (docs/EXTENSION-PACKAGES.md §5).
        source: Where it came from, when the loader knew.
    """

    path: str
    error: str
    explicit: bool = False
    source: ExtensionSource | None = None


@agent_facing(topic="extensions")
@dataclass
class LoadExtensionsResult:
    """Result of loading extensions — port of pi ``LoadExtensionsResult``.

    Reference: pi agent/../types.ts:1590. The ``runtime`` field is intentionally
    omitted until the API is bound to the live session (E1/S3).

    Attributes:
        extensions: The extensions that loaded, in load order.
        errors: The extensions that failed to load and were collected rather
            than raised.
        withheld: Extensions found and not loaded: ``unauthorized``,
            ``unusable`` or ``collision`` (docs/EXTENSION-PACKAGES.md §5).
        grandfathered: Drop-ins authorized because this was the first start
            with no authorization store; a head announces them once.
    """

    extensions: list[LoadedExtension] = field(default_factory=list)
    errors: list[ExtensionLoadError] = field(default_factory=list)
    withheld: list[ExtensionRecord] = field(default_factory=list)
    grandfathered: list[str] = field(default_factory=list)


@agent_facing(topic="extensions")
@dataclass
class ExtensionInfo:
    """Read-only summary of one loaded extension for the ``/extensions`` surface.

    Reference: EXTENSIONS-E5-WIRING.md §5 (E5.4 / S34); shortcuts E10 §6 / S69.

    Attributes:
        name: The identifier (docs/EXTENSION-PACKAGES.md §2).
        path: The source file.
        tools: Names of the tools it registered.
        commands: Names of the commands it registered.
        shortcuts: The shortcuts it registered.
        hooks: The hook events it handles, sorted.
        content_hash: Hex sha256 of the source at load time (H7, SIM_SPEC_v2
            §16.6); :func:`~tau_agent_core.run_manifest.build_run_manifest`
            writes it, with ``subjects``, into ``manifest.json``.
        subjects: Its declared bus subjects; ``()`` if it does not touch the bus.
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
    """Summarize each loaded extension for the ``/extensions`` listing.

    Reference: EXTENSIONS-E5-WIRING.md §5 (E5.4 / S34). Registrations are read
    from each extension's runner bucket
    (:class:`~tau_agent_core.extensions.runner.ExtensionHandlers`), the only
    record of which extension registered what.

    Args:
        result: The load result. Its ``errors`` are not summarized; the caller
            surfaces them alongside.

    Returns:
        One :class:`ExtensionInfo` per loaded extension, in load order.

    Raises:
        RuntimeError: An extension's api has no runner bucket, a construction
            bug (Fail-Early, not an empty listing).
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
                name=extension_identifier(ext.path),
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


def _register_module(
    module: Any,
    label: str,
    api_factory: Callable[[str], ExtensionAPI],
    *,
    bus_available: bool,
    forget: Callable[[], object],
) -> tuple[Callable[..., Any], ExtensionAPI, tuple[str, ...], bool, dict[str, Any] | None]:
    """Check an imported extension module's declarations, then bind and call ``register``.

    H7/H8 (SIM_SPEC_v2 §16.6/§16.10) and ``CONFIG_SCHEMA`` are read before
    ``register`` is looked up, because they are set at import time: a bus
    declaration without ``SUBJECTS``, a bus declaration in a session with no
    bus, and an invalid schema each raise :class:`ExtensionCapabilityError`,
    and ``register`` never runs.

    Args:
        module: The imported module.
        label: The bucket label it binds under.
        api_factory: Produces the bucket-bound api for ``label``.
        bus_available: Whether the session has a bus transport.
        forget: Undoes the import on a refusal (a file's private module only).

    Returns:
        ``(register, api, subjects, touches_bus, config_schema)``.
    """
    touches_bus = bool(getattr(module, _TOUCHES_BUS_ATTR, False))
    raw_subjects = getattr(module, _SUBJECTS_ATTR, ())
    subjects: tuple[str, ...] = ()
    if touches_bus:
        subjects = tuple(raw_subjects) if isinstance(raw_subjects, (list, tuple)) else ()
        if not subjects or not all(isinstance(s, str) and s for s in subjects):
            forget()
            raise ExtensionCapabilityError(
                f"{label} sets {_TOUCHES_BUS_ATTR} = True but {_SUBJECTS_ATTR} is "
                f"{raw_subjects!r}, not a non-empty sequence of non-empty subject "
                "strings. An extension that touches the bus must declare which "
                "subjects (H7) — 'leave it unset' is not admissible."
            )
        if not bus_available:
            forget()
            raise ExtensionCapabilityError(
                f"{label} declares {_TOUCHES_BUS_ATTR} = True with subjects "
                f"{subjects!r}, but this session has no bus transport "
                "(bus_available=False). Refusing to load rather than admitting an "
                "extension whose declared capability the session cannot back (H8). "
                "Pass --bus, and install the [bus] extra for nats-py."
            )

    raw_schema = getattr(module, _CONFIG_SCHEMA_ATTR, None)
    config_schema: dict[str, Any] | None = None
    if raw_schema is not None:
        from tau_agent_core.extension_types import validate_form_spec

        try:
            title, fields = validate_form_spec(raw_schema)
        except ValueError as err:
            forget()
            raise ExtensionCapabilityError(
                f"{label} declares {_CONFIG_SCHEMA_ATTR} but it is not a valid form "
                f"spec: {err}. A schema a head cannot render is a load error, not a "
                "settings screen that silently comes up empty."
            ) from err
        config_schema = {"title": title, "fields": fields}

    register = getattr(module, "register", None)
    if register is None:
        forget()
        raise AttributeError(f"{label} has no register(api) function")
    if not callable(register):
        forget()
        raise TypeError(f"{label} register is not callable")
    return register, api_factory(label), subjects, touches_bus, config_schema


async def _load_one_extension(
    path: Path,
    api_factory: Callable[[str], ExtensionAPI],
    *,
    bus_available: bool = False,
    source: ExtensionSource | None = None,
) -> LoadedExtension:
    """Import one file extension under a private name and invoke its ``register(api)``.

    The identifier is the stem verbatim and the import name is private
    (docs/EXTENSION-PACKAGES.md §2.1). A package directory gets
    ``submodule_search_locations``, so ``from .helper import x`` works.

    The module's own directory is on ``sys.path`` for the ``exec`` only, so a
    single-file extension can ``import helper`` beside it. The window holds no
    ``await``, so no other load can splice its directory in; an import deferred
    into ``register`` or a hook still fails loudly.

    Raises on any failure (missing file, missing or non-callable ``register``,
    an unmet H7/H8 declaration, an invalid ``CONFIG_SCHEMA``, or an exception
    raised by ``register``); the caller applies the explicit-vs-discovered
    error policy.
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
    safe_stem = re.sub(r"\W", "_", extension_identifier(str(path)))
    module_name = f"_tau_ext_{safe_stem}_{_ext_load_counter}"
    spec = importlib.util.spec_from_file_location(
        module_name, module_file, submodule_search_locations=submodule_search
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for extension {module_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sibling_dir = str(module_file.parent)
    borrowed = sibling_dir not in sys.path
    if borrowed:
        sys.path.insert(0, sibling_dir)
    try:
        source_bytes = module_file.read_bytes()
        code = compile(source_bytes, str(module_file), "exec")
        exec(code, module.__dict__)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if borrowed and sibling_dir in sys.path:
            sys.path.remove(sibling_dir)

    register, api, subjects, touches_bus, config_schema = _register_module(
        module,
        str(path),
        api_factory,
        bus_available=bus_available,
        forget=lambda: sys.modules.pop(module_name, None),
    )
    outcome = register(api)
    if inspect.isawaitable(outcome):
        await outcome

    return LoadedExtension(
        path=str(path),
        register=register,
        api=api,
        content_hash=hashlib.sha256(source_bytes).hexdigest(),
        subjects=subjects,
        touches_bus=touches_bus,
        config_schema=config_schema,
        source=source,
    )


async def _load_module_extension(
    source: ExtensionSource,
    api_factory: Callable[[str], ExtensionAPI],
    *,
    bus_available: bool = False,
    reload: bool = False,
) -> LoadedExtension:
    """Import an installed extension under its real module name and invoke ``register(api)``.

    ``importlib.import_module``, never a private name, so the package keeps
    one copy of its module state and its relative imports
    (docs/EXTENSION-PACKAGES.md §2).

    Args:
        source: A ``package`` or ``module`` source.
        api_factory: Produces the bucket-bound api for the module name.
        bus_available: Whether the session has a bus transport.
        reload: Re-execute an already-imported module (``reload_extension``).

    Raises:
        ImportError: The module has no source file to hash.
        Exception: Whatever the import, a declaration check or ``register`` raises.
    """
    module = importlib.import_module(source.target)
    if reload:
        module = importlib.reload(module)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ImportError(f"extension module {source.target} has no source file")
    register, api, subjects, touches_bus, config_schema = _register_module(
        module, source.label, api_factory, bus_available=bus_available, forget=lambda: None
    )
    outcome = register(api)
    if inspect.isawaitable(outcome):
        await outcome
    return LoadedExtension(
        path=source.label,
        register=register,
        api=api,
        content_hash=hashlib.sha256(Path(module_file).read_bytes()).hexdigest(),
        subjects=subjects,
        touches_bus=touches_bus,
        config_schema=config_schema,
        source=source,
    )


async def _load_source(
    source: ExtensionSource,
    api_factory: Callable[[str], ExtensionAPI],
    *,
    bus_available: bool,
    reload: bool = False,
) -> LoadedExtension:
    """Load ``source`` by the route its kind takes: real module name, or file path."""
    if source.kind in ("package", "module"):
        return await _load_module_extension(
            source, api_factory, bus_available=bus_available, reload=reload
        )
    return await _load_one_extension(
        Path(source.target), api_factory, bus_available=bus_available, source=source
    )


def _withheld(source: ExtensionSource, state: str, reason: str, **extra: Any) -> ExtensionRecord:
    """An :class:`ExtensionRecord` for a source that was found and not loaded."""
    return ExtensionRecord(
        identifier=source.identifier,
        kind=source.kind,
        target=source.target,
        state=state,  # type: ignore[arg-type]
        reason=reason,
        distribution=source.distribution,
        version=source.version,
        entry_point=source.entry_point,
        **extra,
    )


def _unauthorized_reason(source: ExtensionSource, verdict: str, previous: str | None) -> str:
    """The sentence a head shows for a discovered, unauthorized source."""
    if source.kind == "package" and verdict == "update":
        return (
            f"{source.distribution} changed from {previous} to {source.version} "
            "since it was authorized"
        )
    if source.kind == "package":
        return f"{source.distribution} {source.version} has never been authorized"
    return "this drop-in has never been authorized"


@agent_facing(topic="extensions")
@dataclass(frozen=True)
class ExtensionPlan:
    """What a load will do with every source, decided before anything is imported.

    Attributes:
        load: The sources to load, in load order, each with whether it was
            named with ``-e``.
        withheld: The sources found and not loaded, as records.
        grandfathered: Drop-ins authorized because no store existed (§5).
    """

    load: list[tuple[ExtensionSource, bool]]
    withheld: list[ExtensionRecord]
    grandfathered: list[str]


@agent_facing(topic="extensions")
def plan_extensions(
    explicit: Sequence[str] = (),
    *,
    discover: bool = True,
    extensions_dir: str | Path | None = None,
    store_path: str | Path | None = None,
    write: bool = True,
) -> ExtensionPlan:
    """Decide which extensions load, from metadata and paths alone (docs/EXTENSION-PACKAGES.md §5).

    Discovered drop-ins load first, then discovered packages, then the ``-e``
    values in argument order. A discovered source loads only with an
    authorization record. An ``-e`` value is authorized by being named, and
    claims its identifier first: a discovered source with the same identifier
    but another target is withheld as a ``collision``, as is a later
    discovered source whose identifier an earlier one already claimed. Two
    routes to one file load once.

    Args:
        explicit: The ``-e`` values (paths or module names).
        discover: Whether to discover at all; ``--no-extensions`` sets False.
        extensions_dir: The drop-in directory; ``None`` is
            :func:`~tau_agent_core.extension_sources.default_extensions_dir`.
        store_path: The authorization store; ``None`` is the file beside
            ``extensions_dir``.
        write: Whether a missing store may be created to grandfather the
            drop-ins present. A listing passes False.

    Returns:
        The plan.

    Raises:
        ValueError: An ``-e`` value resolves to nothing, or is ambiguous.
        AuthorizationStoreError: The store exists and cannot be read.
    """
    packages = extension_sources.discover_packages()
    explicit_sources = [extension_sources.resolve_explicit(raw, packages) for raw in explicit]
    claimed: dict[str, str] = {}
    seen_targets: set[str] = set()
    load: list[tuple[ExtensionSource, bool]] = []
    withheld: list[ExtensionRecord] = []

    def _target_key(source: ExtensionSource) -> str:
        if source.kind in ("package", "module"):
            return f"module:{source.target}"
        return str(Path(source.target).expanduser().resolve())

    for source in explicit_sources:
        claimed.setdefault(source.identifier, _target_key(source))

    grandfathered: list[str] = []
    if discover:
        directory = (
            Path(extensions_dir)
            if extensions_dir is not None
            else extension_sources.default_extensions_dir()
        )
        dropins = extension_sources.discover_dropins(directory)
        store = AuthorizationStore.load(
            store_path if store_path is not None else store_path_for(directory)
        )
        if write:
            grandfathered = store.grandfather(dropins)
        pending_grandfather = not store.existed
        explicit_keys = {_target_key(s) for s in explicit_sources}
        for source in [*dropins, *packages]:
            key = _target_key(source)
            if source.unusable is not None:
                withheld.append(_withheld(source, "unusable", source.unusable))
                continue
            if key in explicit_keys:
                continue
            owner = claimed.get(source.identifier)
            if owner is not None and owner != key:
                withheld.append(
                    _withheld(
                        source,
                        "collision",
                        f"another source already claims {source.identifier!r}",
                        authorization=None,
                    )
                )
                continue
            if pending_grandfather and source.kind == "dropin":
                verdict, previous = "authorized", None
            else:
                answer = store.verdict(source)
                verdict, previous = answer.verdict, answer.previous_version
            if verdict != "authorized":
                withheld.append(
                    _withheld(
                        source,
                        "unauthorized",
                        _unauthorized_reason(source, verdict, previous),
                        authorization=verdict,
                        previous_version=previous,
                    )
                )
                continue
            claimed[source.identifier] = key
            seen_targets.add(key)
            load.append((source, False))

    for source in explicit_sources:
        key = _target_key(source)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        load.append((source, True))
    return ExtensionPlan(load=load, withheld=withheld, grandfathered=grandfathered)


async def _load_extensions(
    explicit_paths: list[str] | None = None,
    *,
    discover: bool = True,
    user_dir: str | None = None,
    api_factory: Callable[[str], ExtensionAPI] | None = None,
    collect_explicit_errors: bool = False,
    bus_available: bool = False,
    store_path: str | Path | None = None,
) -> LoadExtensionsResult:
    """Plan, import, and invoke ``register(api)`` for every extension that may load.

    THE single extension loader. :func:`plan_extensions` decides from metadata
    alone what loads (docs/EXTENSION-PACKAGES.md §5); this imports each planned
    source by its route (a package under its real name, a file under a private
    one) and calls its ``register``.

    Args:
        explicit_paths: The ``-e`` values: paths or module names. A failure
            here RAISES.
        discover: Whether to discover drop-ins and packages. ``--no-extensions``
            sets False; ``explicit_paths`` load either way.
        user_dir: The drop-in directory; ``None`` is ``<tau home>/extensions``.
        api_factory: Produces a fresh ``ExtensionAPI`` per extension, keyed by its
            label. Defaults to :func:`_standalone_api_factory`.
        collect_explicit_errors: Collect an explicit failure into
            ``result.errors`` and keep loading, instead of raising. The TUI
            passes True: a launched Textual session cannot abort mid-load.
        bus_available: Whether this load's session has a bus transport (H8);
            a ``TOUCHES_BUS`` extension is refused without one.
        store_path: The authorization store; ``None`` is the file beside the
            drop-in directory.

    Returns:
        The loaded extensions, the failures collected, the sources withheld,
        and any drop-ins grandfathered.

    Raises:
        ExtensionStartupError: An explicit extension failed to load, unless
            ``collect_explicit_errors``; the original is ``__cause__`` (#47).
        ValueError: An ``-e`` value names nothing installed, or is ambiguous.
        AuthorizationStoreError: The authorization store cannot be read.
    """
    if api_factory is None:
        api_factory = _standalone_api_factory

    plan = plan_extensions(
        explicit_paths or (),
        discover=discover,
        extensions_dir=user_dir,
        store_path=store_path,
    )
    result = LoadExtensionsResult(withheld=plan.withheld, grandfathered=plan.grandfathered)
    for source, is_explicit in plan.load:
        try:
            loaded = await _load_source(source, api_factory, bus_available=bus_available)
        except Exception as exc:
            if is_explicit and not collect_explicit_errors:
                raise ExtensionStartupError(
                    source.identifier, source.label, "load", f"{type(exc).__name__}: {exc}"
                ) from exc
            result.errors.append(
                ExtensionLoadError(
                    path=source.label, error=str(exc), explicit=is_explicit, source=source
                )
            )
            continue
        loaded.explicit = is_explicit
        result.extensions.append(loaded)

    return result


def _record_for_source(
    source: ExtensionSource, state: str, *, explicit: bool, reason: str | None = None
) -> ExtensionRecord:
    """An :class:`ExtensionRecord` for a planned or loaded source."""
    return ExtensionRecord(
        identifier=source.identifier,
        kind=source.kind,
        target=source.target,
        state=state,  # type: ignore[arg-type]
        reason=reason,
        distribution=source.distribution,
        version=source.version,
        entry_point=source.entry_point,
        authorization="explicit" if explicit else "authorized",
    )


def _source_of(label: str, source: ExtensionSource | None) -> ExtensionSource:
    """``source``, or the file-path source a label built outside the loader stands for."""
    if source is not None:
        return source
    return ExtensionSource(identifier=extension_identifier(label), kind="path", target=label)


@agent_facing(topic="extensions")
def session_extension_records(
    loaded: Sequence[LoadedExtension],
    disabled: Collection[str],
    errors: Sequence[ExtensionLoadError],
    withheld: Sequence[ExtensionRecord],
    orphan_topics: Mapping[str, tuple[str, ...]] | None = None,
    start_errors: Mapping[str, str] | None = None,
) -> list[ExtensionRecord]:
    """The ``tau ext list`` rows for a live session (docs/EXTENSION-PACKAGES.md §7).

    Args:
        loaded: The managed extensions, in load order.
        disabled: The labels of those currently disabled.
        errors: The extensions that failed to load.
        withheld: The sources found and not loaded.
        orphan_topics: Per label, subscribed topics whose emitter is no loaded
            extension (#44).
        start_errors: Per label, what its ``session_start`` handler raised; such
            an extension is ``failed`` though its registrations stand (#47).

    Returns:
        Loaded and disabled rows, then failed, then withheld.
    """
    orphans = orphan_topics or {}
    started = start_errors or {}
    records = [
        replace(
            _record_for_source(
                _source_of(ext.path, ext.source),
                "disabled"
                if ext.path in disabled
                else ("failed" if ext.path in started else "loaded"),
                explicit=ext.explicit,
                reason=f"session_start raised: {started[ext.path]}"
                if ext.path in started
                else None,
            ),
            orphan_topics=orphans.get(ext.path, ()),
        )
        for ext in loaded
    ]
    records += [
        _record_for_source(
            _source_of(err.path, err.source), "failed", explicit=err.explicit, reason=err.error
        )
        for err in errors
    ]
    return records + list(withheld)


@agent_facing(topic="extensions")
def extension_records(
    explicit: Sequence[str] = (),
    *,
    discover: bool = True,
    extensions_dir: str | Path | None = None,
    store_path: str | Path | None = None,
) -> list[ExtensionRecord]:
    """The ``tau ext list`` rows with no session: what would load, and what would not.

    Imports nothing and writes nothing; a missing authorization store lists the
    drop-ins present as ``authorized``, because the first start grandfathers
    them (docs/EXTENSION-PACKAGES.md §5).

    Args:
        explicit: ``-e`` values to include, as a start would.
        discover: Whether to discover at all.
        extensions_dir: The drop-in directory; ``None`` is the default.
        store_path: The authorization store; ``None`` is the file beside it.

    Returns:
        The planned sources as ``authorized``, then the withheld ones.

    Raises:
        ValueError: An ``-e`` value names nothing installed.
        AuthorizationStoreError: The store cannot be read.
    """
    plan = plan_extensions(
        explicit,
        discover=discover,
        extensions_dir=extensions_dir,
        store_path=store_path,
        write=False,
    )
    rows = [_record_for_source(src, "authorized", explicit=exp) for src, exp in plan.load]
    return rows + plan.withheld


def _discovered_sources(extensions_dir: str | Path | None) -> tuple[Path, list[ExtensionSource]]:
    """The drop-in directory in use and every usable source discovery finds there and installed."""
    directory = (
        Path(extensions_dir)
        if extensions_dir is not None
        else extension_sources.default_extensions_dir()
    )
    found = [
        *extension_sources.discover_dropins(directory),
        *extension_sources.discover_packages(),
    ]
    return directory, [source for source in found if source.unusable is None]


@agent_facing(topic="extensions")
def find_discovered_extension(
    token: str,
    *,
    extensions_dir: str | Path | None = None,
    kind: Literal["package", "dropin"] | None = None,
) -> ExtensionSource:
    """The one discovered source ``token`` names: a drop-in path, an identifier, or its last segment.

    Args:
        token: What the user typed.
        extensions_dir: The drop-in directory; ``None`` is the default.
        kind: Consider only packages, or only drop-ins.

    Returns:
        The source.

    Raises:
        ValueError: Nothing matches, or two sources do; the message names them
            and how to choose.
    """
    _, sources = _discovered_sources(extensions_dir)
    if kind is not None:
        sources = [source for source in sources if source.kind == kind]
    if extension_sources.is_path_spec(token):
        wanted = str(Path(token).expanduser().resolve())
        matches = [
            s for s in sources if s.kind == "dropin" and str(Path(s.target).resolve()) == wanted
        ]
    else:
        ident = extension_sources.resolve_shorthand(
            token, sorted({source.identifier for source in sources})
        )
        matches = [source for source in sources if source.identifier == ident]
    if not matches:
        raise ValueError(f"no discovered extension is named {token!r}; see `tau ext list`")
    if len(matches) > 1:
        names = "; ".join(f"{m.kind} {m.target}" for m in matches)
        raise ValueError(
            f"{token!r} names more than one discovered extension ({names}). Pass the "
            "drop-in's path, or --package for the installed one."
        )
    return matches[0]


@agent_facing(topic="extensions")
def authorize_discovered_extension(
    token: str,
    *,
    extensions_dir: str | Path | None = None,
    store_path: str | Path | None = None,
    kind: Literal["package", "dropin"] | None = None,
) -> ExtensionSource:
    """Write an authorization record for the discovered source ``token`` names.

    The non-interactive route to the gate (docs/EXTENSION-PACKAGES.md §9):
    ``tau ext authorize``, for a host with no prompt to answer.

    Args:
        token: A drop-in path, an identifier, or its last segment.
        extensions_dir: The drop-in directory; ``None`` is the default.
        store_path: The authorization store; ``None`` is the file beside it.
        kind: Consider only packages, or only drop-ins.

    Returns:
        The source now authorized.

    Raises:
        ValueError: ``token`` matches nothing, or more than one source.
        AuthorizationStoreError: The store cannot be read.
    """
    directory, _ = _discovered_sources(extensions_dir)
    source = find_discovered_extension(token, extensions_dir=directory, kind=kind)
    store = AuthorizationStore.load(
        store_path if store_path is not None else store_path_for(directory)
    )
    store.authorize(source)
    store.save()
    return source


@agent_facing(topic="extensions")
def revoke_extension_authorization(
    identifier: str,
    *,
    extensions_dir: str | Path | None = None,
    store_path: str | Path | None = None,
) -> int:
    """Remove every authorization record for ``identifier``.

    Args:
        identifier: The full identifier; a shorthand is not resolved, because
            removing records is not a convenience to guess at.
        extensions_dir: The drop-in directory; ``None`` is the default.
        store_path: The authorization store; ``None`` is the file beside it.

    Returns:
        How many records were removed.

    Raises:
        AuthorizationStoreError: The store cannot be read.
    """
    directory = (
        Path(extensions_dir)
        if extensions_dir is not None
        else extension_sources.default_extensions_dir()
    )
    store = AuthorizationStore.load(
        store_path if store_path is not None else store_path_for(directory)
    )
    removed = store.revoke(identifier)
    if removed:
        store.save()
    return removed


@agent_facing(topic="extensions")
def withheld_notices(result: LoadExtensionsResult) -> list[str]:
    """The lines a head shows after a load for what was grandfathered or withheld.

    The text is a proposal (docs/EXTENSION-PACKAGES.md §9). A head with a
    prompt may ask about an ``unauthorized`` record instead of showing its line.

    Args:
        result: The load result.

    Returns:
        One line for any grandfathered drop-ins, then one per withheld record.
    """
    lines = []
    if result.grandfathered:
        lines.append(
            f"authorized {len(result.grandfathered)} existing drop-in extension(s) at upgrade: "
            f"{', '.join(result.grandfathered)}. From now on τ loads a new drop-in or package "
            "only once it is authorized; `tau ext list` shows each one's state."
        )
    for record in result.withheld:
        lines.append(withheld_notice(record))
    return lines


@agent_facing(topic="extensions")
def withheld_notice(record: ExtensionRecord) -> str:
    """The one line for a record that was found and not loaded (§9, proposal).

    Args:
        record: An ``unauthorized``, ``unusable`` or ``collision`` record.

    Returns:
        A sentence naming the extension, why it did not load, and what to do.
    """
    where = record.distribution if record.kind == "package" else record.target
    if record.state == "unauthorized":
        return (
            f"extension {record.identifier} ({where}) was found but not loaded: "
            f"{record.reason}. To load it: tau ext authorize {record.identifier}"
        )
    return f"extension {record.identifier} ({where}) was not loaded: {record.reason}"


@agent_facing(topic="extensions")
def authorization_prompt(record: ExtensionRecord) -> tuple[str, str]:
    """The title and body a head's authorization prompt shows (§9, proposal).

    Says whether this is an update or a new extension (#35 decision 3), and
    what authorizing grants.

    Args:
        record: An ``unauthorized`` record.

    Returns:
        ``(title, body)``.
    """
    if record.kind == "package" and record.authorization == "update":
        what = (
            f"{record.distribution} was updated from {record.previous_version} to "
            f"{record.version} since you authorized it."
        )
        title = f"Load the updated extension {record.identifier}?"
    elif record.kind == "package":
        what = (
            f"The installed package {record.distribution} {record.version} provides the "
            f"extension {record.identifier}. You have not authorized it."
        )
        title = f"Load the new extension {record.identifier}?"
    else:
        what = f"A new drop-in extension was found at {record.target}."
        title = f"Load the new extension {record.identifier}?"
    body = (
        f"{what} An extension runs its own code in every τ session that loads it. "
        f"Authorizing it is remembered until its package version changes; "
        f"`tau ext revoke {record.identifier}` undoes it."
    )
    return title, body


@agent_facing(topic="extensions")
def orphan_subscription_notices(records: Sequence[ExtensionRecord]) -> list[str]:
    """The debug lines for subscriptions no loaded extension can answer (#44).

    A notice, never an error: a subscriber may wait for an optional extension
    (docs/EXTENSION-PACKAGES.md §10).

    Args:
        records: From :meth:`AgentSession.extension_records`.

    Returns:
        One line per orphaned topic.
    """
    return [
        f"{record.identifier} subscribes to {topic}, but no loaded extension emits "
        f"under {topic.split(':', 1)[1].rpartition(':')[0]!r}"
        for record in records
        for topic in record.orphan_topics
    ]


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


CONTEXT_FILE_NAMES: tuple[str, ...] = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
)

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
    """One discovered context file.

    Attributes:
        path: The file's canonicalized path.
        content: Its text, decoded as UTF-8 with any leading BOM dropped.
    """

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
    if worktree_root == main_repo_root or not worktree_root.is_relative_to(main_repo_root):
        return None
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

    Prompt order, so the nearest file has the last word:

    1. ``agent_dir``'s context file (τ home's by default), the user's global one.
    2. Every ancestor of ``cwd`` that has one, root-most first, ending with
       ``cwd``'s own. The walk goes to ``/``. In a linked worktree nested inside
       its main repo, the main checkout's copy is skipped
       (:func:`_find_shadowed_context_file`).
    3. ``cwd``'s ``.tau/SYSTEM.md`` (:data:`TAU_SYSTEM_FILE`).

    A file reached twice (by resolved path) is taken once, at its first slot.

    Args:
        cwd: Where the ancestor walk starts; ``None`` means the process cwd.
        agent_dir: The global context directory; ``None`` means τ home
            (:func:`~tau_agent_core.tau_home.resolve_tau_home`).

    Returns:
        The context files in prompt order, weakest first.

    Raises:
        ContextFileError: A found file could not be read or is not valid UTF-8.
    """
    resolved_cwd = _canonicalize(Path(cwd).expanduser() if cwd else Path(os.getcwd()))
    resolved_agent_dir = _canonicalize(
        Path(agent_dir).expanduser() if agent_dir else resolve_tau_home()
    )

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
    system-prompt.ts:48). This composes only the base-text slot of
    :func:`_build_system_prompt`, so the sections land ahead of the project
    context and the tool list. ``tau_coding_agent.headless`` re-exports it.

    Args:
        base: The base system prompt; may be empty.
        sections: The sections to append, in order; ``None`` or empty appends
            nothing.

    Returns:
        ``base`` and the sections joined by blank lines: ``base`` unchanged with
        no sections, just the sections with an empty ``base``.
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


_PROMPT_FIELD = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

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
    skills: Sequence[Skill] = (),
) -> str:
    """Build the system prompt from a base prompt, context files, skills and tools.

    The system prompt is built from:

    1. ``custom_prompt`` if given, else :data:`BASE_SYSTEM_PROMPT`.
    2. Every context file :func:`load_project_context_files` discovers, unless
       ``no_context_files``.
    3. The skill listing (:func:`~tau_agent_core.skills.render_skills_block`),
       only when a tool named ``read`` is offered, since the listing tells the
       model to read the skill's file.
    4. Tool definitions for ``prompt_snippet`` and ``prompt_guidelines``.

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
    ``{{skills}}``       the ``<available_skills>`` block.
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
        skills: The skills to list; pass ``SkillCatalog.listed()``.

    Returns:
        Complete system prompt string.

    Raises:
        SystemPromptFieldError: on a ``{{field}}`` naming no known field.
    """
    cwd = cwd or os.getcwd()

    context_block = "" if no_context_files else _render_project_context(cwd, agent_dir)
    tools_block = _render_tool_list(tools)
    can_read = any(tool.name == "read" for tool in (tools or []))
    skills_block = render_skills_block(skills) if can_read else ""

    fields = {
        "project_context": context_block,
        "skills": skills_block,
        "tools": tools_block,
        "tool_names": ", ".join(tool.name for tool in (tools or [])),
        "cwd": cwd,
        "model": model or "",
    }

    base_text, base_used = _render_prompt_fields(BASE_SYSTEM_PROMPT, fields)

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
    if skills_block and "skills" not in used:
        lines.append("")
        lines.append(skills_block)
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
    thinking_level: str | None = None,
    cwd: str | None = None,
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel",
    compaction_policy: CompactionPolicy | None = None,
    compaction_settings: CompactionSettings | None = None,
    model_resolver: Callable[[str], Model] | None = None,
    bus_available: bool = False,
    no_tools: Literal["all", "builtin"] | None = None,
    max_turns: int | None = None,
    tool_options: dict[str, dict[str, Any]] | None = None,
    prompt_template_paths: Iterable[str | Path] = (),
    no_prompt_templates: bool = False,
    skill_paths: Iterable[str | Path] = (),
    no_skills: bool = False,
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
        thinking_level: ``None`` (the default) sends no reasoning control;
            ``"off"`` sends the provider's off form; any other level marks the
            model reasoning-capable (docs/REASONING-WIRE.md §4).
        cwd: The working directory: context-file discovery starts there, the
            built-in tools resolve paths and run commands there, and the session
            records it in its config. ``None`` is the process cwd.
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
        compaction_settings: Optional :class:`~tau_agent_core.compaction.CompactionSettings`
            — the two thresholds and the retention size. Mutually exclusive with
            ``compaction_policy``, which supplies its own; ``AgentSession`` raises
            on both. ``None`` takes the shipped defaults.
        model_resolver: Config model name to ``Model``, forwarded to
            ``AgentSession``. Required by a ``local_summarizer`` policy, whose
            summarizer it resolves together with its key.
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
        prompt_template_paths: Template files or directories to load ahead of
            the discovered ones (docs/PROMPT-TEMPLATES-AND-SKILLS.md §4).
        no_prompt_templates: Skip template discovery; explicit paths still load.
        skill_paths: Skill directories, ``SKILL.md`` files or directories of
            skills to load ahead of the discovered ones (§5).
        no_skills: Skip skill discovery; explicit paths still load.

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

    reasoning_arg = thinking_level
    if reasoning_arg not in (None, "off"):
        model.reasoning = True

    if no_tools is not None and tools:
        raise ValueError(
            f"create_agent_session() got tools={tools!r} together with "
            f"no_tools={no_tools!r}. Those ask for opposite things: a built-in tool, "
            "and no built-in tools. Drop `tools=` to suppress the built-ins, or drop "
            "`no_tools=` to offer them."
        )
    tool_objs: list[AgentTool] = (
        [] if no_tools is not None else _resolve_tools(tools, tool_options, cwd)
    )

    ext_factories = list(extensions) if extensions else []

    templates = load_prompt_templates(
        cwd=cwd, paths=prompt_template_paths, discover=not no_prompt_templates
    )
    skills = load_skills(cwd=cwd, paths=skill_paths, discover=not no_skills)

    sys_prompt = _build_system_prompt(
        cwd,
        tool_objs,
        custom_prompt=system_prompt,
        no_context_files=no_context_files,
        model=model.id,
        skills=skills.listed(),
    )

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
        compaction_settings=compaction_settings,
        model_resolver=model_resolver,
        tool_execution_mode=tool_execution_mode,
        bus_available=bus_available,
        no_tools=no_tools,
        max_turns=max_turns,
        cwd=cwd,
        prompt_templates=templates,
        skills=skills,
    )
