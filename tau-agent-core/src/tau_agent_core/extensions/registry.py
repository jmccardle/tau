"""τ-agent-core extensions registry — manages tool registration for extensions.

Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.

Contract:
    class ExtensionRegistry:
        def register_tool(self, definition: dict) -> None: ...
        def get_all_tools(self) -> list[ToolInfo]: ...
"""

from __future__ import annotations

from typing import Any


class ExtensionRegistry:
    """Registry for extension tools and capabilities.

    Manages the collection of tools provided by loaded extensions.
    Extensions register their tools with the registry, and the
    agent loop queries the registry for available tools.

    Reference: PHASE-3-SUBPHASE-0.md ExtensionRegistry contract.
    """

    def __init__(self) -> None:
        """Initialize the registry with empty tool collections."""
        self._tools: dict[str, dict[str, Any]] = {}
        self._active_tools: list[str] = []
        self._extension_tools: dict[str, list[str]] = {}

    def register_tool(self, definition: dict[str, Any]) -> None:
        """Register a tool definition.

        Args:
            definition: Tool definition dict. Must contain at least
                       a 'name' key.

        Raises:
            ValueError: If definition is missing required fields.
        """
        if "name" not in definition:
            raise ValueError("Tool definition must contain a 'name' field")

        name = definition["name"]
        self._tools[name] = definition

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all registered tool definitions.

        Returns:
            List of all tool definitions.
        """
        return list(self._tools.values())

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """Get a specific tool by name.

        Args:
            name: Tool name.

        Returns:
            Tool definition if found, None otherwise.
        """
        return self._tools.get(name)

    def unregister_tool(self, name: str) -> bool:
        """Unregister a tool by name.

        Args:
            name: Tool name to remove.

        Returns:
            True if tool was found and removed, False otherwise.
        """
        if name in self._tools:
            del self._tools[name]
            if name in self._active_tools:
                self._active_tools.remove(name)
            return True
        return False

    def set_active_tools(self, names: list[str]) -> None:
        """Set the list of active tool names.

        Only tools in this list will be available during execution.

        Args:
            names: List of active tool names.
        """
        self._active_tools = names

    def get_active_tools(self) -> list[dict[str, Any]]:
        """Get only the active tool definitions.

        Returns:
            List of active tool definitions.
        """
        return [self._tools[name] for name in self._active_tools if name in self._tools]

    def register_tool_from_extension(self, extension_name: str, definition: dict[str, Any]) -> None:
        """Register a tool belonging to a specific extension.

        Args:
            extension_name: Name of the extension.
            definition: Tool definition dict.
        """
        self.register_tool(definition)
        if extension_name not in self._extension_tools:
            self._extension_tools[extension_name] = []
        self._extension_tools[extension_name].append(definition["name"])

    def get_extension_tools(self, extension_name: str) -> list[dict[str, Any]]:
        """Get all tools registered by a specific extension.

        Args:
            extension_name: Name of the extension.

        Returns:
            List of tool definitions for the extension.
        """
        tool_names = self._extension_tools.get(extension_name, [])
        return [self._tools[name] for name in tool_names if name in self._tools]

    def get_tool_count(self) -> int:
        """Get the total number of registered tools.

        Returns:
            Number of registered tools.
        """
        return len(self._tools)

    def get_extension_count(self) -> int:
        """Get the number of extensions that have registered tools.

        Returns:
            Number of extensions with registered tools.
        """
        return len(self._extension_tools)

    def clear(self) -> None:
        """Clear all registered tools and extensions."""
        self._tools.clear()
        self._active_tools.clear()
        self._extension_tools.clear()
