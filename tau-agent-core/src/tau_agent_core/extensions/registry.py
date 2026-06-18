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
        def register_flag(self, name: str, options: dict) -> None: ...
        def get_flag(self, name: str) -> Any: ...
        def append_entry(self, custom_type: str, data: dict) -> None: ...
        def get_entries(self) -> list[dict]: ...
"""

from __future__ import annotations

from typing import Any


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
        self._flags: dict[str, dict] = {}  # name -> flag def
        self._active_tools: set[str] | None = None  # None = all active
        self._entry_store: list[dict] = []  # extension-persisted entries

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
            result.append(ToolInfo(
                name=name,
                description=defn.get("description", ""),
                parameters=defn.get("parameters", {}),
                source=defn.get("_source", "built-in"),
            ))
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

    def register_flag(self, name: str, options: dict) -> None:
        """Register a CLI flag."""
        self._flags[name] = options

    def get_flag(self, name: str) -> Any:
        """Get the value of a CLI flag."""
        return self._flags.get(name, {}).get("value")

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist extension state (does not appear in LLM context)."""
        entry = {
            "custom_type": custom_type,
            "data": data,
        }
        self._entry_store.append(entry)

    def get_entries(self) -> list[dict]:
        """Get all persisted extension entries."""
        return list(self._entry_store)
