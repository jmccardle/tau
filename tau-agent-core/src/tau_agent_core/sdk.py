"""τ-agent-core sdk: SDK entry point — create_agent_session().

Reference: PHASE-2-SUBPHASE-4.md — Agent Session and SDK Entry Point.
Reference: SUBPHASE-0.0.md, "7. AgentSession Interface" section.

This module provides:
- create_agent_session(): Main SDK entry point for creating fully configured sessions.
- _resolve_model(): Resolve model string to Model object.
- _resolve_tools(): Discover and create tool objects from string names.
- _load_extensions(): Load extension factories from paths.
- _build_system_prompt(): Build system prompt from context files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from tau_ai.types import Model
from tau_ai.providers.registry import ProviderRegistry

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_manager import SessionManager
from tau_agent_core.tools.base import AgentTool


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
    # Try provider registry
    try:
        registry = ProviderRegistry()
        prov = registry.get(provider)
        if prov:
            return prov.resolve_model(model)
    except KeyError:
        pass
    # Fallback: create a generic model
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


def _load_extensions(
    extension_factories: list[Callable] | None,
    user_dir: str | None = None,
    project_dir: str | None = None,
) -> list[Callable]:
    """Load extension factories.

    Extensions are loaded from:
    1. Explicit factory callables passed in
    2. ~/.tau/extensions/ (user directory)
    3. ./.tau/extensions/ (project directory)

    Args:
        extension_factories: Explicit extension factory callables.
        user_dir: User extensions directory (~/.tau/extensions/).
        project_dir: Project extensions directory (./.tau/extensions/).

    Returns:
        List of extension factory callables.
    """
    exts = list(extension_factories) if extension_factories else []

    # Load from user directory
    if user_dir is None:
        user_dir = os.path.expanduser("~/.tau/extensions")
    _load_extensions_from_dir(user_dir, exts)

    # Load from project directory
    if project_dir is None:
        project_dir = os.path.join(os.getcwd(), ".tau", "extensions")
    _load_extensions_from_dir(project_dir, exts)

    return exts


def _load_extensions_from_dir(extensions_dir: str, exts: list[Callable]) -> None:
    """Load Python modules from an extensions directory.

    Each .py file in the directory is loaded as an extension.
    The module must have an `extend` function that takes an ExtensionAPI.

    Args:
        extensions_dir: Path to the extensions directory.
        exts: List to append extension factories to.
    """
    try:
        ext_path = Path(extensions_dir)
        if not ext_path.exists() or not ext_path.is_dir():
            return

        for py_file in sorted(ext_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Create a simple factory that imports and runs the extension
            def make_ext_factory(path_str: str) -> Callable:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "ext_module", path_str
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "extend"):
                        return lambda api: mod.extend(api)
                return lambda api: None  # No-op

            factory = make_ext_factory(str(py_file))
            exts.append(factory)
    except (ImportError, OSError):
        pass  # Silently ignore missing or unparseable extensions


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
    session_manager: SessionManager | None = None,
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
    - Extension loading (from ~/.tau/extensions/ and ./.tau/extensions/)
    - System prompt building (from AGENTS.md, .tau/SYSTEM.md)
    - Settings loading (from ~/.tau/settings.json)

    Args:
        model: Model identifier string or Model object (default: "gpt-4o").
        provider: Provider name for model resolution (default: "openai").
        base_url: Optional custom API base URL.
        api_key: Optional API key.
        tools: List of tool name strings (e.g., ["read", "bash"]).
        session_manager: Optional SessionManager instance.
        extensions: List of extension factory callables.
        system_prompt: Optional custom system prompt.
        thinking_level: Thinking level ("off", "low", "high").
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

    # 2. Discover and create tools
    tool_objs = _resolve_tools(tools)

    # 3. Load extensions
    ext_factories = _load_extensions(extensions)

    # 4. Build system prompt
    sys_prompt = system_prompt or _build_system_prompt(cwd, tool_objs)

    # 5. Create session manager
    if session_manager is None:
        session_manager = SessionManager(cwd=cwd)

    # 6. Create and return AgentSession
    return AgentSession(
        session_manager=session_manager,
        model=model,
        system_prompt=sys_prompt,
        tools=tool_objs,
        extensions=ext_factories,
    )
