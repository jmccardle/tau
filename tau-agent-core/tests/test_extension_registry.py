"""Tests for tau_agent_core.extensions.registry — ExtensionRegistry and ToolInfo.

Tests verify:
- ExtensionRegistry.register_tool() adds a tool to the registry
- ExtensionRegistry.get_all_tools() returns all tools with metadata (ToolInfo)
- ExtensionRegistry.set_active_tools() filters tools by name
- ExtensionRegistry.get_active_tools() returns only active tools
- ExtensionRegistry.register_command() and register_flag() work
- ExtensionRegistry.append_entry() persists extension state
- ExtensionRegistry.get_entries() returns persisted entries

Reference: PHASE-3-SUBPHASE-2.md, Testing Strategy (Tests 6, 7, 8)
Reference: SUBPHASE-0.0.md, ExtensionRegistry contract
"""

import pytest

from tau_agent_core.extensions.registry import ExtensionRegistry, ToolInfo


class TestToolInfo:
    """Tests for the ToolInfo data class."""

    def test_tool_info_creation(self):
        """ToolInfo can be instantiated with all fields."""
        info = ToolInfo(
            name="my_tool",
            description="A test tool",
            parameters={"type": "object"},
            source="built-in",
        )
        assert info.name == "my_tool"
        assert info.description == "A test tool"
        assert info.parameters == {"type": "object"}
        assert info.source == "built-in"

    def test_tool_info_default_source(self):
        """ToolInfo source defaults to 'built-in' when not set externally."""
        info = ToolInfo(
            name="tool",
            description="desc",
            parameters={},
            source="my_extension",
        )
        assert info.source == "my_extension"

    def test_tool_info_repr(self):
        """ToolInfo has a readable __repr__."""
        info = ToolInfo(name="ls", description="", parameters={}, source="built-in")
        assert "ls" in repr(info)
        assert "built-in" in repr(info)

    def test_tool_info_immutability(self):
        """ToolInfo fields can be set but are not inherently frozen (dict-like)."""
        info = ToolInfo(name="old", description="", parameters={}, source="built-in")
        info.name = "new"
        assert info.name == "new"


class TestRegisterAndQueryTools:
    """Test 6: Register and query tools.

    Reference: PHASE-3-SUBPHASE-2.md, Test 6
    > ExtensionRegistry.register_tool() adds a tool to the registry
    > ExtensionRegistry.get_all_tools() returns all tools with metadata
    """

    def test_registry_creation(self):
        """ExtensionRegistry can be instantiated."""
        reg = ExtensionRegistry()
        assert reg is not None

    def test_register_single_tool(self):
        """ExtensionRegistry.register_tool() adds a single tool."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
        tools = reg.get_all_tools()
        assert len(tools) == 1

    def test_register_tool_returns_tool_info(self):
        """ExtensionRegistry.get_all_tools() returns ToolInfo objects."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
        tools = reg.get_all_tools()
        assert isinstance(tools[0], ToolInfo)

    def test_register_tool_name(self):
        """ToolInfo.name matches the registered tool name."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
        tools = reg.get_all_tools()
        assert tools[0].name == "my_tool"

    def test_register_tool_description(self):
        """ToolInfo.description matches the registered tool description."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
        tools = reg.get_all_tools()
        assert tools[0].description == "desc"

    def test_register_tool_parameters(self):
        """ToolInfo.parameters matches the registered tool parameters."""
        reg = ExtensionRegistry()
        params = {"type": "object", "properties": {"path": {"type": "string"}}}
        reg.register_tool({"name": "ls", "description": "List files", "parameters": params})
        tools = reg.get_all_tools()
        assert tools[0].parameters == params

    def test_register_tool_default_source(self):
        """ToolInfo.source defaults to 'built-in' when no _source is provided."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
        tools = reg.get_all_tools()
        assert tools[0].source == "built-in"

    def test_register_tool_custom_source(self):
        """ToolInfo.source uses _source when provided in definition."""
        reg = ExtensionRegistry()
        reg.register_tool({
            "name": "ext_tool",
            "description": "From extension",
            "parameters": {},
            "_source": "my_extension",
        })
        tools = reg.get_all_tools()
        assert tools[0].source == "my_extension"

    def test_register_multiple_tools(self):
        """ExtensionRegistry.register_tool() can register multiple tools."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "ls", "description": "List", "parameters": {}})
        reg.register_tool({"name": "grep", "description": "Search", "parameters": {}})
        reg.register_tool({"name": "find", "description": "Find", "parameters": {}})
        tools = reg.get_all_tools()
        assert len(tools) == 3
        names = [t.name for t in tools]
        assert "ls" in names
        assert "grep" in names
        assert "find" in names

    def test_get_all_tools_empty(self):
        """ExtensionRegistry.get_all_tools() returns empty list initially."""
        reg = ExtensionRegistry()
        assert reg.get_all_tools() == []

    def test_get_all_tools_default_parameters(self):
        """ToolInfo.parameters defaults to {} when not provided."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "simple", "description": "simple tool"})
        tools = reg.get_all_tools()
        assert tools[0].parameters == {}

    def test_get_all_tools_default_description(self):
        """ToolInfo.description defaults to '' when not provided."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "simple", "parameters": {}})
        tools = reg.get_all_tools()
        assert tools[0].description == ""

    def test_register_overwrites_existing_tool(self):
        """ExtensionRegistry.register_tool() overwrites existing tool with same name."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "tool", "description": "first", "parameters": {}})
        reg.register_tool({"name": "tool", "description": "second", "parameters": {}})
        tools = reg.get_all_tools()
        assert len(tools) == 1
        assert tools[0].description == "second"


class TestActiveToolFiltering:
    """Test 7: Active tool filtering.

    Reference: PHASE-3-SUBPHASE-2.md, Test 7
    > ExtensionRegistry.set_active_tools() filters tools by name
    > ExtensionRegistry.get_active_tools() returns only active tools
    """

    def test_get_active_tools_all_active(self):
        """get_active_tools() returns all tools when none are set as active."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        active = reg.get_active_tools()
        assert set(active.keys()) == {"a", "b"}

    def test_set_active_tools_single(self):
        """set_active_tools() filters to a single active tool."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.set_active_tools(["a"])
        active = reg.get_active_tools()
        assert list(active.keys()) == ["a"]

    def test_set_active_tools_multiple(self):
        """set_active_tools() filters to multiple active tools."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.register_tool({"name": "c", "description": "c", "parameters": {}})
        reg.set_active_tools(["a", "c"])
        active = reg.get_active_tools()
        assert set(active.keys()) == {"a", "c"}

    def test_set_active_tools_empty(self):
        """set_active_tools() with empty list activates no tools."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.set_active_tools([])
        active = reg.get_active_tools()
        assert active == {}

    def test_set_active_tools_ignores_missing(self):
        """set_active_tools() ignores tool names that don't exist in registry."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.set_active_tools(["a", "nonexistent"])
        active = reg.get_active_tools()
        assert set(active.keys()) == {"a"}

    def test_set_active_tools_all(self):
        """set_active_tools() can activate all registered tools."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.set_active_tools(["a", "b"])
        active = reg.get_active_tools()
        assert set(active.keys()) == {"a", "b"}

    def test_get_active_tools_returns_dict(self):
        """get_active_tools() returns a dict of name -> definition."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "tool", "description": "desc", "parameters": {}})
        active = reg.get_active_tools()
        assert isinstance(active, dict)
        assert "tool" in active
        assert active["tool"]["name"] == "tool"

    def test_get_all_tools_after_deselect_all(self):
        """After setting active tools to empty, get_active_tools returns empty dict."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.set_active_tools([])
        active = reg.get_active_tools()
        assert active == {}


class TestEntryStoreRemoved:
    """The RAM-only ``_entry_store`` was removed in E6 §2 / S39 (G4).

    ``append_entry`` no longer lives on the registry — durable extension state is
    persisted onto the session tree as a ``customEntry`` node via
    ``AgentSession._append_custom_entry`` / ``ExtensionAPI.append_entry`` (was
    lost on restart before). See ``test_append_entry_durable.py``.
    """

    def test_registry_has_no_append_entry(self):
        assert not hasattr(ExtensionRegistry(), "append_entry")

    def test_registry_has_no_get_entries(self):
        assert not hasattr(ExtensionRegistry(), "get_entries")

    def test_registry_has_no_entry_store(self):
        assert not hasattr(ExtensionRegistry(), "_entry_store")


class TestRegisterCommand:
    """Tests for command registration."""

    def test_register_command(self):
        """ExtensionRegistry.register_command() stores a command."""
        reg = ExtensionRegistry()
        cmd = {"action": "help", "description": "Show help"}
        reg.register_command("help", cmd)
        assert "help" in reg._commands
        assert reg._commands["help"] == cmd

    def test_register_multiple_commands(self):
        """ExtensionRegistry.register_command() can register multiple commands."""
        reg = ExtensionRegistry()
        reg.register_command("help", {"action": "help"})
        reg.register_command("status", {"action": "status"})
        reg.register_command("reset", {"action": "reset"})
        assert len(reg._commands) == 3
        assert "help" in reg._commands
        assert "status" in reg._commands
        assert "reset" in reg._commands

    def test_register_command_overwrites(self):
        """ExtensionRegistry.register_command() overwrites existing command."""
        reg = ExtensionRegistry()
        reg.register_command("help", {"action": "old"})
        reg.register_command("help", {"action": "new"})
        assert reg._commands["help"]["action"] == "new"


class TestFlagsRemoved:
    """``register_flag`` / ``get_flag`` were deleted in E6 §2 / S38 (G6)."""

    def test_registry_has_no_register_flag(self):
        assert not hasattr(ExtensionRegistry(), "register_flag")

    def test_registry_has_no_get_flag(self):
        assert not hasattr(ExtensionRegistry(), "get_flag")

    def test_registry_has_no_flags_store(self):
        assert not hasattr(ExtensionRegistry(), "_flags")


class TestExtensionRegistryIntegration:
    """Integration tests for ExtensionRegistry combining multiple features."""

    def test_tools_and_commands(self):
        """All registry features work together."""
        reg = ExtensionRegistry()

        # Tools
        reg.register_tool({"name": "ls", "description": "List files", "parameters": {}})
        tools = reg.get_all_tools()
        assert len(tools) == 1
        assert tools[0].name == "ls"

        # Commands
        reg.register_command("help", {"action": "help"})
        assert "help" in reg._commands

    def test_tool_filtering_with_other_features(self):
        """Tool filtering doesn't affect commands."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.register_command("help", {})

        reg.set_active_tools(["a"])

        assert len(reg.get_all_tools()) == 2
        assert set(reg.get_active_tools().keys()) == {"a"}
        assert "help" in reg._commands
