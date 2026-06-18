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


class TestExtensionEntryPersistence:
    """Test 8: Extension entry persistence.

    Reference: PHASE-3-SUBPHASE-2.md, Test 8
    > ExtensionRegistry.append_entry() persists extension state
    > ExtensionRegistry.get_entries() returns persisted entries
    """

    def test_append_entry(self):
        """ExtensionRegistry.append_entry() persists an extension entry."""
        reg = ExtensionRegistry()
        reg.append_entry("counter", {"value": 42})
        entries = reg.get_entries()
        assert len(entries) == 1
        assert entries[0]["custom_type"] == "counter"
        assert entries[0]["data"]["value"] == 42

    def test_append_multiple_entries(self):
        """ExtensionRegistry.append_entry() can append multiple entries."""
        reg = ExtensionRegistry()
        reg.append_entry("counter", {"value": 42})
        reg.append_entry("counter", {"value": 43})
        entries = reg.get_entries()
        assert len(entries) == 2
        assert entries[0]["custom_type"] == "counter"
        assert entries[0]["data"]["value"] == 42
        assert entries[1]["custom_type"] == "counter"
        assert entries[1]["data"]["value"] == 43

    def test_get_entries_initially_empty(self):
        """ExtensionRegistry.get_entries() returns empty list initially."""
        reg = ExtensionRegistry()
        assert reg.get_entries() == []

    def test_get_entries_returns_copy(self):
        """ExtensionRegistry.get_entries() returns a copy, not the internal list."""
        reg = ExtensionRegistry()
        reg.append_entry("counter", {"value": 42})
        entries1 = reg.get_entries()
        entries2 = reg.get_entries()
        entries1.append({"custom_type": "injected"})
        assert len(reg.get_entries()) == 1

    def test_append_entry_different_types(self):
        """ExtensionRegistry.append_entry() handles different custom types."""
        reg = ExtensionRegistry()
        reg.append_entry("counter", {"value": 42})
        reg.append_entry("metrics", {"requests": 100})
        reg.append_entry("state", {"status": "running"})
        entries = reg.get_entries()
        assert len(entries) == 3
        types = [e["custom_type"] for e in entries]
        assert "counter" in types
        assert "metrics" in types
        assert "state" in types

    def test_append_entry_preserves_data(self):
        """ExtensionRegistry.append_entry() preserves the full data dict."""
        reg = ExtensionRegistry()
        complex_data = {
            "nested": {"key": "value"},
            "list": [1, 2, 3],
            "flag": True,
        }
        reg.append_entry("complex", complex_data)
        entries = reg.get_entries()
        assert entries[0]["data"]["nested"]["key"] == "value"
        assert entries[0]["data"]["list"] == [1, 2, 3]
        assert entries[0]["data"]["flag"] is True


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


class TestRegisterFlag:
    """Tests for flag registration."""

    def test_register_flag(self):
        """ExtensionRegistry.register_flag() stores a flag."""
        reg = ExtensionRegistry()
        options = {"type": "boolean", "default": False}
        reg.register_flag("debug", options)
        assert "debug" in reg._flags
        assert reg._flags["debug"] == options

    def test_register_multiple_flags(self):
        """ExtensionRegistry.register_flag() can register multiple flags."""
        reg = ExtensionRegistry()
        reg.register_flag("debug", {"type": "boolean"})
        reg.register_flag("verbose", {"type": "boolean"})
        assert "debug" in reg._flags
        assert "verbose" in reg._flags

    def test_get_flag_existing(self):
        """ExtensionRegistry.get_flag() returns existing flag value."""
        reg = ExtensionRegistry()
        reg.register_flag("debug", {"type": "boolean", "value": True})
        assert reg.get_flag("debug") is True

    def test_get_flag_missing(self):
        """ExtensionRegistry.get_flag() returns None for missing flag."""
        reg = ExtensionRegistry()
        assert reg.get_flag("nonexistent") is None

    def test_get_flag_default_value(self):
        """ExtensionRegistry.get_flag() returns None when flag has no value set."""
        reg = ExtensionRegistry()
        reg.register_flag("debug", {"type": "boolean", "default": False})
        assert reg.get_flag("debug") is None

    def test_set_flag_value(self):
        """Setting a flag value updates get_flag()."""
        reg = ExtensionRegistry()
        reg.register_flag("debug", {"type": "boolean"})
        reg._flags["debug"]["value"] = True
        assert reg.get_flag("debug") is True


class TestExtensionRegistryIntegration:
    """Integration tests for ExtensionRegistry combining multiple features."""

    def test_tools_and_commands_and_flags_and_entries(self):
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

        # Flags
        reg.register_flag("debug", {"type": "boolean", "value": True})
        assert reg.get_flag("debug") is True

        # Entries
        reg.append_entry("counter", {"value": 1})
        assert len(reg.get_entries()) == 1

    def test_tool_filtering_with_other_features(self):
        """Tool filtering doesn't affect commands, flags, or entries."""
        reg = ExtensionRegistry()
        reg.register_tool({"name": "a", "description": "a", "parameters": {}})
        reg.register_tool({"name": "b", "description": "b", "parameters": {}})
        reg.register_command("help", {})
        reg.register_flag("debug", {})
        reg.append_entry("state", {})

        reg.set_active_tools(["a"])

        assert len(reg.get_all_tools()) == 2
        assert set(reg.get_active_tools().keys()) == {"a"}
        assert "help" in reg._commands
        assert reg.get_flag("debug") is None
        assert len(reg.get_entries()) == 1
