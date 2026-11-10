"""The docstrings of the native modules' stubs (`python/tau/_core.pyi`, and
`tokens/python/tau_tokens/_core.pyi` for the tokenizer wheel) against the built modules' `__doc__`.

    scripts/stub_docs.py            copy the built module's docstrings into the stub
    scripts/stub_docs.py --check    fail if any differs (scripts/api_docs.py --check runs this too)

The Rust `///` on a pyclass or pymethod is the source of a native type's documentation; the stub
carries the same text so a reader and a type checker see it. Editing a `///` is therefore two
steps: rebuild (`maturin develop`), then run this, which rewrites only the docstrings that differ.
"""

import ast
import inspect
import sys
from collections.abc import Sequence
from pathlib import Path

from tau import _core
from tau_tokens import _core as _tokens_core

ROOT = Path(__file__).parents[1]
STUB = ROOT / "python" / "tau" / "_core.pyi"
# Every hand-written stub of a native module, with the module it describes (D26 §7.1: the
# tokenizer wheel's is held the same way as the main wheel's).
STUBS: list[tuple[Path, object]] = [
    (STUB, _core),
    (ROOT / "tokens" / "python" / "tau_tokens" / "_core.pyi", _tokens_core),
]
SHORT = 40  # characters of a docstring shown in a mismatch message
StubNode = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def stub_nodes(stub: Path = STUB) -> dict[str, StubNode]:
    """Dotted path under the stub's module -> its node, for every class, nested class, function,
    method and property the stub declares (underscore names are not documented)."""
    out: dict[str, StubNode] = {}

    def visit(body: Sequence[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, StubNode) and not node.name.startswith("_"):
                if any(ast.unparse(d).endswith(".setter") for d in node.decorator_list):
                    continue
                out[f"{prefix}{node.name}"] = node
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

    visit(ast.parse(stub.read_text(encoding="utf-8")).body, "")
    return out


def runtime_doc(root: object, path: str) -> tuple[bool, str | None]:
    """(exists, cleaned `__doc__`) of a dotted path under the built module."""
    obj: object = root
    for part in path.split("."):
        raw = obj.__dict__.get(part) if isinstance(obj, type) else None
        if raw is not None and not isinstance(raw, staticmethod | classmethod):
            obj = raw  # a property's descriptor, not the value it would give
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            return False, None
    doc = getattr(obj, "__doc__", None)
    return True, inspect.cleandoc(doc) if doc else None


def _short(doc: str | None) -> str:
    if not doc:
        return "none"
    first = doc.splitlines()[0]
    return repr(first if len(first) <= SHORT else first[: SHORT - 3] + "...")


def mismatches() -> list[str]:
    """Docstrings in `_core.pyi` that differ from the built module's `__doc__`."""
    problems = []
    for file, module in STUBS:
        name = str(file.relative_to(ROOT))
        for path, node in sorted(stub_nodes(file).items()):
            stub = ast.get_docstring(node)
            exists, runtime = runtime_doc(module, path)
            if not exists:
                problems.append(f"{name} declares {path}, which the built module lacks")
            elif (stub or None) != (runtime or None):
                problems.append(
                    f"{name}: the docstring of {path} differs from the built module's __doc__ "
                    f"(stub: {_short(stub)}; runtime: {_short(runtime)})"
                )
    return problems


def docstring_lines(doc: str, indent: str) -> list[str]:
    """`doc` as a triple-quoted docstring statement, `indent`ed."""
    text = doc.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if text.endswith('"'):
        text = text[:-1] + '\\"'
    first, *rest = text.split("\n")
    if not rest:
        return [f'{indent}"""{first}"""']
    return [f'{indent}"""{first}', *(f"{indent}{r}" if r else "" for r in rest), f'{indent}"""']


def stub_edit(node: StubNode, doc: str, lines: list[str]) -> tuple[int, int, list[str]]:
    """(first line, last line, replacement), 0-based and inclusive, giving `node` the docstring."""
    body = node.body
    first = body[0]
    has_doc = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    is_ellipsis = (
        len(body) == 1
        and isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and first.value.value is Ellipsis
    )
    if has_doc or is_ellipsis:
        assert first.end_lineno is not None
        lead = lines[first.lineno - 1].encode()[: first.col_offset].decode()
        if lead.strip():  # `def f(): ...`: the body moves to a line of its own
            indent = " " * (node.col_offset + 4)
            return (
                first.lineno - 1,
                first.end_lineno - 1,
                [lead.rstrip(), *docstring_lines(doc, indent)],
            )
        return (first.lineno - 1, first.end_lineno - 1, docstring_lines(doc, lead))
    start = min([first.lineno, *(d.lineno for d in getattr(first, "decorator_list", []))])
    indent = " " * first.col_offset
    return (start - 1, start - 2, [*docstring_lines(doc, indent), ""])  # an insertion


def sync_stub() -> int:
    """Copy the built modules' docstrings into their stubs wherever a stub's differ. The Rust
    `///` is the source; this only copies it. Returns how many docstrings changed."""
    changed = 0
    for path, module in STUBS:
        lines = path.read_text(encoding="utf-8").split("\n")
        edits = []
        for name, node in stub_nodes(path).items():
            exists, runtime = runtime_doc(module, name)
            if exists and runtime and runtime != ast.get_docstring(node):
                edits.append(stub_edit(node, runtime, lines))
        for first, last, replacement in sorted(edits, reverse=True):
            lines[first : last + 1] = replacement
        path.write_text("\n".join(lines), encoding="utf-8")
        changed += len(edits)
    return changed


def main(args: list[str]) -> int:
    if args == ["--check"]:
        problems = mismatches()
        for problem in problems:
            print(f"stub_docs: {problem}", file=sys.stderr)
        return int(bool(problems))
    if args:
        print("usage: scripts/stub_docs.py [--check]", file=sys.stderr)
        return 2
    print(f"stub_docs: {sync_stub()} docstring(s) copied into the stubs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
