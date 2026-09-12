"""Tests for the single extension loader — tau_agent_core.sdk._load_extensions.

The E0/S1 loader replaces the two contradictory loaders (the dead
``extensions/loader.py`` ``ExtensionLoader`` class and the ``sdk`` ``extend(api)``
thunk path). Verified behaviours (docs/EXTENSIONS-IMPLEMENTATION.md E0.1/§8 S1):

- Verb ``register(api)`` is INVOKED (not returned un-called).
- An ``async def register`` is awaited.
- Discovery = global dir + explicit paths only; ``discover=False`` (``-ne``)
  suppresses discovery but still loads explicit ``-e`` paths.
- A broken *discovered* extension is collected into ``errors`` while the others
  still load; a broken *explicit* extension RAISES.
- Paths are deduped by resolved path, first-wins.

Reference: pi loader.ts; LoadExtensionsResult = pi types.ts:1590.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.sdk import (
    ExtensionLoadError,
    LoadedExtension,
    LoadExtensionsResult,
    _discover_extension_paths,
    _load_extensions,
)

_SYNC_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    api.register_tool({
        "name": "%s",
        "description": "sync tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
"""

# An async register() — only awaiting it runs the body and registers the tool.
_ASYNC_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

async def register(api):
    api.register_tool({
        "name": "%s",
        "description": "async tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
"""

_BROKEN_EXT = "raise RuntimeError('boom during import')\n"

_NO_REGISTER_EXT = "def something_else(api):\n    pass\n"

# Names its tool from a module beside it, so the tool name IS the import's result.
_SIBLING_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    api.register_tool({
        "name": %s.TOOL_NAME,
        "description": "sibling tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
"""


def _write(path: Path, name: str, template: str = _SYNC_EXT) -> Path:
    path.write_text(template % name)
    return path


def _tool_names(loaded: LoadedExtension) -> list[str]:
    return [t.name for t in loaded.api.get_all_tools()]


class TestRegisterInvoked:
    async def test_register_is_invoked_not_returned(self, tmp_path):
        """register(api) is CALLED with a live api — its side effect is visible."""
        ext = _write(tmp_path / "ext.py", "sync_tool")

        result = await _load_extensions([str(ext)], discover=False)

        assert len(result.extensions) == 1
        loaded = result.extensions[0]
        # The register function ran and registered the tool against the api.
        assert "sync_tool" in _tool_names(loaded)
        # And the loader hands back the callable + the api it ran against.
        assert callable(loaded.register)
        assert isinstance(loaded.api, ExtensionAPI)

    async def test_async_register_is_awaited(self, tmp_path):
        """An async register() is awaited — the tool only appears if it ran."""
        ext = _write(tmp_path / "aext.py", "async_tool", template=_ASYNC_EXT)

        result = await _load_extensions([str(ext)], discover=False)

        assert len(result.extensions) == 1
        assert "async_tool" in _tool_names(result.extensions[0])

    async def test_api_factory_is_used_per_extension(self, tmp_path):
        """Each extension is invoked against a fresh api from the factory."""
        ext_a = _write(tmp_path / "a.py", "tool_a")
        ext_b = _write(tmp_path / "b.py", "tool_b")
        made: list[ExtensionAPI] = []
        paths: list[str] = []

        def factory(path: str) -> ExtensionAPI:
            api = ExtensionAPI()
            made.append(api)
            paths.append(path)
            return api

        result = await _load_extensions(
            [str(ext_a), str(ext_b)], discover=False, api_factory=factory
        )

        assert len(result.extensions) == 2
        assert len(made) == 2
        assert made[0] is not made[1]
        # The factory is path-aware (S24): each api is keyed by its file path.
        assert paths == [str(ext_a), str(ext_b)]


class TestDiscoveryAndExplicit:
    async def test_discovered_and_explicit_both_load(self, tmp_path):
        """A global-dir extension AND an explicit -e path both load."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "discovered.py", "discovered_tool")

        explicit = _write(tmp_path / "explicit.py", "explicit_tool")

        result = await _load_extensions([str(explicit)], discover=True, user_dir=str(global_dir))

        names = {t for loaded in result.extensions for t in _tool_names(loaded)}
        assert names == {"discovered_tool", "explicit_tool"}
        assert result.errors == []

    async def test_no_extensions_suppresses_discovery_keeps_explicit(self, tmp_path):
        """discover=False (-ne) drops discovery but still loads explicit -e."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "discovered.py", "discovered_tool")

        explicit = _write(tmp_path / "explicit.py", "explicit_tool")

        result = await _load_extensions([str(explicit)], discover=False, user_dir=str(global_dir))

        names = {t for loaded in result.extensions for t in _tool_names(loaded)}
        assert names == {"explicit_tool"}
        assert "discovered_tool" not in names

    async def test_discovery_off_with_no_explicit_loads_nothing(self, tmp_path):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "discovered.py", "discovered_tool")

        result = await _load_extensions(None, discover=False, user_dir=str(global_dir))

        assert result.extensions == []
        assert result.errors == []

    async def test_missing_global_dir_is_empty(self, tmp_path):
        result = await _load_extensions(
            None, discover=True, user_dir=str(tmp_path / "does-not-exist")
        )
        assert result.extensions == []
        assert result.errors == []

    async def test_package_dir_extension_discovered(self, tmp_path):
        """A package dir (subdir with __init__.py) is a valid entry point."""
        global_dir = tmp_path / "global"
        pkg = global_dir / "mypkg"
        pkg.mkdir(parents=True)
        _write(pkg / "__init__.py", "pkg_tool")

        result = await _load_extensions(None, discover=True, user_dir=str(global_dir))

        assert len(result.extensions) == 1
        assert "pkg_tool" in _tool_names(result.extensions[0])


class TestSiblingImports:
    """An extension can import the modules sitting beside it.

    The loader puts the module's own directory on ``sys.path`` for the length of
    the ``exec`` and pops it after. These tests never chdir into that directory
    and assert it is absent from ``sys.path`` beforehand, because the behaviour
    under test is the loader's and a cwd coincidence would pass just as green.
    """

    async def test_single_file_extension_imports_its_sibling(self, tmp_path):
        home = tmp_path / "proj"
        home.mkdir()
        (home / "helper.py").write_text("TOOL_NAME = 'sibling_reached'\n")
        (home / "ext.py").write_text("import helper\n" + _SIBLING_EXT % "helper")
        assert str(home) not in sys.path, "the test would pass without the loader doing anything"

        result = await _load_extensions([str(home / "ext.py")], discover=False)

        assert "sibling_reached" in _tool_names(result.extensions[0])

    async def test_a_package_reaches_its_own_submodule_absolutely(self, tmp_path):
        """``from .inner`` already worked; ``import inner`` now works too."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "inner.py").write_text("TOOL_NAME = 'inner_reached'\n")
        (pkg / "__init__.py").write_text("import inner\n" + _SIBLING_EXT % "inner")

        result = await _load_extensions([str(pkg)], discover=False)

        assert "inner_reached" in _tool_names(result.extensions[0])

    async def test_the_directory_does_not_outlive_the_import(self, tmp_path):
        before = list(sys.path)
        ext = _write(tmp_path / "ext.py", "tool_a")

        await _load_extensions([str(ext)], discover=False)

        assert sys.path == before

    async def test_the_directory_does_not_outlive_a_failed_import(self, tmp_path):
        """The pop is in a ``finally``: a raising import must not leak the entry."""
        before = list(sys.path)
        ext = tmp_path / "boom.py"
        ext.write_text(_BROKEN_EXT)

        with pytest.raises(RuntimeError):
            await _load_extensions([str(ext)], discover=False)

        assert sys.path == before

    async def test_an_import_deferred_into_register_still_fails(self, tmp_path):
        """The window is the import and not the whole load — stated, so it is checked.

        Widening it over ``register(api)`` would put an ``await`` inside the
        window, and two sessions load extensions in one process.
        """
        home = tmp_path / "proj"
        home.mkdir()
        (home / "late.py").write_text("VALUE = 'late'\n")
        ext = home / "ext.py"
        ext.write_text("def register(api):\n    import late\n")

        with pytest.raises(ModuleNotFoundError, match="late"):
            await _load_extensions([str(ext)], discover=False)


class TestErrorPolicy:
    async def test_broken_discovered_collected_others_load(self, tmp_path):
        """A broken discovered ext -> errors[]; the good ones still load."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        _write(global_dir / "good_a.py", "good_a")
        (global_dir / "broken.py").write_text(_BROKEN_EXT)
        _write(global_dir / "good_b.py", "good_b")

        result = await _load_extensions(None, discover=True, user_dir=str(global_dir))

        names = {t for loaded in result.extensions for t in _tool_names(loaded)}
        assert names == {"good_a", "good_b"}
        assert len(result.errors) == 1
        assert isinstance(result.errors[0], ExtensionLoadError)
        assert result.errors[0].path.endswith("broken.py")
        assert "boom" in result.errors[0].error

    async def test_discovered_without_register_collected(self, tmp_path):
        """A discovered module missing register() is an error, not a crash."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "noreg.py").write_text(_NO_REGISTER_EXT)
        _write(global_dir / "good.py", "good_tool")

        result = await _load_extensions(None, discover=True, user_dir=str(global_dir))

        assert {t for loaded in result.extensions for t in _tool_names(loaded)} == {"good_tool"}
        assert len(result.errors) == 1
        assert result.errors[0].path.endswith("noreg.py")

    async def test_broken_explicit_raises(self, tmp_path):
        """A broken explicit -e extension RAISES (the user named it)."""
        broken = tmp_path / "broken.py"
        broken.write_text(_BROKEN_EXT)

        with pytest.raises(RuntimeError, match="boom"):
            await _load_extensions([str(broken)], discover=False)

    async def test_explicit_without_register_raises(self, tmp_path):
        noreg = tmp_path / "noreg.py"
        noreg.write_text(_NO_REGISTER_EXT)

        with pytest.raises(AttributeError, match="register"):
            await _load_extensions([str(noreg)], discover=False)

    async def test_missing_explicit_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            await _load_extensions([str(tmp_path / "nope.py")], discover=False)

    async def test_broken_explicit_collected_when_opted_in(self, tmp_path):
        """collect_explicit_errors: a broken -e -> errors[]; the good -e still loads."""
        good = _write(tmp_path / "good.py", "good_tool")
        broken = tmp_path / "broken.py"
        broken.write_text(_BROKEN_EXT)

        result = await _load_extensions(
            [str(good), str(broken)], discover=False, collect_explicit_errors=True
        )

        assert {t for loaded in result.extensions for t in _tool_names(loaded)} == {"good_tool"}
        assert len(result.errors) == 1
        assert result.errors[0].path.endswith("broken.py")
        assert "boom" in result.errors[0].error

    async def test_explicit_without_register_collected_when_opted_in(self, tmp_path):
        """collect_explicit_errors also demotes a missing-register() explicit failure."""
        noreg = tmp_path / "noreg.py"
        noreg.write_text(_NO_REGISTER_EXT)

        result = await _load_extensions([str(noreg)], discover=False, collect_explicit_errors=True)

        assert result.extensions == []
        assert len(result.errors) == 1
        assert result.errors[0].path.endswith("noreg.py")


class TestDedup:
    async def test_dedup_explicit_duplicate(self, tmp_path):
        ext = _write(tmp_path / "ext.py", "dup_tool")

        result = await _load_extensions([str(ext), str(ext)], discover=False)

        assert len(result.extensions) == 1

    async def test_dedup_across_discovery_and_explicit(self, tmp_path):
        """A path both discovered and passed as -e loads once (first-wins)."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        ext = _write(global_dir / "shared.py", "shared_tool")

        result = await _load_extensions([str(ext)], discover=True, user_dir=str(global_dir))

        assert len(result.extensions) == 1
        assert "shared_tool" in _tool_names(result.extensions[0])

    async def test_dedup_normalizes_relative_path(self, tmp_path):
        ext = _write(tmp_path / "ext.py", "rel_tool")
        weird = str(tmp_path / "sub" / ".." / "ext.py")

        result = await _load_extensions([str(ext), weird], discover=False)

        assert len(result.extensions) == 1


class TestResultShape:
    async def test_returns_load_extensions_result(self, tmp_path):
        result = await _load_extensions(None, discover=False)
        assert isinstance(result, LoadExtensionsResult)
        assert result.extensions == []
        assert result.errors == []

    def test_discover_helper_finds_files_and_packages(self, tmp_path):
        _write(tmp_path / "a.py", "a")
        (tmp_path / "__init__.py").write_text("")  # skipped
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (tmp_path / "notpy.txt").write_text("nope")
        plain = tmp_path / "plaindir"
        plain.mkdir()  # no __init__.py -> skipped

        found = {p.name for p in _discover_extension_paths(str(tmp_path))}
        assert found == {"a.py", "pkg"}
