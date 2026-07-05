"""τ-agent-core extensions registry — manages tool/command/flag registration.

Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.
Reference: PHASE-3-SUBPHASE-2.md ExtensionRegistry implementation.

Contract:
    class ToolInfo:
        name: str
        description: str
        parameters: dict
        source: str

    class ExtensionRegistry:
        def register_tool(self, definition: dict) -> None: ...
        def get_all_tools(self) -> list[ToolInfo]: ...
        def set_active_tools(self, names: list[str]) -> None: ...
        def get_active_tools(self) -> dict[str, dict]: ...
        def register_command(self, name: str, command: dict) -> None: ...
        def get_command(self, name: str) -> dict | None: ...
        def get_commands(self) -> dict[str, dict]: ...
        def register_shortcut(self, key: str, shortcut: dict) -> None: ...
        def get_shortcut(self, key: str) -> dict | None: ...
        def get_shortcuts(self) -> dict[str, dict]: ...

Note: ``append_entry`` is NO LONGER a registry method. Durable extension state is
persisted onto the session tree as a ``customEntry`` node via
``AgentSession._append_custom_entry`` (E6 §2 / S39), replacing the former RAM-only
``_entry_store`` that was lost on restart (G4). See ``ExtensionAPI.append_entry``.
"""

from __future__ import annotations


class ToolInfo:
    """Read-only tool information."""

    def __init__(self, name: str, description: str, parameters: dict, source: str):
        """Initialize tool info.

        Args:
            name: Tool name.
            description: Tool description.
            parameters: Tool parameters (JSON schema).
            source: Where the tool is from ("built-in" or extension name).
        """
        self.name = name
        self.description = description
        self.parameters = parameters
        self.source = source  # "built-in" or extension name

    def __repr__(self) -> str:
        return f"ToolInfo(name={self.name!r}, source={self.source!r})"


class ExtensionRegistry:
    """Manages tool, command, and flag registration.

    Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.
    Reference: PHASE-3-SUBPHASE-2.md implementation outline.
    """

    def __init__(self) -> None:
        """Initialize the registry with empty collections."""
        self._tools: dict[str, dict] = {}  # name -> definition
        self._commands: dict[str, dict] = {}  # name -> command def
        self._shortcuts: dict[str, dict] = {}  # chord-tail key -> shortcut def
        self._active_tools: set[str] | None = None  # None = all active

    def register_tool(self, definition: dict) -> None:
        """Register a tool definition."""
        name = definition["name"]
        if name in self._tools:
            import logging

            logging.warning(f"Tool '{name}' already registered, overwriting")
        self._tools[name] = definition

    def get_all_tools(self) -> list[ToolInfo]:
        """Get all registered tools (built-in + extension)."""
        result = []
        for name, defn in self._tools.items():
            result.append(
                ToolInfo(
                    name=name,
                    description=defn.get("description", ""),
                    parameters=defn.get("parameters", {}),
                    source=defn.get("_source", "built-in"),
                )
            )
        return result

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name."""
        self._active_tools = set(names)

    def get_active_tools(self) -> dict[str, dict]:
        """Get currently active tools."""
        if self._active_tools is None:
            return self._tools
        return {n: d for n, d in self._tools.items() if n in self._active_tools}

    def register_command(self, name: str, command: dict) -> None:
        """Register a slash command."""
        self._commands[name] = command

    def get_command(self, name: str) -> dict | None:
        """Look up a registered slash command by name (``None`` if unknown)."""
        return self._commands.get(name)

    def get_commands(self) -> dict[str, dict]:
        """Get all registered slash commands (name -> command def)."""
        return dict(self._commands)

    def register_shortcut(self, key: str, shortcut: dict) -> None:
        """Register an extension key binding (E10 §6 / S69).

        ``key`` is the chord-tail key (the second key after the ``ctrl+e``
        extension leader — the guarded namespace the TUI binds these under, so an
        extension can never clobber a core global binding). ``shortcut`` carries
        the ``command`` name to dispatch (plus optional ``args``/``description``).

        Last-wins on a duplicate tail key (two extensions binding the same chord),
        mirroring :meth:`register_tool`'s warn-and-overwrite — a namespace collision
        is an environment fact, not one extension's construction bug.
        """
        if key in self._shortcuts:
            import logging

            logging.warning(f"Shortcut 'ctrl+e {key}' already registered, overwriting")
        self._shortcuts[key] = shortcut

    def get_shortcut(self, key: str) -> dict | None:
        """Look up a registered shortcut by its chord-tail key (``None`` if unknown)."""
        return self._shortcuts.get(key)

    def get_shortcuts(self) -> dict[str, dict]:
        """Get all registered shortcuts (chord-tail key -> shortcut def)."""
        return dict(self._shortcuts)
