"""The API documentation, generated: the surface manifest and the reference (Phase 7, 7C).

    scripts/api_docs.py                 rewrite docs/api/surface.txt and docs/reference/
    scripts/api_docs.py --check         fail (with a diff) if either differs from the code
    scripts/api_docs.py --undocumented  list the public names that have no docstring

What is public is `docs/api/groups.toml`: ordered groups, each a reader's reason to open it and
the names in it. A name is `Name` (as `tau.Name`) or `module.name` (as `tau.module.name`, for the
few public modules). The check fails when `tau.__all__` and the groups disagree, when a module
is public by name but holds no public name, when a public module defines a public-looking name
no group lists, when a public signature mentions a type that is neither public, the standard
library's nor on the `foreign_types` list of groups.toml, and (scripts/stub_docs.py) when
`python/tau/_core.pyi`'s docstrings differ from the built module's `__doc__`.

The package is imported (`__module__` says where a definition is, `__mro__` what it inherits),
and `ast` reads only what the runtime does not keep: a signature as written, the string after an
assignment. Native types are read from the stub, which stubtest holds to the built module, and
the check holds its docstrings equal to the built module's. A class lists its own members and
those of its underscore bases, and none that a public base already declares: they are
documented there, once. Everything under docs/reference/ is generated; a file there that no
group makes is stale.

Writing and checking the generated files is scripts/generated.py, shared with the other
generators.
"""

import ast
import builtins
import contextlib
import importlib
import inspect
import sys
import tomllib
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import stub_docs
from generated import ROOT, sync

import tau

PKG = ROOT / "python" / "tau"
GROUPS = ROOT / "docs" / "api" / "groups.toml"
README = ROOT / "docs" / "api" / "README.md"
MANIFEST = ROOT / "docs" / "api" / "surface.txt"
REFERENCE = ROOT / "docs" / "reference"

# Dunders that are part of how a thing is used; the rest (`__eq__`, `__repr__`, ...) are not listed.
USED_DUNDERS = {
    *("__init__", "__new__", "__call__", "__enter__", "__exit__", "__aenter__", "__aexit__"),
    *("__iter__", "__next__", "__aiter__", "__anext__", "__len__", "__getitem__", "__contains__"),
}
# Decorators that only help a type checker; the manifest leaves them out.
TYPING_DECORATORS = {"final", "disjoint_base", "runtime_checkable", "override", "overload"}
WRAP = 100  # a signature longer than this has one parameter per line
HEADING_DEPTH = 6  # Markdown stops at six


class SurfaceError(Exception):
    """The groups, the code or the stub disagree; the message says how."""


# ---- reading the code ---------------------------------------------------------------------
# The package is imported: `__module__` says where a class or function is defined, `__mro__` what
# it inherits. `ast` reads only what the runtime does not keep: a signature as written, the
# string after an assignment, the order of a class body.


@cache
def tree_of(module: str) -> ast.Module:
    """The syntax of a module; a native module's is its stub."""
    path = Path(importlib.import_module(module).__file__ or "")
    if path.suffix != ".py":
        path = PKG / f"{module.rpartition('.')[2]}.pyi"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


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


def assigned_names(node: ast.stmt) -> list[str]:
    match node:
        case ast.Assign(targets=targets):
            return [t.id for t in targets if isinstance(t, ast.Name)]
        case ast.AnnAssign(target=ast.Name(id=name)):
            return [name]
        case _:
            return []


def defined(module: str, name: str) -> tuple[ast.stmt, str | None]:
    """The statement that defines `name` in `module`, and the doc after an assignment."""
    body = tree_of(module).body
    for i, node in enumerate(body):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name == name:
                return node, None
        elif name in assigned_names(node):
            return node, attribute_doc(body, i)
    raise SurfaceError(f"{module} does not define {name}")


def resolve(name: str) -> object:
    """The object a group's name means: `Name` is `tau.Name`, `module.name` is in that module."""
    module, _, attr = name.rpartition(".")
    owner = importlib.import_module(f"tau.{module}") if module else tau
    if not hasattr(owner, attr):
        raise SurfaceError(f"{GROUPS.name}: {name} is not defined where it says")
    return getattr(owner, attr)


def home(name: str) -> tuple[str, str]:
    """(module, name) of the definition behind a group's name. A class or function says where it
    is defined; an alias or constant is where `tau` (or the public module) takes it from."""
    obj = resolve(name)
    module, _, attr = name.rpartition(".")
    owner = getattr(obj, "__module__", "")
    if owner.startswith("tau.") and hasattr(obj, "__name__"):
        return owner, obj.__name__
    package = f"tau.{module}" if module else "tau"
    for node in tree_of(package).body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and attr in [a.asname or a.name for a in node.names]
        ):
            return node.module, attr
    return package, attr


def bound(module: str, name: str) -> tuple[str, object]:
    """(module it comes from, object) of a name written in `module`, imports under
    `if TYPE_CHECKING:` included."""
    for node in ast.walk(tree_of(module)):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return node.module, getattr(importlib.import_module(node.module), alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return alias.name, importlib.import_module(alias.name.split(".")[0])
    here = importlib.import_module(module)
    if hasattr(here, name):
        return module, getattr(here, name)
    if hasattr(builtins, name):
        return "builtins", getattr(builtins, name)
    raise SurfaceError(f"{module}: cannot tell what {name} means")


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
        source, value = bound(module, default.id)
        if type(value) in (int, float, str):
            written = getattr(defined(source, default.id)[0], "value", None)
            return ast.unparse(written) if written is not None else repr(value)
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
    args = node.args
    exprs = [
        a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs) if a.annotation
    ]
    exprs += [s.annotation for s in (args.vararg, args.kwarg) if s and s.annotation]
    exprs += [node.returns] if node.returns else []
    counted = not node.name.startswith("__")
    doc = ast.get_docstring(node)
    if "property" in decorators:
        head = f"property {node.name}: {unparse(node.returns) or '...'}"
        return Item(
            "property", node.name, head, doc=doc, counted=counted, exprs=exprs, module=module
        )
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {unparse(node.returns)}" if node.returns else ""
    head = signature(keyword, node.name, render_args(node.args, module), ret)
    return Item(
        "def", node.name, head, decorators, doc, counted=counted, exprs=exprs, module=module
    )


def public_member(name: str) -> bool:
    return not name.startswith("_") or name in USED_DUNDERS


def member_item(
    stmt: ast.stmt, body: Sequence[ast.stmt], module: str, cls: type | None
) -> Item | None:
    """The documented member a statement of a class body makes, if it makes one."""
    after = attribute_doc(body, body.index(stmt))
    match stmt:
        case ast.FunctionDef() | ast.AsyncFunctionDef() if public_member(stmt.name):
            return function_item(stmt, module)
        case ast.ClassDef() if public_member(stmt.name):
            return class_item(stmt, module, getattr(cls, stmt.name, None))
        case ast.AnnAssign(target=ast.Name(id=name)) if public_member(name):
            head = f"{name}: {unparse(stmt.annotation)}"
            head += f" = {unparse(stmt.value)}" if stmt.value is not None else ""
            return Item("field", name, head, doc=after, exprs=[stmt.annotation], module=module)
        case ast.Assign(targets=[ast.Name(id=name)]) if public_member(name):
            return Item("field", name, f"{name} = {unparse(stmt.value)}", doc=after, module=module)
        case _:
            return None


def class_members(cls: type | None, node: ast.ClassDef, module: str) -> list[Item]:
    """The class's own members, then those of its underscore bases that it does not override.
    A name that a public base of tau declares is that base's to document."""
    bases = cls.__mro__[1:] if cls else ()
    declared = {
        n
        for b in bases
        if id(b) in PUBLIC_IDS
        for part in b.__mro__  # a public base documents its underscore bases' members too
        if part is b or (part.__module__.startswith("tau.") and part.__name__.startswith("_"))
        for n in (*vars(part), *vars(part).get("__annotations__", {}))
        if n not in ("__init__", "__new__")  # a subclass's constructor is its own signature
    }
    sources = [(cls, node, module)]
    for base in bases:
        if base.__module__.startswith("tau.") and base.__name__.startswith("_"):
            base_node, _ = defined(base.__module__, base.__name__)
            assert isinstance(base_node, ast.ClassDef)
            sources.append((base, base_node, base.__module__))
    members: dict[str, Item] = {}
    for owner, owner_node, owner_module in sources:
        for stmt in owner_node.body:
            item = member_item(stmt, owner_node.body, owner_module, owner)
            if item and item.name not in declared:
                members.setdefault(item.name, item)
    return list(members.values())


def class_item(node: ast.ClassDef, module: str, cls: type | None) -> Item:
    shown = [b for b in node.bases if not (isinstance(b, ast.Name) and b.id.startswith("_"))]
    bases = [ast.unparse(b) for b in shown]
    bases += [f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords if k.arg]
    head = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    decorators = decorator_names(node)
    item = Item(
        "class", node.name, head, decorators, ast.get_docstring(node), [], True, shown, module
    )
    item.members = class_members(cls, node, module)
    return item


def item_of(module: str, name: str) -> Item:
    node, doc = defined(module, name)
    counted = not name.startswith("__")
    match node:
        case ast.ClassDef():
            return class_item(node, module, getattr(importlib.import_module(module), name, None))
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            item = function_item(node, module)
            assert item is not None
            return item
        case ast.AnnAssign(annotation=annotation, value=value):
            head = f"{name}: {unparse(annotation)}" + (f" = {unparse(value)}" if value else "")
            return Item(
                "alias", name, head, doc=doc, counted=counted, exprs=[annotation], module=module
            )
        case ast.Assign(value=value):
            head = f"{name} = {unparse(value)}"
            # data (a tuple of hook names, a number) is not a type: its strings are not names
            exprs = [] if isinstance(value, ast.Constant | ast.Tuple | ast.Call) else [value]
            return Item("alias", name, head, doc=doc, counted=counted, exprs=exprs, module=module)
        case _:
            raise SurfaceError(f"{module}.{name}: cannot document a {type(node).__name__}")


# ---- the groups ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    reader: str
    names: tuple[str, ...]


@cache
def config() -> dict[str, typing.Any]:
    return tomllib.loads(GROUPS.read_text(encoding="utf-8"))


def load_groups() -> list[Group]:
    groups = [
        Group(g["id"], g["title"], g["reader"].strip(), tuple(g["names"]))
        for g in config()["group"]
    ]
    ids = [g.id for g in groups]
    if len(set(ids)) != len(ids):
        raise SurfaceError(f"{GROUPS.name}: a group id is used twice")
    return groups


PUBLIC_IDS: set[int] = (
    set()
)  # id() of every public object: a base in this set documents its members


def public_items(groups: Sequence[Group]) -> dict[str, Item]:
    """Every public name's item. `made_by_tau` classes are tau's to construct: no `__init__`."""
    names = [name for g in groups for name in g.names]
    PUBLIC_IDS.update(id(resolve(name)) for name in names)
    items = {name: item_of(*home(name)) for name in names}
    for name in config().get("made_by_tau", []):
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
    exported = set(tau.__all__)
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


def named_in(expr: ast.AST) -> Iterator[str]:
    """The names an annotation uses, including those in string annotations (not in a Literal)."""
    if isinstance(expr, ast.Name):
        yield expr.id
    elif isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        with contextlib.suppress(SyntaxError):  # not every string is a type
            yield from named_in(ast.parse(expr.value, mode="eval").body)
    elif isinstance(expr, ast.Subscript) and ast.unparse(expr.value).endswith("Literal"):
        yield from named_in(expr.value)
    else:
        for child in ast.iter_child_nodes(expr):
            yield from named_in(child)


def walk_items(item: Item) -> Iterator[Item]:
    yield item
    for member in item.members:
        yield from walk_items(member)


def refusal(source: str, obj: object) -> str | None:
    """Why a public signature may not name `obj`, bound from module `source`; None if it may.
    A name is fine if it is public in tau, the standard library's, or on the allow list."""
    top = source.split(".", maxsplit=1)[0]
    if top == "tau":
        if isinstance(obj, typing.TypeVar | typing.ParamSpec | typing.TypeVarTuple):
            return None
        if id(obj) in PUBLIC_IDS:
            return None
        return f"which tau defines ({source}) but does not publish"
    if top in sys.stdlib_module_names or top == "builtins" or top in config()["foreign_types"]:
        return None
    return f"which comes from {top}, neither tau, the standard library nor `foreign_types`"


def unpublished_names(items: Mapping[str, Item]) -> list[str]:
    """The names a public signature uses that it may not (see `refusal`)."""
    problems: set[str] = set()
    for name, item in sorted(items.items()):
        for sub in walk_items(item):
            for used in sorted({n for expr in sub.exprs for n in named_in(expr)}):
                source, obj = bound(sub.module, used)
                why = refusal(source, obj)
                if why:
                    where = f"{name}.{sub.name}" if sub is not item else name
                    problems.add(f"{where} names {used}, {why}")
    return sorted(problems)


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
    problems += unpublished_names(items)
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


def main(args: list[str]) -> int:
    if args not in ([], ["--check"], ["--undocumented"]):
        print("usage: scripts/api_docs.py [--check | --undocumented]", file=sys.stderr)
        return 2
    try:
        if args == ["--undocumented"]:
            for group, names in undocumented().items():
                print(f"{group}: {len(names)}")
                for name in names:
                    print(f"  {name}")
            return 0
        files = outputs()
        status = sync(files, check=args == ["--check"], label="api_docs", owned=[REFERENCE])
        if args == ["--check"]:
            problems = stub_docs.mismatches()
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
