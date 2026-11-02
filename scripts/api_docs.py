"""The API documentation, generated: the surface manifest and the reference (Phase 7, 7C).

    scripts/api_docs.py                 rewrite docs/api/surface.txt and docs/reference/
    scripts/api_docs.py --check         fail (with a diff) if either differs from the code
    scripts/api_docs.py --undocumented  list the public names that have no docstring
    scripts/api_docs.py --sync-stub     copy the built module's docstrings into _core.pyi

What is public is `docs/api/groups.toml`: ordered groups, each a reader's reason to open it and
the names in it. A name is `Name` (as `tau.Name`) or `module.name` (as `tau.module.name`, for the
few public modules). The check fails when `tau.__all__` and the groups disagree, when a module
is public by name but holds no public name, when a public module defines a public-looking name
no group lists, when a public signature mentions a name that is not public, and when
`python/tau/_core.pyi`'s docstrings differ from the built module's `__doc__`.

The code is read as syntax (`ast`), never imported, except for the stub check: a signature is
what was written, and the manifest does not depend on a build. Native types are read from the
stub, which stubtest holds to the built module. Everything under docs/reference/ is generated;
a file there that no group makes is stale.

Other generators reuse `sync`: it writes or checks a set of files and reports them the same way
(the wire protocol guide of 7C-3 is one).
"""

import ast
import contextlib
import difflib
import inspect
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parents[1]
PKG = ROOT / "python" / "tau"
GROUPS = ROOT / "docs" / "api" / "groups.toml"
README = ROOT / "docs" / "api" / "README.md"
MANIFEST = ROOT / "docs" / "api" / "surface.txt"
REFERENCE = ROOT / "docs" / "reference"

# Dunders that are part of how a thing is used; the rest (`__eq__`, `__repr__`, ...) are not listed.
USED_DUNDERS = {
    "__init__",
    "__new__",
    "__call__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
    "__iter__",
    "__next__",
    "__aiter__",
    "__anext__",
    "__len__",
    "__getitem__",
    "__contains__",
}
# Decorators that only help a type checker; the manifest leaves them out.
TYPING_DECORATORS = {"final", "disjoint_base", "runtime_checkable", "override", "overload"}
SHORT = 40  # characters of a docstring shown in a mismatch message
WRAP = 100  # a signature longer than this has one parameter per line
HEADING_DEPTH = 6  # Markdown stops at six


class SurfaceError(Exception):
    """The groups, the code or the stub disagree; the message says how."""


# ---- reading the code ---------------------------------------------------------------------


def module_path(module: str) -> Path | None:
    """The file of a dotted module under tau, or None."""
    parts = module.split(".")
    if parts[0] != "tau":
        return None
    base = PKG.joinpath(*parts[1:])
    for candidate in (base / "__init__.py", base.with_suffix(".py"), base.with_suffix(".pyi")):
        if candidate.is_file():
            return candidate
    return None


_trees: dict[str, ast.Module] = {}


def tree_of(module: str) -> ast.Module:
    if module not in _trees:
        path = module_path(module)
        if path is None:
            raise SurfaceError(f"no module {module}")
        _trees[module] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _trees[module]


def attribute_doc(body: Sequence[ast.stmt], index: int) -> str | None:
    """A string literal right after an assignment documents the name it assigned."""
    if index + 1 < len(body):
        nxt = body[index + 1]
        if (
            isinstance(nxt, ast.Expr)
            and isinstance(nxt.value, ast.Constant)
            and isinstance(nxt.value.value, str)
        ):
            return inspect.cleandoc(nxt.value.value)
    return None


@dataclass(frozen=True)
class Defn:
    """Where a name is defined: its module, its syntax, and the doc after an assignment."""

    module: str
    name: str
    node: ast.stmt
    doc: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.name)


def imports_of(module: str) -> dict[str, tuple[str, str]]:
    """Names a module takes from other tau modules: local name -> (module, name)."""
    found: dict[str, tuple[str, str]] = {}
    for node in tree_of(module).body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "tau":
            for alias in node.names:
                found[alias.asname or alias.name] = (node.module, alias.name)
    return found


def assigned_names(node: ast.stmt) -> list[str]:
    match node:
        case ast.Assign(targets=targets):
            return [t.id for t in targets if isinstance(t, ast.Name)]
        case ast.AnnAssign(target=ast.Name(id=name)):
            return [name]
        case _:
            return []


def find(module: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()) -> Defn | None:
    """The definition `name` means in `module`, following imports from other tau modules."""
    if (module, name) in seen:
        raise SurfaceError(f"{module}.{name} imports itself")
    seen |= {(module, name)}
    body = tree_of(module).body
    for i, node in enumerate(body):
        if (
            isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return Defn(module, name, node)
        if name in assigned_names(node):
            value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
            # `TraceError = _core.TraceError`: the name is the other module's
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                source = imports_of(module).get(value.value.id)
                if source and module_path(f"{source[0]}.{source[1]}"):
                    return find(f"{source[0]}.{source[1]}", value.attr, seen)
            return Defn(module, name, node, attribute_doc(body, i))
    source = imports_of(module).get(name)
    if source is None:
        return None
    if module_path(f"{source[0]}.{source[1]}"):  # a submodule, not a name in one
        return None
    return find(source[0], source[1], seen)


# ---- the model of one public name ---------------------------------------------------------


@dataclass
class Item:
    """One documented thing: a class, a function, a method, a property, a field or an alias."""

    kind: str  # class, def, property, field, alias
    name: str
    head: str
    decorators: list[str] = field(default_factory=list)
    doc: str | None = None
    members: list["Item"] = field(default_factory=list)
    counted: bool = False  # a docstring is expected (classes, functions, methods, properties)
    exprs: list[ast.expr] = field(default_factory=list)  # annotations, to check what they name
    module: str = ""

    def lines(self, indent: int = 0) -> Iterator[str]:
        pad = "    " * indent
        for decorator in self.decorators:
            yield f"{pad}@{decorator}"
        for line in self.head.split("\n"):
            yield f"{pad}{line}"
        for member in sorted(self.members, key=lambda m: m.name):
            yield from member.lines(indent + 1)

    def undocumented(self, prefix: str = "") -> Iterator[str]:
        path = f"{prefix}{self.name}"
        if self.counted and not self.doc:
            yield path
        for member in self.members:
            yield from member.undocumented(path + ".")


def unparse(node: ast.AST | None) -> str:
    return "" if node is None else ast.unparse(node)


def default_text(default: ast.expr, module: str) -> str:
    """A default as written, except that a module constant of plain numbers or text is replaced
    by its value, so a changed default shows in the manifest."""
    if isinstance(default, ast.Name):
        defn = find(module, default.id)
        value = getattr(defn.node, "value", None) if defn else None
        if value is not None and all(
            isinstance(n, ast.Constant | ast.BinOp | ast.UnaryOp | ast.operator | ast.unaryop)
            for n in ast.walk(value)
        ):
            return ast.unparse(value)
    return ast.unparse(default)


def render_args(args: ast.arguments, module: str) -> list[str]:
    """The parameters as written, with PEP 8 spacing around the `=` of an annotated default."""

    def one(arg: ast.arg, default: ast.expr | None) -> str:
        text = arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation else "")
        if default is None:
            return text
        spacing = " = " if arg.annotation else "="
        return f"{text}{spacing}{default_text(default, module)}"

    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    parts = [one(a, d) for a, d in zip(positional, defaults, strict=True)]
    if args.posonlyargs:
        parts.insert(len(args.posonlyargs), "/")
    if args.vararg:
        parts.append("*" + one(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    parts += [one(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)]
    if args.kwarg:
        parts.append("**" + one(args.kwarg, None))
    return parts


def signature(keyword: str, name: str, parts: list[str], ret: str) -> str:
    """One line, or one parameter per line when that line would be long."""
    line = f"{keyword} {name}({', '.join(parts)}){ret}"
    if len(line) <= WRAP:
        return line
    body = "".join(f"    {part},\n" for part in parts)
    return f"{keyword} {name}(\n{body}){ret}"


def decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    kept = []
    for decorator in node.decorator_list:
        text = ast.unparse(decorator)
        if text.split(".")[-1].split("(")[0] not in TYPING_DECORATORS:
            kept.append(text)
    return kept


def function_item(node: ast.FunctionDef | ast.AsyncFunctionDef, module: str) -> Item | None:
    decorators = decorator_names(node)
    if any(d.endswith((".setter", ".deleter")) for d in decorators):
        return None
    exprs = [
        a.annotation
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if a.annotation
    ]
    for star in (node.args.vararg, node.args.kwarg):
        if star and star.annotation:
            exprs.append(star.annotation)
    if node.returns:
        exprs.append(node.returns)
    doc = ast.get_docstring(node)
    counted = not node.name.startswith("__")
    if "property" in decorators:
        ret = unparse(node.returns) or "..."
        return Item(
            "property",
            node.name,
            f"property {node.name}: {ret}",
            [],
            doc,
            [],
            counted,
            exprs,
            module,
        )
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {unparse(node.returns)}" if node.returns else ""
    head = signature(keyword, node.name, render_args(node.args, module), ret)
    return Item("def", node.name, head, decorators, doc, [], counted, exprs, module)


def public_member(name: str) -> bool:
    return not name.startswith("_") or name in USED_DUNDERS


def private_bases(node: ast.ClassDef, module: str) -> list[Defn]:
    """The classes in tau this one inherits from whose names start with an underscore."""
    found = []
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id.startswith("_"):
            defn = find(module, base.id)
            if defn and isinstance(defn.node, ast.ClassDef):
                found.append(defn)
    return found


def class_members(node: ast.ClassDef, module: str) -> list[Item]:
    own: list[Item] = []
    body = node.body
    for i, stmt in enumerate(body):
        item: Item | None = None
        match stmt:
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if public_member(stmt.name):
                    item = function_item(stmt, module)
            case ast.ClassDef():
                if public_member(stmt.name):
                    item = class_item(stmt, module)
            case ast.AnnAssign(target=ast.Name(id=name)) if public_member(name):
                head = f"{name}: {unparse(stmt.annotation)}"
                if stmt.value is not None:
                    head += f" = {unparse(stmt.value)}"
                doc = attribute_doc(body, i)
                item = Item("field", name, head, [], doc, [], False, [stmt.annotation], module)
            case ast.Assign(targets=[ast.Name(id=name)]) if public_member(name):
                head = f"{name} = {unparse(stmt.value)}"
                item = Item("field", name, head, [], attribute_doc(body, i), [], False, [], module)
            case _:
                pass
        if item is not None:
            own.append(item)
    taken = {m.name for m in own}
    inherited: list[Item] = []  # a private base's public members are this class's own
    for base in private_bases(node, module):
        assert isinstance(base.node, ast.ClassDef)
        inherited += [m for m in class_members(base.node, base.module) if m.name not in taken]
    return [*inherited, *own]


def class_item(node: ast.ClassDef, module: str) -> Item:
    bases = [
        ast.unparse(b) for b in node.bases if not (isinstance(b, ast.Name) and b.id.startswith("_"))
    ]
    bases += [f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords if k.arg]
    head = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    exprs = [b for b in node.bases if not (isinstance(b, ast.Name) and b.id.startswith("_"))]
    item = Item(
        "class",
        node.name,
        head,
        decorator_names(node),
        ast.get_docstring(node),
        [],
        True,
        exprs,
        module,
    )
    item.members = class_members(node, module)
    return item


def item_of(defn: Defn) -> Item:
    node = defn.node
    match node:
        case ast.ClassDef():
            return class_item(node, defn.module)
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            item = function_item(node, defn.module)
            assert item is not None
            return item
        case ast.AnnAssign(annotation=annotation, value=value):
            head = f"{defn.name}: {unparse(annotation)}" + (f" = {unparse(value)}" if value else "")
            return Item(
                "alias",
                defn.name,
                head,
                [],
                defn.doc,
                [],
                not defn.name.startswith("__"),
                [annotation],
                defn.module,
            )
        case ast.Assign(value=value):
            return Item(
                "alias",
                defn.name,
                f"{defn.name} = {unparse(value)}",
                [],
                defn.doc,
                [],
                not defn.name.startswith("__"),
                [value],
                defn.module,
            )
        case _:
            raise SurfaceError(
                f"{defn.module}.{defn.name}: cannot document a {type(node).__name__}"
            )


# ---- the groups ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    reader: str
    names: tuple[str, ...]


def load_groups() -> list[Group]:
    data = tomllib.loads(GROUPS.read_text(encoding="utf-8"))
    groups = [
        Group(g["id"], g["title"], g["reader"].strip(), tuple(g["names"])) for g in data["group"]
    ]
    ids = [g.id for g in groups]
    if len(set(ids)) != len(ids):
        raise SurfaceError(f"{GROUPS.name}: a group id is used twice")
    return groups


def dunder_all() -> list[str]:
    for node in tree_of("tau").body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return list(ast.literal_eval(node.value))
    raise SurfaceError("python/tau/__init__.py has no __all__")


def locate(name: str) -> Defn:
    """The definition of a group's name: `Name` is `tau.Name`, `module.name` is in that module."""
    if "." in name:
        module, _, attr = name.rpartition(".")
        defn = find(f"tau.{module}", attr)
    else:
        defn = find("tau", name)
    if defn is None:
        raise SurfaceError(f"{GROUPS.name}: {name} is not defined where it says")
    return defn


def made_by_tau() -> list[str]:
    """Classes tau makes and hands over: their constructors are not part of the surface."""
    return list(tomllib.loads(GROUPS.read_text(encoding="utf-8")).get("made_by_tau", []))


def public_items(groups: Sequence[Group]) -> dict[str, Item]:
    items = {name: item_of(locate(name)) for g in groups for name in g.names}
    for name in made_by_tau():
        if name not in items or items[name].kind != "class":
            raise SurfaceError(
                f"{GROUPS.name}: made_by_tau lists {name}, which is not a public class"
            )
        items[name].members = [
            m for m in items[name].members if m.name not in ("__init__", "__new__")
        ]
    return items


def check_groups(groups: Sequence[Group]) -> list[str]:
    """Where `tau.__all__`, the groups and the module names disagree."""
    problems: list[str] = []
    listed = [n for g in groups for n in g.names]
    for name in sorted({n for n in listed if listed.count(n) > 1}):
        problems.append(f"{name} is in more than one group")
    exported = set(dunder_all())
    top = {n for n in listed if "." not in n}
    problems += [f"{n} is in tau.__all__ but in no group" for n in sorted(exported - top)]
    problems += [f"{n} is in a group but not in tau.__all__" for n in sorted(top - exported)]
    public_modules = {n.rpartition(".")[0] for n in listed if "." in n}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        parts = [*rel.parts[:-1], rel.stem if rel.stem != "__init__" else ""]
        dotted = ".".join(p for p in parts if p)
        if not dotted or any(p.startswith("_") for p in dotted.split(".")):
            continue  # the package itself, or a private module
        if dotted not in public_modules:
            problems.append(
                f"tau.{dotted} is public by name but no group lists a name in it: "
                "prefix it with an underscore, or list what is public"
            )
    for module in sorted(public_modules):
        for node in tree_of(f"tau.{module}").body:
            defined = (
                [node.name]
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                else assigned_names(node)
            )
            for name in defined:
                if (
                    not name.startswith("_")
                    and f"{module}.{name}" not in listed
                    and name not in top
                ):
                    problems.append(f"tau.{module}.{name} is public by name but in no group")
    return problems


def check_readme(groups: Sequence[Group]) -> list[str]:
    text = README.read_text(encoding="utf-8") if README.is_file() else ""
    return [
        f"{README.relative_to(ROOT)} has no '## {g.title}' heading for the group {g.id}"
        for g in groups
        if f"\n## {g.title}\n" not in text
    ]


# ---- what a public signature may name -----------------------------------------------------


def named_in(expr: ast.expr) -> Iterator[str]:
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            with contextlib.suppress(SyntaxError):  # not every string is a type
                yield from named_in(ast.parse(node.value, mode="eval").body)


def walk_items(item: Item) -> Iterator[Item]:
    yield item
    for member in item.members:
        yield from walk_items(member)


def private_names_in_signatures(groups: Sequence[Group], items: Mapping[str, Item]) -> list[str]:
    """Names a public signature uses that tau defines but does not publish."""
    public_keys = {locate(n).key for g in groups for n in g.names}
    problems: list[str] = []
    for name, item in sorted(items.items()):
        for sub in walk_items(item):
            for expr in sub.exprs:
                for used in sorted(set(named_in(expr))):
                    defn = find(sub.module, used)
                    if defn is None or defn.key in public_keys or is_type_variable(defn):
                        continue
                    where = f"{name}.{sub.name}" if sub is not item else name
                    problems.append(
                        f"{where} names {used}, which is defined in tau ({defn.module}) "
                        "but not public"
                    )
    return sorted(set(problems))


def is_type_variable(defn: Defn) -> bool:
    value = getattr(defn.node, "value", None)
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"TypeVar", "ParamSpec", "TypeVarTuple"}
    )


# ---- the stub's docstrings against the built module -----------------------------------------


StubNode = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def stub_nodes() -> dict[str, StubNode]:
    """Dotted path under tau._core -> its node, for every class, nested class, function, method
    and property the stub declares (underscore names are not documented)."""
    out: dict[str, StubNode] = {}

    def visit(body: Sequence[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, StubNode) and not node.name.startswith("_"):
                if any(ast.unparse(d).endswith(".setter") for d in node.decorator_list):
                    continue
                out[f"{prefix}{node.name}"] = node
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

    visit(tree_of("tau._core").body, "")
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


def built_module() -> object:
    sys.path.insert(0, str(ROOT / "python"))
    try:
        from tau import _core  # noqa: PLC0415 - only the stub check needs the build
    except ImportError as e:
        raise SurfaceError(
            f"cannot import tau._core to compare docstrings ({e}); run maturin develop"
        ) from e
    return _core


def stub_doc_mismatches() -> list[str]:
    """Docstrings in `_core.pyi` that differ from the built module's `__doc__`."""
    built = built_module()
    problems = []
    for path, node in sorted(stub_nodes().items()):
        stub = ast.get_docstring(node)
        exists, runtime = runtime_doc(built, path)
        if not exists:
            problems.append(f"_core.pyi declares {path}, which the built module lacks")
        elif (stub or None) != (runtime or None):
            problems.append(
                f"_core.pyi: the docstring of {path} differs from the built module's __doc__ "
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
    """Copy the built module's docstrings into `_core.pyi` wherever the stub's differ. The Rust
    `///` is the source; this only copies it. Returns how many docstrings changed."""
    built = built_module()
    path = module_path("tau._core")
    assert path is not None
    lines = path.read_text(encoding="utf-8").split("\n")
    edits = []
    for name, node in stub_nodes().items():
        exists, runtime = runtime_doc(built, name)
        if exists and runtime and runtime != ast.get_docstring(node):
            edits.append(stub_edit(node, runtime, lines))
    for first, last, replacement in sorted(edits, reverse=True):
        lines[first : last + 1] = replacement
    path.write_text("\n".join(lines), encoding="utf-8")
    _trees.pop("tau._core", None)
    return len(edits)


def _short(doc: str | None) -> str:
    if not doc:
        return "none"
    first = doc.splitlines()[0]
    return repr(first if len(first) <= SHORT else first[: SHORT - 3] + "...")


# ---- the outputs ---------------------------------------------------------------------------

BANNER = "Generated by scripts/api_docs.py from the code and docs/api/groups.toml. Do not edit."


def manifest(groups: Sequence[Group], items: Mapping[str, Item]) -> str:
    out = [f"# {BANNER}", "# The public surface of tau: one block per group, names sorted.", ""]
    for g in groups:
        out.append(f"== {g.id}: {g.title}")
        for name in sorted(g.names):
            if "." in name:  # a name in a public module says which
                out.append(f"# tau.{name.rpartition('.')[0]}")
            out += items[name].lines()
        out.append("")
    return "\n".join(out)


def reference_item(item: Item, shown: str, depth: int) -> list[str]:
    """One item as Markdown: heading, signature, docstring, then its members."""
    mark = "#" * min(depth, HEADING_DEPTH)
    out = [f"{mark} {shown}", "", "```python", *_head_block(item), "```", ""]
    if item.doc:
        out += [item.doc, ""]
    for member in item.members:
        if member.kind in {"field"}:
            continue
        out += reference_item(member, f"{shown}.{member.name}", depth + 1)
    return out


def _head_block(item: Item) -> list[str]:
    lines = [f"@{d}" for d in item.decorators] + [item.head]
    lines += [
        f"    {m.head}" for m in item.members if m.kind == "field"
    ]  # a class's fields are part of its shape
    return lines


def reference(group: Group, items: Mapping[str, Item]) -> str:
    out = [f"# {group.title}", "", f"<!-- {BANNER} -->", "", group.reader, ""]
    out += ["Names: " + ", ".join(f"`{n}`" for n in sorted(group.names)), ""]
    for name in sorted(group.names):
        shown = name if "." not in name else f"tau.{name}"
        out += reference_item(items[name], shown, 2)
    return "\n".join(out).rstrip() + "\n"


def reference_index(groups: Sequence[Group], items: Mapping[str, Item]) -> str:
    out = ["# Reference", "", f"<!-- {BANNER} -->", ""]
    out += [
        "One page per group of the public surface (`docs/api/README.md` says who each is for).",
        "",
    ]
    for g in groups:
        out.append(f"- [{g.title}]({g.id}.md): {len(g.names)} names")
    return "\n".join(out) + "\n"


def outputs() -> dict[Path, str]:
    groups = load_groups()
    problems = check_groups(groups) + check_readme(groups)
    items = public_items(groups)
    problems += private_names_in_signatures(groups, items)
    if problems:
        raise SurfaceError("\n".join(problems))
    files = {MANIFEST: manifest(groups, items) + "\n"}
    files[REFERENCE / "README.md"] = reference_index(groups, items)
    for g in groups:
        files[REFERENCE / f"{g.id}.md"] = reference(g, items)
    return files


def undocumented() -> dict[str, list[str]]:
    """Group id -> the public names (and members) that have no docstring."""
    groups = load_groups()
    items = public_items(groups)
    return {
        g.id: [p for name in sorted(g.names) for p in items[name].undocumented(prefix_of(name))]
        for g in groups
    }


def prefix_of(name: str) -> str:
    return name.rpartition(".")[0] + "." if "." in name else ""


# ---- writing and checking ------------------------------------------------------------------


def sync(files: Mapping[Path, str], *, check: bool, label: str, owned: Sequence[Path] = ()) -> int:
    """Write `files`, or with `check` compare them with what is on disk and print a diff.

    `owned` are directories every file of which is generated: one not in `files` is stale.
    Returns the exit status."""
    stale = [
        p
        for d in owned
        if d.is_dir()
        for p in sorted(d.rglob("*"))
        if p.is_file() and p not in files
    ]
    if not check:
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        for path in stale:
            path.unlink()
        return 0
    status = 0
    for path, text in sorted(files.items()):
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        if have != text:
            status = 1
            sys.stderr.writelines(
                difflib.unified_diff(
                    have.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    str(path.relative_to(ROOT)),
                    "generated",
                )
            )
    for path in stale:
        status = 1
        print(
            f"{label}: {path.relative_to(ROOT)} is not generated any more; remove it",
            file=sys.stderr,
        )
    if status:
        print(f"{label}: stale; run scripts/{Path(sys.argv[0]).name}", file=sys.stderr)
    else:
        print(f"{label}: up to date")
    return status


def main(args: list[str]) -> int:
    if args not in ([], ["--check"], ["--undocumented"], ["--sync-stub"]):
        print(
            "usage: scripts/api_docs.py [--check | --undocumented | --sync-stub]", file=sys.stderr
        )
        return 2
    try:
        if args == ["--sync-stub"]:
            print(f"api_docs: {sync_stub()} docstring(s) copied into _core.pyi")
            return 0
        if args == ["--undocumented"]:
            for group, names in undocumented().items():
                print(f"{group}: {len(names)}")
                for name in names:
                    print(f"  {name}")
            return 0
        files = outputs()
        status = sync(files, check=args == ["--check"], label="api_docs", owned=[REFERENCE])
        if args == ["--check"]:
            problems = stub_doc_mismatches()
            for problem in problems:
                print(f"api_docs: {problem}", file=sys.stderr)
            status = status or int(bool(problems))
        return status
    except SurfaceError as e:
        for line in str(e).splitlines():
            print(f"api_docs: {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
