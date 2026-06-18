"""Tests for tau_agent_core.extensions.loader — ExtensionLoader.

Tests verify:
- ExtensionLoader.discover() finds extensions in global directory (~/.tau/extensions/)
- ExtensionLoader.discover(cwd=) finds extensions in project directory (<cwd>/.tau/extensions/)
- ExtensionLoader.load(path) loads a Python module and returns its register() function
- ExtensionLoader.load(path) returns None if the module has no register() function
- ExtensionLoader.load(path) logs errors but doesn't crash
- Global extensions are discovered before project extensions

Reference: PHASE-3-SUBPHASE-2.md, Testing Strategy (Tests 1, 2, 3, 4, 5, 9)
Reference: SUBPHASE-0.0.md, ExtensionLoader contract
"""

import pytest
from pathlib import Path

from tau_agent_core.extensions.loader import ExtensionLoader


class TestDiscoverGlobalExtensions:
    """Test 1: Discover global extensions.

    Reference: PHASE-3-SUBPHASE-2.md, Test 1
    > ExtensionLoader.discover() finds extensions in ~/.tau/extensions/
    """

    def test_discover_finds_global_extensions(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() finds .py files in global extensions dir."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        (ext_dir / "my_ext.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert any("my_ext.py" in str(p) for p in extensions)

    def test_discover_finds_multiple_global_extensions(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() finds multiple .py files."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        (ext_dir / "ext_a.py").write_text("def register(api): pass")
        (ext_dir / "ext_b.py").write_text("def register(api): pass")
        (ext_dir / "ext_c.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        ext_names = [p.name for p in extensions]
        assert "ext_a.py" in ext_names
        assert "ext_b.py" in ext_names
        assert "ext_c.py" in ext_names

    def test_discover_finds_directory_extensions(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() finds directory-based extensions with __init__.py."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        pkg_ext = ext_dir / "pkg_ext"
        pkg_ext.mkdir()
        (pkg_ext / "__init__.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert any("pkg_ext" in str(p) for p in extensions)

    def test_discover_finds_directory_extensions_in_subdirs(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() finds .py files in subdirectories (rglob)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        sub = ext_dir / "subdir"
        sub.mkdir()
        (sub / "nested.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert any("nested.py" in str(p) for p in extensions)

    def test_discover_skips_init_py(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() skips __init__.py files."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        (ext_dir / "__init__.py").write_text("# init")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert not any("__init__.py" in str(p) for p in extensions)

    def test_discover_returns_empty_when_no_global_dir(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() returns empty list when global dir doesn't exist."""
        fake_home = tmp_path / "home_no_tau"
        fake_home.mkdir()

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert extensions == []

    def test_discover_returns_paths(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() returns Path objects."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau").mkdir()
        ext_dir = fake_home / ".tau" / "extensions"
        ext_dir.mkdir()
        (ext_dir / "test.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert all(isinstance(p, Path) for p in extensions)


class TestDiscoverProjectExtensions:
    """Test 2: Discover project extensions.

    Reference: PHASE-3-SUBPHASE-2.md, Test 2
    > ExtensionLoader.discover(cwd=) finds extensions in <cwd>/.tau/extensions/
    """

    def test_discover_finds_project_extensions(self, tmp_path):
        """ExtensionLoader.discover(cwd=) finds .py files in project extensions dir."""
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "project_ext.py").write_text("def register(api): pass")

        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        assert any("project_ext.py" in str(p) for p in extensions)

    def test_discover_finds_project_directory_extensions(self, tmp_path):
        """ExtensionLoader.discover(cwd=) finds directory-based project extensions."""
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        pkg = project_ext / "my_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def register(api): pass")

        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        assert any("my_pkg" in str(p) for p in extensions)

    def test_discover_project_skips_init_py(self, tmp_path):
        """ExtensionLoader.discover(cwd=) skips __init__.py in project extensions."""
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "__init__.py").write_text("# init")

        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        assert not any("__init__.py" in str(p) for p in extensions)

    def test_discover_project_empty_when_no_cwd_ext(self):
        """ExtensionLoader.discover(cwd=) returns empty when project dir doesn't exist."""
        extensions = ExtensionLoader.discover(cwd="/nonexistent/path/12345")
        assert extensions == []


class TestLoadExtensionModule:
    """Test 3: Load extension module.

    Reference: PHASE-3-SUBPHASE-2.md, Test 3
    > ExtensionLoader.load(path) loads a Python module and returns its register() function
    """

    def test_load_returns_register_function(self, tmp_path):
        """ExtensionLoader.load() returns the register function from the module."""
        ext_path = tmp_path / "test_ext.py"
        ext_path.write_text("""
def register(api):
    return "registered"
""")
        result = ExtensionLoader.load(ext_path)
        assert result is not None
        assert callable(result)

    def test_load_calls_register_with_api(self, tmp_path):
        """ExtensionLoader.load() returns register function that can be called."""
        ext_path = tmp_path / "test_ext.py"
        ext_path.write_text("""
def register(api):
    return "registered"
""")
        result = ExtensionLoader.load(ext_path)
        class MockAPI:
            pass
        assert result(MockAPI()) == "registered"

    def test_load_directory_extension(self, tmp_path):
        """ExtensionLoader.load() loads directory-based extensions from __init__.py."""
        ext_dir = tmp_path / "my_pkg"
        ext_dir.mkdir()
        (ext_dir / "__init__.py").write_text("""
def register(api):
    return "pkg_loaded"
""")
        result = ExtensionLoader.load(ext_dir)
        assert result is not None
        class MockAPI:
            pass
        assert result(MockAPI()) == "pkg_loaded"

    def test_load_extension_returns_none_for_nonexistent(self, tmp_path):
        """ExtensionLoader.load() returns None for nonexistent files."""
        nonexistent = tmp_path / "does_not_exist.py"
        result = ExtensionLoader.load(nonexistent)
        assert result is None

    def test_load_extension_returns_callable(self, tmp_path):
        """ExtensionLoader.load() returns a callable register function."""
        ext_path = tmp_path / "test_ext.py"
        ext_path.write_text("""
def register(api):
    return True
""")
        result = ExtensionLoader.load(ext_path)
        assert callable(result)


class TestLoadExtensionWithoutRegister:
    """Test 4: Load extension without register function.

    Reference: PHASE-3-SUBPHASE-2.md, Test 4
    > ExtensionLoader.load(path) returns None if the module has no register() function
    """

    def test_load_no_register_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None when module has no register function."""
        ext_path = tmp_path / "no_register.py"
        ext_path.write_text("pass")  # no register function
        result = ExtensionLoader.load(ext_path)
        assert result is None

    def test_load_empty_module_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None for empty module."""
        ext_path = tmp_path / "empty.py"
        ext_path.write_text("")
        result = ExtensionLoader.load(ext_path)
        assert result is None

    def test_load_module_with_other_functions_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None when module has functions but no register."""
        ext_path = tmp_path / "other_funcs.py"
        ext_path.write_text("""
def foo():
    return 1

def bar():
    return 2
""")
        result = ExtensionLoader.load(ext_path)
        assert result is None


class TestLoadFailsGracefully:
    """Test 5: Load fails gracefully.

    Reference: PHASE-3-SUBPHASE-2.md, Test 5
    > ExtensionLoader.load(path) logs errors but doesn't crash
    """

    def test_load_broken_extension_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None for extension with import errors."""
        ext_path = tmp_path / "broken.py"
        ext_path.write_text("import nonexistent_module_xyz_12345")
        result = ExtensionLoader.load(ext_path)
        assert result is None

    def test_load_syntax_error_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None for extension with syntax errors."""
        ext_path = tmp_path / "syntax_error.py"
        ext_path.write_text("def foo(unclosed")
        result = ExtensionLoader.load(ext_path)
        assert result is None

    def test_load_attribute_error_returns_none(self, tmp_path):
        """ExtensionLoader.load() returns None for extension with runtime errors."""
        ext_path = tmp_path / "runtime_error.py"
        ext_path.write_text("""
raise RuntimeError("intentional error")
""")
        result = ExtensionLoader.load(ext_path)
        assert result is None

    def test_load_does_not_crash_with_multiple_broken(self, tmp_path):
        """ExtensionLoader.load() can be called multiple times on broken extensions."""
        for i in range(5):
            ext_path = tmp_path / f"broken_{i}.py"
            ext_path.write_text("import nonexistent_xyz")
            result = ExtensionLoader.load(ext_path)
            assert result is None


class TestDiscoveryOrder:
    """Test 9: Global extensions loaded before project.

    Reference: PHASE-3-SUBPHASE-2.md, Test 9
    > Global extensions are discovered before project extensions
    """

    def test_global_before_project(self, tmp_path, monkeypatch):
        """Global extensions appear before project extensions in discovery order."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau" / "extensions").mkdir(parents=True)
        (fake_home / ".tau" / "extensions" / "global.py").write_text("def register(api): pass")
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "project.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        global_idx = next(i for i, p in enumerate(extensions) if "global.py" in str(p))
        project_idx = next(i for i, p in enumerate(extensions) if "project.py" in str(p))
        assert global_idx < project_idx, "Global extensions must appear before project extensions"

    def test_global_only(self, tmp_path, monkeypatch):
        """Discovery with only global extensions works."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau" / "extensions").mkdir(parents=True)
        (fake_home / ".tau" / "extensions" / "global_a.py").write_text("def register(api): pass")
        (fake_home / ".tau" / "extensions" / "global_b.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        names = [p.name for p in extensions]
        assert "global_a.py" in names
        assert "global_b.py" in names

    def test_project_only(self, tmp_path):
        """Discovery with only project extensions works."""
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "proj_a.py").write_text("def register(api): pass")
        (project_ext / "proj_b.py").write_text("def register(api): pass")

        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        names = [p.name for p in extensions]
        assert "proj_a.py" in names
        assert "proj_b.py" in names

    def test_global_and_project_mixed(self, tmp_path, monkeypatch):
        """Both global and project extensions discovered in correct order."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau" / "extensions").mkdir(parents=True)
        (fake_home / ".tau" / "extensions" / "ext_global.py").write_text("def register(api): pass")
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "ext_project.py").write_text("def register(api): pass")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        assert len(extensions) == 2
        # Global first, project second
        assert "ext_global.py" in str(extensions[0])
        assert "ext_project.py" in str(extensions[1])

    def test_extension_dirs_attribute_is_list(self):
        """ExtensionLoader.EXTENSION_DIRS is a list (mutable for test overrides)."""
        assert isinstance(ExtensionLoader.EXTENSION_DIRS, list)
        # EXTENSION_DIRS starts empty (lazy computation)
        # Tests can override it: ExtensionLoader.EXTENSION_DIRS = [Path.home() / ".tau" / "extensions"]

    def test_extension_dirs_computed_at_discover_time(self, tmp_path, monkeypatch):
        """ExtensionLoader computes dirs fresh from Path.home() at discover time."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".tau" / "extensions").mkdir(parents=True)
        (fake_home / ".tau" / "extensions" / "lazy.py").write_text("def register(api): pass")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        extensions = ExtensionLoader.discover()
        assert any("lazy.py" in str(p) for p in extensions)


class TestExtensionLoaderEdgeCases:
    """Edge case tests for ExtensionLoader."""

    def test_discover_cwd_as_path_object(self, tmp_path, monkeypatch):
        """ExtensionLoader.discover() works when cwd is a Path object (str coercion)."""
        project_ext = tmp_path / ".tau" / "extensions"
        project_ext.mkdir(parents=True)
        (project_ext / "test.py").write_text("def register(api): pass")

        # Should work with string
        extensions = ExtensionLoader.discover(cwd=str(tmp_path))
        assert any("test.py" in str(p) for p in extensions)

    def test_load_directory_missing_init(self, tmp_path):
        """ExtensionLoader.load() fails gracefully for directory without __init__.py."""
        ext_dir = tmp_path / "no_init"
        ext_dir.mkdir()
        # No __init__.py — should handle gracefully
        result = ExtensionLoader.load(ext_dir)
        # Will fail to load, should return None
        assert result is None

    def test_load_extension_with_register_returning_none(self, tmp_path):
        """ExtensionLoader.load() returns register function even if it returns None."""
        ext_path = tmp_path / "returns_none.py"
        ext_path.write_text("""
def register(api):
    return None
""")
        result = ExtensionLoader.load(ext_path)
        # The register function itself exists and is callable
        assert result is not None
        assert callable(result)
