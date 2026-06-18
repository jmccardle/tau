"""τ-agent-core extensions loader — discovers and loads extension modules.

Reference: PHASE-3-SUBPHASE-0.md ExtensionLoader contract.
Reference: PHASE-3-SUBPHASE-2.md ExtensionLoader implementation.

Contract:
    class ExtensionLoader:
        EXTENSION_DIRS: list[Path]
        @classmethod
        def discover(cls, cwd: str | None = None) -> list[Path]: ...
        @classmethod
        def load(cls, path: Path) -> Callable | None: ...
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Callable


class ExtensionLoader:
    """Discovers and loads Python extension modules.

    Provides mechanisms for:
    - Discovering installed extension modules from global and project directories
    - Loading extension modules via importlib
    - Calling the extension's register() function

    Reference: PHASE-3-SUBPHASE-0.md ExtensionLoader contract.
    Reference: PHASE-3-SUBPHASE-2.md implementation outline.
    """

    EXTENSION_DIRS: list[Path] = []  # Default dirs, may be overridden in tests

    @classmethod
    def _get_extension_dirs(cls) -> list[Path]:
        """Compute extension directories.

        Uses EXTENSION_DIRS if explicitly set (e.g., by tests),
        otherwise computes default from Path.home().
        """
        # If tests have overridden EXTENSION_DIRS, use it
        if cls.EXTENSION_DIRS:
            return cls.EXTENSION_DIRS
        # Otherwise compute from current Path.home()
        return [Path.home() / ".tau" / "extensions"]

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
        extensions: list[Path] = []
        for ext_dir in cls._get_extension_dirs():
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
            logging.error(f"Failed to load extension {path}: {e}")
            return None
