"""Fail when python/tau_next/_core.pyi and the built native module disagree.

A build check, not a behavior test: it needs the private native module, and
tests may not touch private names (CLAUDE.md rule 4).
"""

import ast
import importlib
import sys
from pathlib import Path

STUB = Path(__file__).parents[1] / "python" / "tau_next" / "_core.pyi"


def stub_names() -> set[str]:
    names: set[str] = set()
    for node in ast.parse(STUB.read_text()).body:
        match node:
            case ast.ClassDef(name=name) | ast.FunctionDef(name=name):
                names.add(name)
            case ast.AnnAssign(target=ast.Name(id=name)):
                names.add(name)
            case _:
                pass
    return names


def native_names() -> set[str]:
    core = importlib.import_module("tau_next._core")
    return {n for n in vars(core) if not n.startswith("__")}


def main() -> int:
    stub, native = stub_names(), native_names()
    for name in sorted(native - stub):
        print(f"{STUB}: native export {name!r} is not declared", file=sys.stderr)
    for name in sorted(stub - native):
        print(f"{STUB}: declares {name!r}, which the native module lacks", file=sys.stderr)
    return 1 if stub != native else 0


if __name__ == "__main__":
    sys.exit(main())
