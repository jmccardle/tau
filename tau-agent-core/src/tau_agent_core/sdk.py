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

import importlib.util
import inspect
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog, SessionLog


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


def _resolve_tools(tool_names: list[str] | None) -> list:
    """Resolve tool names to tool instances.

    Tool instances must have .name, .label, .description, .parameters,
    and .execute attributes.

    Args:
        tool_names: List of tool name strings (e.g., ["read", "bash"]).

    Returns:
        List of tool instances (AgentTool or raw tool class).

    Raises:
        ValueError: If a tool name is not recognized.
    """
    if not tool_names:
        return []

    # Import the tools package to get access to built-in tools
    import tau_agent_core.tools as tools_pkg

    # Map of tool name to (module, class_name) for dynamic import
    tool_classes = {
        "read": ("read", "ReadTool"),
        "write": ("write", "WriteTool"),
        "edit": ("edit", "EditTool"),
        "bash": ("bash", "BashTool"),
        "ls": ("ls", "LsTool"),
        "grep": ("grep", "GrepTool"),
        "find": ("find", "FindTool"),
    }

    tool_objs: list = []
    for name in tool_names:
        if name not in tool_classes:
            raise ValueError(f"Unknown tool: {name}")

        mod_name, class_name = tool_classes[name]
        mod = getattr(tools_pkg, mod_name, None)
        if mod is None:
            mod = __import__(
                f"tau_agent_core.tools.{mod_name}",
                fromlist=[class_name],
            )
        cls = getattr(mod, class_name, None)
        if cls is None:
            raise ValueError(f"Unknown tool: {name}")

        tool_obj = cls()
        tool_objs.append(tool_obj)

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


@dataclass
class LoadedExtension:
    """A successfully loaded extension.

    Narrowed port of pi's ``Extension`` record (coding-agent types.ts:1577) to
    what S1 needs: the source ``path``, the module-level ``register`` factory
    that was invoked, and the ``ExtensionAPI`` it registered against.
    """

    path: str
    register: Callable[..., Any]
    api: ExtensionAPI


@dataclass
class ExtensionLoadError:
    """A discovered extension that failed to load (pi types.ts:1590 errors[])."""

    path: str
    error: str


@dataclass
class LoadExtensionsResult:
    """Result of loading extensions — port of pi ``LoadExtensionsResult``.

    Reference: pi agent/../types.ts:1590. The ``runtime`` field is intentionally
    omitted until the API is bound to the live session (E1/S3).
    """

    extensions: list[LoadedExtension] = field(default_factory=list)
    errors: list[ExtensionLoadError] = field(default_factory=list)


@dataclass
class ExtensionInfo:
    """Read-only summary of one loaded extension for the ``/extensions`` surface.

    Reference: EXTENSIONS-E5-WIRING.md §5 (E5.4 / S34). Carries an extension's
    display ``name``, source ``path``, and the ``tools`` / ``commands`` /
    ``shortcuts`` / ``hooks`` it registered — everything the palette listing shows
    for a loaded extension (shortcuts E10 §6 / S69).
    """

    name: str
    path: str
    tools: list[str]
    commands: list[str]
    shortcuts: list[str]
    hooks: list[str]


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
) -> LoadedExtension:
    """Import one extension module and invoke its ``register(api)``.

    Imports by file path (``importlib.util.spec_from_file_location``), fetches
    the module-level ``register`` callable, invokes ``register(api)``, and
    awaits the result when ``register`` is a coroutine function.

    Raises on any failure (missing file/spec, missing or non-callable
    ``register``, or an exception raised by ``register``); the caller applies
    the explicit-vs-discovered error policy.
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

    register = getattr(module, "register", None)
    if register is None:
        raise AttributeError(f"{path} has no register(api) function")
    if not callable(register):
        raise TypeError(f"{path} register is not callable")

    # Path-aware (S24): the factory keys each extension's api to its real file
    # path so a session-bound factory can bind it to a fresh runner bucket.
    api = api_factory(str(path))
    outcome = register(api)
    if inspect.isawaitable(outcome):
        await outcome

    return LoadedExtension(path=str(path), register=register, api=api)


async def _load_extensions(
    explicit_paths: list[str] | None = None,
    *,
    discover: bool = True,
    user_dir: str | None = None,
    api_factory: Callable[[str], ExtensionAPI] | None = None,
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

    Returns:
        ``LoadExtensionsResult`` with the loaded extensions and any discovered
        load errors.

    Raises:
        Exception: propagated from an explicit ``-e`` extension that fails to
            load (Fail-Early — the user named it).
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
            loaded = await _load_one_extension(path, api_factory)
        except Exception as exc:
            if is_explicit:
                # Fail-Early: the user named this path — surfacing it silently
                # is the anti-pattern, so re-raise.
                raise
            # Discovered: collect and keep loading the rest. The error is RETURNED
            # (never swallowed) for the caller to surface — headless prints it to
            # stderr, the TUI shows a notice. The loader deliberately does NOT
            # print here: a stderr write during a live Textual render corrupts the
            # screen, and structured errors[] is the honest channel anyway (E5 §2.1).
            result.errors.append(ExtensionLoadError(path=str(path), error=str(exc)))
            continue
        result.extensions.append(loaded)

    return result


def _build_system_prompt(
    cwd: str | None = None,
    tools: list | None = None,
) -> str:
    """Build system prompt from context files and tool definitions.

    The system prompt is built from:
    1. Base prompt (hardcoded)
    2. AGENTS.md if present in cwd
    3. .tau/SYSTEM.md if present in cwd
    4. Tool definitions for prompt_snippet and prompt_guidelines

    Args:
        cwd: Current working directory.
        tools: List of tool instances (AgentTool or raw tool class).

    Returns:
        Complete system prompt string.
    """
    cwd = cwd or os.getcwd()
    lines: list[str] = []

    # Base prompt
    lines.append(
        "You are τ, a helpful AI assistant. "
        "You can help with coding, file editing, and system commands."
    )

    # Try to load AGENTS.md
    agents_md = Path(cwd) / "AGENTS.md"
    if agents_md.exists():
        lines.append("")
        lines.append("---")
        lines.append("Additional instructions from AGENTS.md:")
        lines.append(agents_md.read_text(encoding="utf-8", errors="replace"))

    # Try to load .tau/SYSTEM.md
    system_md = Path(cwd) / ".tau" / "SYSTEM.md"
    if system_md.exists():
        lines.append("")
        lines.append("---")
        lines.append("Additional instructions from .tau/SYSTEM.md:")
        lines.append(system_md.read_text(encoding="utf-8", errors="replace"))

    # Add tool definitions (handles both AgentTool and raw tool classes)
    if tools:
        lines.append("")
        lines.append("---")
        lines.append("Available tools:")
        for tool in tools:
            # Handle both AgentTool (has .definition) and raw tools (has .label)
            if hasattr(tool, "definition"):
                # AgentTool
                snippet = tool.definition.prompt_snippet
                guidelines = tool.definition.prompt_guidelines
            else:
                # Raw tool class (ReadTool, BashTool, etc.)
                snippet = f"{tool.name}: {getattr(tool, 'label', tool.name)}"
                guidelines = None

            if snippet:
                lines.append(f"- {snippet}")
            if guidelines:
                for guideline in guidelines:
                    lines.append(f"  - {guideline}")

    return "\n".join(lines)


def create_agent_session(
    model: str | Model = "gpt-4o",
    provider: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    tools: list[str] | None = None,
    session_log: SessionLog | None = None,
    extensions: list[Callable] | None = None,
    system_prompt: str | None = None,
    thinking_level: str = "off",
    cwd: str | None = None,
    settings: dict | None = None,
) -> AgentSession:
    """Create an AgentSession with all defaults.

    This is the main SDK entry point. It handles:
    - Model resolution (string → Model object)
    - Tool discovery (string names → AgentTool objects)
    - Extension registration (inline factory callables invoked at construction)
    - System prompt building (from AGENTS.md, .tau/SYSTEM.md)
    - Settings loading (from ~/.tau/settings.json)

    Args:
        model: Model identifier string or Model object (default: "gpt-4o").
        provider: Provider name for model resolution (default: "openai").
        base_url: Optional custom API base URL.
        api_key: Optional API key.
        tools: List of tool name strings (e.g., ["read", "bash"]).
        session_log: Optional SessionLog to persist through (the coding-agent's
            file Session on the live path). Defaults to an in-memory log.
        extensions: List of extension factory callables.
        system_prompt: Optional custom system prompt.
        thinking_level: Thinking level ("off", "minimal", "low", "medium",
            "high", "xhigh"). A non-"off" level marks the model reasoning-capable
            and is forwarded to the provider as `reasoning_effort`.
        cwd: Current working directory.
        settings: Optional settings dict.

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
    tool_objs = _resolve_tools(tools)

    # 3. Extensions: inline factory callables are invoked by AgentSession at
    #    construction (pi's loadExtensionFromFactory analog). File-path discovery
    #    + loading is handled by the single async loader (_load_extensions),
    #    wired into the CLI/headless run path (E0/S2).
    ext_factories = list(extensions) if extensions else []

    # 4. Build system prompt
    sys_prompt = system_prompt or _build_system_prompt(cwd, tool_objs)

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
    )
