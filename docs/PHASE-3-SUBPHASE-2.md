# Phase 3 Subphase 2 — Extension Loader and Registry

> **Topic**: Implement extension discovery/loading and tool/command registration.

## Scope

This subphase implements:
1. `ExtensionLoader` — discovers and loads Python extension modules from `~/.tau/extensions/` and `<cwd>/.tau/extensions/`
2. `ExtensionRegistry` — manages tool/command/flag registration and provides access to registered items

These are the infrastructure pieces that the extension system needs to function.

## Reference

- `SUBPHASE-3-SUBPHASE-0.md`: loader and registry interfaces
- `docs/extensions.md` lines 40-100: extension file structure
- `docs/tau-agent-core.md` lines 500-600: extension system design
- `docs/IMPLEMENTATION-PLAN.md` lines 250-300: loader and registry spec
- pi's `extension-runner.js` (reference)

## Implementation Outline

### `tau_agent_core/extensions/loader.py`

```python
import importlib.util
import sys
from pathlib import Path
from typing import Callable

class ExtensionLoader:
    """Discovers and loads Python extension modules."""

    EXTENSION_DIRS = [
        Path.home() / ".tau" / "extensions",
    ]

    @classmethod
    def discover(cls, cwd: str | None = None) -> list[Path]:
        """Find all extension files.

        Discovery order:
        1. Global extensions (~/.tau/extensions/)
        2. Project extensions (<cwd>/.tau/extensions/)

        Returns paths to .py files and directory-based extensions.

        Returns:
            list[Path] — paths to extension entry points
        """
        extensions = []
        for ext_dir in cls.EXTENSION_DIRS:
            if not ext_dir.exists():
                continue
            for path in sorted(ext_dir.rglob("*.py")):
                if path.name != "__init__.py":
                    extensions.append(path)
            # Directory-based extensions
            for subdir in ext_dir.iterdir():
                if subdir.is_dir() and (subdir / "__init__.py").exists():
                    extensions.append(subdir)

        # Project extensions (loaded after global)
        if cwd:
            project_ext = Path(cwd) / ".tau" / "extensions"
            if project_ext.exists():
                for path in sorted(project_ext.rglob("*.py")):
                    if path.name != "__init__.py":
                        extensions.append(path)
                for subdir in project_ext.iterdir():
                    if subdir.is_dir() and (subdir / "__init__.py").exists():
                        extensions.append(subdir)

        return extensions

    @classmethod
    def load(cls, path: Path) -> Callable | None:
        """Load an extension module and call its register() function.

        Args:
            path: Path to the extension file or directory

        Returns:
            The result of register(api) or None if loading failed.
        """
        try:
            if path.is_dir():
                module_path = path / "__init__.py"
            else:
                module_path = path

            module_name = f"tau_ext_{path.stem}_{id(path)}"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Call register function
            register_fn = getattr(module, "register", None)
            if register_fn:
                return register_fn
            return None

        except Exception as e:
            import logging
            logging.error(f"Failed to load extension {path}: {e}")
            return None
```

### `tau_agent_core/extensions/registry.py`

```python
from typing import Callable

class ToolInfo:
    """Read-only tool information."""
    def __init__(self, name: str, description: str, parameters: dict, source: str):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.source = source  # "built-in" or extension name

class ExtensionRegistry:
    """Manages tool, command, and flag registration."""

    def __init__(self):
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
```

## Done Criteria

- `ExtensionLoader.discover(cwd)` finds extensions in both global and project directories
- `ExtensionLoader.load(path)` loads a Python module and returns its `register()` function
- `ExtensionLoader.load(path)` returns None if the module has no `register()` function
- `ExtensionLoader.load(path)` logs errors but doesn't crash
- `ExtensionRegistry.register_tool()` adds a tool to the registry
- `ExtensionRegistry.get_all_tools()` returns all tools with metadata
- `ExtensionRegistry.set_active_tools()` filters tools by name
- `ExtensionRegistry.get_active_tools()` returns only active tools
- `ExtensionRegistry.register_command()` and `register_flag()` work
- `ExtensionRegistry.append_entry()` persists extension state
- Global extensions are discovered before project extensions

## Testing Strategy

### Test 1: Discover global extensions

```python
async def test_discover_global(tmp_path, monkeypatch):
    # Create a fake home directory
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".tau").mkdir()
    (fake_home / ".tau" / "extensions").mkdir()
    (fake_home / ".tau" / "extensions" / "my_ext.py").write_text("def register(api): pass")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    extensions = ExtensionLoader.discover()
    assert any("my_ext.py" in str(p) for p in extensions)
```

### Test 2: Discover project extensions

```python
async def test_discover_project(tmp_path):
    project_ext = tmp_path / ".tau" / "extensions"
    project_ext.mkdir(parents=True)
    (project_ext / "project_ext.py").write_text("def register(api): pass")

    extensions = ExtensionLoader.discover(cwd=str(tmp_path))
    assert any("project_ext.py" in str(p) for p in extensions)
```

### Test 3: Load extension module

```python
async def test_load_extension(tmp_path):
    ext_path = tmp_path / "test_ext.py"
    ext_path.write_text("""
def register(api):
    return "registered"
""")
    result = ExtensionLoader.load(ext_path)
    assert result is not None
    # Call register with a mock API
    class MockAPI:
        pass
    assert result(MockAPI()) == "registered"
```

### Test 4: Load extension without register function

```python
async def test_load_no_register(tmp_path):
    ext_path = tmp_path / "no_register.py"
    ext_path.write_text("pass")  # no register function
    result = ExtensionLoader.load(ext_path)
    assert result is None
```

### Test 5: Load fails gracefully

```python
async def test_load_fails_gracefully(tmp_path):
    ext_path = tmp_path / "broken.py"
    ext_path.write_text("import nonexistent_module_xyz")
    # Should not crash, should log error
    result = ExtensionLoader.load(ext_path)
    assert result is None
```

### Test 6: Register and query tools

```python
async def test_registry_tools():
    reg = ExtensionRegistry()
    reg.register_tool({"name": "my_tool", "description": "desc", "parameters": {}})
    tools = reg.get_all_tools()
    assert len(tools) == 1
    assert tools[0].name == "my_tool"
    assert tools[0].description == "desc"
```

### Test 7: Active tool filtering

```python
async def test_active_tool_filtering():
    reg = ExtensionRegistry()
    reg.register_tool({"name": "a", "description": "a", "parameters": {}})
    reg.register_tool({"name": "b", "description": "b", "parameters": {}})
    reg.set_active_tools(["a"])
    active = reg.get_active_tools()
    assert list(active.keys()) == ["a"]
```

### Test 8: Extension entry persistence

```python
async def test_extension_entries():
    reg = ExtensionRegistry()
    reg.append_entry("counter", {"value": 42})
    reg.append_entry("counter", {"value": 43})
    entries = reg.get_entries()
    assert len(entries) == 2
    assert entries[0]["custom_type"] == "counter"
    assert entries[0]["data"]["value"] == 42
```

### Test 9: Global extensions loaded before project

```python
async def test_discovery_order(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".tau").mkdir()
    (fake_home / ".tau" / "extensions" / "global.py").write_text("def register(api): pass")
    project_ext = tmp_path / ".tau" / "extensions"
    project_ext.mkdir()
    (project_ext / "project.py").write_text("def register(api): pass")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    extensions = ExtensionLoader.discover(cwd=str(tmp_path))
    global_idx = next(i for i, p in enumerate(extensions) if "global.py" in str(p))
    project_idx = next(i for i, p in enumerate(extensions) if "project.py" in str(p))
    assert global_idx < project_idx  # global first
```

## Success Signal

All 9 test categories pass. Extensions are discovered from both global and project directories, loaded with importlib, and errors are handled gracefully. The registry correctly manages tool/command/flag registration and filtering. Extension state persists across reloads.
