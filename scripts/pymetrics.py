"""Python source metrics for python/tau (src only), for scripts/ratchet.py.

Measured with radon (`cc_visit`, the same tool and counting as py_baseline.py):

* Function CC: radon's complexity for a top-level function or a method, plus
  the extra complexity of its nested functions (a closure is part of its
  enclosing function, as in rustmetrics.py).
* Methods per class: the functions defined directly in the class body.
* File lines: physical lines of every .py file.

Keys are repo-relative: `python/tau/x.py::Class.method`,
`python/tau/x.py::Class`, `python/tau/x.py`.
"""

from pathlib import Path

from radon.complexity import cc_visit
from radon.visitors import Class, Function

ROOT = Path(__file__).parents[1]
PKG = ROOT / "python" / "tau"


def _folded(fn: Function) -> int:
    return fn.complexity + sum(_folded(c) - 1 for c in fn.closures)


def analyze() -> dict[str, dict[str, int]]:
    """{'function_cc', 'type_methods', 'file_lines'} for python/tau/**.py."""
    cc: dict[str, int] = {}
    methods: dict[str, int] = {}
    lines: dict[str, int] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text()
        lines[rel] = len(text.splitlines())
        for block in cc_visit(text):
            if isinstance(block, Class):
                methods[f"{rel}::{block.name}"] = len(block.methods)
                for m in block.methods:
                    key = f"{rel}::{block.name}.{m.name}"
                    cc[key] = max(cc.get(key, 0), _folded(m))
            elif not block.is_method:  # methods are reached through their class
                key = f"{rel}::{block.name}"
                cc[key] = max(cc.get(key, 0), _folded(block))
    return {"function_cc": cc, "type_methods": methods, "file_lines": lines}
