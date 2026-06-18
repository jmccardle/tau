"""τ-agent-core extensions loader — discovers and loads extension modules.

Reference: PHASE-3-SUBPHASE-0.md ExtensionLoader contract.

Contract:
    class ExtensionLoader:
        def discover(self) -> list[str]: ...
        def load(self, name: str) -> Extension: ...
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path
from typing import Any


class ExtensionLoader:
    """Discovers and loads extension modules.

    Provides mechanisms for:
    - Discovering installed extension packages
    - Loading extension modules by name
    - Validating extension interfaces

    Reference: PHASE-3-SUBPHASE-0.md ExtensionLoader contract.
    """

    def __init__(self, search_paths: list[str] | None = None) -> None:
        """Initialize the loader.

        Args:
            search_paths: Additional paths to search for extensions.
        """
        self._search_paths = search_paths or []
        self._loaded: dict[str, Any] = {}

    def discover(self, search_path: str | None = None) -> list[str]:
        """Discover available extension modules.

        Searches for Python packages that could be extensions by looking
        for modules that follow the naming convention and contain the
        expected extension attributes.

        Args:
            search_path: Optional path to search. Falls back to
                         configured search paths.

        Returns:
            List of extension module names found.
        """
        extensions = []
        paths_to_search = [search_path] if search_path else self._search_paths

        # Always include the package's own extensions directory
        extensions.extend(self._discover_in_dir())

        for path in paths_to_search:
            extensions.extend(self._discover_in_dir(path))

        return list(set(extensions))

    def load(self, name: str) -> dict[str, Any]:
        """Load an extension by name.

        Loads the extension module and returns its API dictionary.
        The extension module must be importable.

        Args:
            name: The extension module name (e.g., 'my_extension').

        Returns:
            A dict with 'name' and 'module' keys for the loaded extension.

        Raises:
            ImportError: If the extension module cannot be imported.
        """
        if name in self._loaded:
            return self._loaded[name]

        try:
            module = importlib.import_module(name)
            extension = {
                "name": name,
                "module": module,
                "enabled": True,
            }
            self._loaded[name] = extension
            return extension
        except ImportError as e:
            raise ImportError(f"Cannot load extension '{name}': {e}") from e

    def _discover_in_dir(self, directory: str | None = None) -> list[str]:
        """Discover extension modules in a directory.

        Args:
            directory: Directory to search. If None, uses module's
                       extensions directory.

        Returns:
            List of extension module names.
        """
        extensions = []

        if directory:
            # Search the given directory
            try:
                for importer, modname, ispkg in pkgutil.iter_modules([directory]):
                    if ispkg or modname.startswith("_"):
                        continue
                    # Check if it looks like an extension
                    try:
                        mod = importlib.import_module(f"{directory}.{modname}") if directory else modname
                        if hasattr(mod, "__tau_extension__"):
                            extensions.append(modname)
                    except (ImportError, AttributeError):
                        continue
            except (FileNotFoundError, ModuleNotFoundError):
                pass
        else:
            # Search the current package's extensions directory
            try:
                import tau_agent_core
                base = Path(tau_agent_core.__file__).parent
                ext_dir = base / "extensions"
                if ext_dir.exists():
                    for item in sorted(ext_dir.iterdir()):
                        if item.is_dir() and not item.name.startswith("_"):
                            extensions.append(item.name)
                        elif item.suffix == ".py" and not item.name.startswith("_"):
                            extensions.append(item.stem)
            except (ImportError, AttributeError):
                pass

        return extensions
