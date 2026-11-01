"""Importing a project's extension code from the bytes trust approved (D19 section 4).

`.tau/extensions/` is pinned by `trusted.json` (`_trust`); what runs must be what was approved.
So the loader never imports from the directory: `ApprovedFiles` is a finder for one source's
private module name that serves each module's source from the bytes `Trusted.extension_files`
holds, compiles it with the file's real path (tracebacks name it), and refuses a module that was
not among the approved files, even when a file by that name has appeared on disk since. A file
changed between the check and the import can't slip in, relative imports work, and nothing
writes `__pycache__` into the project.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

_INIT = "__init__.py"


def forget(prefix: str) -> None:
    """Drop `prefix` and its submodules from `sys.modules`, so the next import runs them afresh."""
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        del sys.modules[name]


class ApprovedFiles(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """The finder and loader of one project source: module `prefix` is `layout` (`ralph.py`,
    or the package directory `ralph`) of the approved `files` (path relative to
    `.tau/extensions/`, to bytes), whose real directory is `directory`."""

    def __init__(
        self, prefix: str, directory: Path, files: Mapping[str, bytes], layout: str
    ) -> None:
        self.prefix = prefix
        self.directory = directory
        self.files = files
        self.layout = layout

    def _locate(self, fullname: str) -> tuple[str, bool] | None:
        """The approved path of module `fullname` and whether it is a package; `None` if it
        is not this source's name, `ModuleNotFoundError` if it is but no approved file is it."""
        if fullname != self.prefix and not fullname.startswith(self.prefix + "."):
            return None
        parts = [p for p in fullname[len(self.prefix) :].split(".") if p]
        if not parts:
            if self.layout.endswith(".py"):
                return self.layout, False
            return f"{self.layout}/{_INIT}", True
        if not self.layout.endswith(".py"):
            base = "/".join([self.layout, *parts])
            if f"{base}.py" in self.files:
                return f"{base}.py", False
            if f"{base}/{_INIT}" in self.files:
                return f"{base}/{_INIT}", True
        raise ModuleNotFoundError(
            f"No module named {fullname!r}: it is not among the files approved in "
            ".tau/extensions/ (a file added since needs `tau trust`)",
            name=fullname,
        )

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        found = self._locate(fullname)
        if found is None:
            return None
        relative, package = found
        real = self.directory / relative
        return importlib.util.spec_from_file_location(
            fullname,
            str(real),
            loader=self,
            submodule_search_locations=[str(real.parent)] if package else None,
        )

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        located = self._locate(module.__name__)
        assert located is not None  # a spec only comes from find_spec
        real = self.directory / located[0]
        code = compile(self.files[located[0]], str(real), "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_source(self, fullname: str) -> str | None:
        located = self._locate(fullname)
        return None if located is None else self.files[located[0]].decode("utf-8", "replace")

    def install(self) -> None:
        """Put this finder first on `sys.meta_path` (replacing an earlier one for the same
        source), and forget whatever a previous load of the source left in `sys.modules`."""
        sys.meta_path[:] = [
            f
            for f in sys.meta_path
            if not (isinstance(f, ApprovedFiles) and f.prefix == self.prefix)
        ]
        forget(self.prefix)
        sys.meta_path.insert(0, self)
