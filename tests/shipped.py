"""The built-in extensions that tau does not export, reached as a start reaches them.

`usage` is a built-in only: a program gets it from `load_extensions`, never by importing a class.
`shipped(name)` does that, on an empty `TAU_HOME`, and returns a fresh instance each call.
"""

import tempfile
from pathlib import Path

from tau import Extension, load_config, load_extensions

_HOME = tempfile.TemporaryDirectory(prefix="tau-shipped-")


def shipped(name: str) -> Extension:
    """A fresh instance of the built-in extension `name`, as `load_extensions` builds it."""
    home = Path(_HOME.name)
    loaded = load_config(home, {"HOME": str(home), "TAU_HOME": str(home)}, ask=None)
    for extension in load_extensions(loaded, explicit=(name,), discover=False):
        if extension.name == name:
            return extension
    raise LookupError(f"no built-in extension is named {name!r}")
