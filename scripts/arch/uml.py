"""Generate class diagrams (mermaid and DOT) from the five tau source trees.

Parses with ``ast`` only, so no module is imported and no import side effect
runs. Names are resolved through ``import`` / ``from ... import`` statements,
package re-exports, ``import x as y`` and module-level ``A = B`` aliases, so
``tau_llm.Model`` and ``tau_llm.types.Model`` are the same node.

Per class it records: bases, stereotypes (dataclass, Protocol, ABC, Enum,
TypedDict, NamedTuple, pydantic BaseModel), class-level annotated fields,
``self.x`` assignments in ``__init__`` / ``__post_init__`` (type from an
annotation, a constructor call, or an annotated parameter), and methods with
signatures (async marked). Edges are labelled by kind:

    inherits    subclass -> base
    has-field   annotation of a field names a class
    constructs  ``self.x = SomeClass(...)`` in ``__init__`` / ``__post_init__``
    param-type  an ``__init__`` parameter is annotated with a class

Selection: ``--classes A,B`` and/or ``--module pkg.mod`` (module or submodules),
widened by ``--depth N`` along outbound edges (``--incoming`` also follows
edges pointing at the selection). Classes at depth 0 show members per
``--members``; neighbours show their name only unless ``--neighbour-members``.

Usage:
    python scripts/arch/uml.py --classes AgentSession,Cursor --depth 1 --name x
    python scripts/arch/uml.py --module tau_llm.types --members public --name y
    python scripts/arch/uml.py --classes Cursor --diff-against old/x.json

Outputs in ``--out`` (default ``build/arch/uml``): ``NAME.mmd``, ``NAME.dot``,
``NAME.svg`` (``dot -Tsvg``), ``NAME.json`` (the class model), and with
``--themed-svg`` also ``NAME.themed.svg`` (via ``svg_theme.py``). Node and edge
counts are printed. With ``--diff-against`` the added / removed / changed
classes, members and edges versus the earlier JSON are printed as well.
"""

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_TREES = ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta")
EDGE_KINDS = ("inherits", "has-field", "constructs", "param-type")
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
MAX_SIG = 90


@dataclass
class ModuleInfo:
    """Symbol table for one module: class defs and name aliases."""

    name: str
    path: Path
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)


def uml_module_name(src: Path, path: Path) -> str:
    """Return the dotted module name of ``path`` under source root ``src``."""
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def uml_collect_aliases(mod: ModuleInfo, tree: ast.Module) -> None:
    """Fill ``mod.aliases`` / ``mod.stars`` from every import and ``A = B`` alias."""
    is_pkg = mod.path.name == "__init__.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    mod.aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    mod.aliases.setdefault(top, top)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = mod.name.split(".")
                if not is_pkg:
                    pkg = pkg[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([base] if base else []))
            for a in node.names:
                if a.name == "*":
                    mod.stars.append(base)
                else:
                    mod.aliases[a.asname or a.name] = f"{base}.{a.name}"
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    mod.aliases[t.id] = ast.unparse(node.value)


def uml_top_classes(body: list[ast.stmt]) -> list[ast.ClassDef]:
    """Return class definitions at module level, including inside if/try blocks."""
    out: list[ast.ClassDef] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            out.append(node)
        elif isinstance(node, ast.If):
            out += uml_top_classes(node.body) + uml_top_classes(node.orelse)
        elif isinstance(node, ast.Try):
            out += uml_top_classes(node.body)
    return out


class Index:
    """Parsed modules of the five source trees with cross-module name resolution."""

    def __init__(self, root: Path) -> None:
        """Parse every ``.py`` file under each source tree of ``root``."""
        self.modules: dict[str, ModuleInfo] = {}
        for tree_dir in SRC_TREES:
            src = root / tree_dir / "src"
            if not src.is_dir():
                raise SystemExit(f"missing source tree {src}")
            for path in sorted(src.rglob("*.py")):
                name = uml_module_name(src, path)
                mod = ModuleInfo(name, path)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for cls in uml_top_classes(tree.body):
                    mod.classes[cls.name] = cls
                uml_collect_aliases(mod, tree)
                self.modules[name] = mod
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for mod in self.modules.values():
            for cname in mod.classes:
                self.by_name[cname].append(f"{mod.name}.{cname}")

    def resolve_qual(self, qual: str, seen: frozenset[str] = frozenset()) -> str | None:
        """Resolve a dotted path to the qualified name of an internal class, or None."""
        if qual in seen:
            return None
        seen = seen | {qual}
        parts = qual.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mname = ".".join(parts[:i])
            mod = self.modules.get(mname)
            if mod is None:
                continue
            attr, rest = parts[i], parts[i + 1 :]
            if rest:
                if attr in mod.classes:
                    return None
                target = mod.aliases.get(attr)
                if target is not None:
                    return self.resolve_qual(".".join([target, *rest]), seen)
                sub = f"{mname}.{attr}"
                if sub in self.modules:
                    return self.resolve_qual(".".join([sub, *rest]), seen)
                return None
            if attr in mod.classes:
                return f"{mname}.{attr}"
            target = mod.aliases.get(attr)
            if target is not None:
                return self.resolve_qual(target, seen)
            for star in mod.stars:
                got = self.resolve_qual(f"{star}.{attr}", seen)
                if got:
                    return got
            return None
        return None

    def resolve_in(self, modname: str, dotted: str) -> str | None:
        """Resolve a name as written inside module ``modname``."""
        mod = self.modules[modname]
        first, _, rest = dotted.partition(".")
        if first in mod.classes and not rest:
            return f"{modname}.{first}"
        if first in mod.aliases:
            target = mod.aliases[first]
            return self.resolve_qual(f"{target}.{rest}" if rest else target)
        return None


def uml_names_in(expr: ast.expr) -> list[str]:
    """Return dotted names appearing in an annotation expression (forward refs parsed)."""
    out: list[str] = []

    def visit(node: ast.AST) -> None:
        """Collect Name / Attribute chains, parsing string constants as annotations."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                visit(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                pass
        elif isinstance(node, ast.Attribute):
            text = ast.unparse(node)
            if re.fullmatch(r"[\w.]+", text):
                out.append(text)
            else:
                visit(node.value)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    visit(expr)
    return out


def uml_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef, drop_first: bool) -> str:
    """Render ``(args) -> ret`` for a function, dropping self/cls when asked."""
    a = fn.args
    pos = [*a.posonlyargs, *a.args]
    defaults: list[ast.expr | None] = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    items: list[str] = []

    def one(arg: ast.arg, default: ast.expr | None) -> str:
        """Format one parameter with annotation and a ``=…`` marker for defaults."""
        text = arg.arg
        if arg.annotation is not None:
            text += f": {ast.unparse(arg.annotation)}"
        return text + ("=…" if default is not None else "")

    for i, (arg, d) in enumerate(zip(pos, defaults, strict=True)):
        if i == 0 and drop_first:
            continue
        items.append(one(arg, d))
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            items.append("/")
    if a.vararg:
        items.append("*" + one(a.vararg, None))
    elif a.kwonlyargs:
        items.append("*")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        items.append(one(arg, d))
    if a.kwarg:
        items.append("**" + one(a.kwarg, None))
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"({', '.join(items)}){ret}"


def uml_decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    """Return the last identifier of each decorator expression."""
    out: set[str] = set()
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        out.add(ast.unparse(target).rsplit(".", 1)[-1])
    return out


def uml_infer(
    node: ast.expr, params: dict[str, ast.expr | None]
) -> tuple[list[str], list[str], bool]:
    """Infer ``(type texts, constructed dotted names, from_annotation)`` for an RHS."""
    if isinstance(node, ast.Call):
        text = ast.unparse(node.func)
        if re.fullmatch(r"[\w.]+", text):
            return [text], [text], False
        return [], [], False
    if isinstance(node, ast.Name) and params.get(node.id) is not None:
        return [ast.unparse(params[node.id])], [], True  # type: ignore[arg-type]
    if isinstance(node, ast.Constant):
        return ["None" if node.value is None else type(node.value).__name__], [], False
    kinds = {ast.List: "list", ast.Dict: "dict", ast.Set: "set", ast.Tuple: "tuple"}
    for k, v in kinds.items():
        if isinstance(node, k):
            return [v], [], False
    if isinstance(node, ast.BoolOp):
        types, built, ann = [], [], False
        for v in node.values:
            t, b, a = uml_infer(v, params)
            types += t
            built += b
            ann = ann or a
        return list(dict.fromkeys(types)), built, ann
    if isinstance(node, ast.IfExp):
        t1, b1, a1 = uml_infer(node.body, params)
        t2, b2, a2 = uml_infer(node.orelse, params)
        return list(dict.fromkeys(t1 + t2)), b1 + b2, a1 or a2
    return [], [], False


def uml_class_model(index: Index, modname: str, cls: ast.ClassDef) -> dict:
    """Build the JSON-able model of one class, including raw edge targets."""
    qname = f"{modname}.{cls.name}"
    bases = [ast.unparse(b) for b in cls.bases]
    keywords = {k.arg: ast.unparse(k.value) for k in cls.keywords if k.arg}
    base_tails = {b.rsplit(".", 1)[-1].split("[")[0] for b in bases}
    decos = uml_decorator_names(cls)
    stereo: list[str] = []
    if "dataclass" in decos:
        stereo.append("dataclass")
    if "Protocol" in base_tails:
        stereo.append("Protocol")
    if "ABC" in base_tails or "ABCMeta" in keywords.get("metaclass", ""):
        stereo.append("ABC")
    if base_tails & ENUM_BASES:
        stereo.append("Enum")
    if "TypedDict" in base_tails:
        stereo.append("TypedDict")
    if "NamedTuple" in base_tails:
        stereo.append("NamedTuple")
    if "BaseModel" in base_tails:
        stereo.append("BaseModel")
    fields: list[dict] = []
    methods: list[dict] = []
    edges: list[dict] = []
    seen_f: set[str] = set()
    seen_m: set[str] = set()

    def add_edge(kind: str, dotted: str, label: str) -> None:
        """Record an edge to ``dotted`` if it resolves to an internal class."""
        tgt = index.resolve_in(modname, dotted)
        if tgt and tgt != qname:
            edges.append({"kind": kind, "target": tgt, "label": label})

    def add_field(name: str, typ: str | None, how: str) -> None:
        """Add a field once; the first sighting wins."""
        if name in seen_f:
            return
        seen_f.add(name)
        fields.append({"name": name, "type": typ, "how": how})

    for b in bases:
        add_edge("inherits", b.split("[")[0], "")
    enum = "Enum" in stereo
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann = ast.unparse(node.annotation)
            add_field(node.target.id, ann, "annotation")
            for n in uml_names_in(node.annotation):
                add_edge("has-field", n, node.target.id)
        elif enum and isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    add_field(t.id, None, "member")
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fd = uml_decorator_names(node)
        kind = "method"
        if "property" in fd or "cached_property" in fd:
            kind = "property"
        elif "staticmethod" in fd:
            kind = "static"
        elif "classmethod" in fd:
            kind = "classmethod"
        if "setter" in fd or "deleter" in fd or "overload" in fd:
            continue
        if node.name in seen_m:
            continue
        seen_m.add(node.name)
        drop = kind != "static"
        methods.append(
            {
                "name": node.name,
                "sig": uml_signature(node, drop),
                "async": isinstance(node, ast.AsyncFunctionDef),
                "kind": kind,
                "abstract": "abstractmethod" in fd,
            }
        )
        if node.name in ("__init__", "__post_init__"):
            params: dict[str, ast.expr | None] = {
                a.arg: a.annotation
                for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            }
            if node.name == "__init__":
                for pname, ann in params.items():
                    if ann is not None and pname not in ("self", "cls"):
                        for n in uml_names_in(ann):
                            add_edge("param-type", n, pname)
            for sub in ast.walk(node):
                target, value, ann = None, None, None
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                    target, value = sub.targets[0], sub.value
                elif isinstance(sub, ast.AnnAssign):
                    target, value, ann = sub.target, sub.value, sub.annotation
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    continue
                fname = target.attr
                if ann is not None:
                    add_field(fname, ast.unparse(ann), "init-annotation")
                    for n in uml_names_in(ann):
                        add_edge("has-field", n, fname)
                    if isinstance(value, ast.Call):
                        add_edge("constructs", ast.unparse(value.func), fname)
                elif value is not None:
                    types, built, from_ann = uml_infer(value, params)
                    add_field(
                        fname,
                        " | ".join(types) if types else None,
                        "init-annotation-param" if from_ann else "init-inferred",
                    )
                    for b in built:
                        add_edge("constructs", b, fname)
                    if from_ann:
                        for t in types:
                            for n in uml_names_in(ast.parse(t, mode="eval").body):
                                add_edge("has-field", n, fname)
    if any(m["abstract"] for m in methods) and not {"ABC", "Protocol"} & set(stereo):
        stereo.append("abstract")
    return {
        "name": cls.name,
        "module": modname,
        "line": cls.lineno,
        "stereotypes": stereo,
        "bases": bases,
        "fields": fields,
        "methods": methods,
        "edges": edges,
    }


def uml_all_models(index: Index) -> dict[str, dict]:
    """Return the model of every class in the index, keyed by qualified name."""
    out: dict[str, dict] = {}
    for mname in sorted(index.modules):
        mod = index.modules[mname]
        for cname in sorted(mod.classes):
            out[f"{mname}.{cname}"] = uml_class_model(index, mname, mod.classes[cname])
    return out


def uml_lookup(index: Index, models: dict[str, dict], token: str) -> str:
    """Resolve a user-supplied class token (simple name, qname or re-export) or exit."""
    if token in models:
        return token
    if "." in token:
        got = index.resolve_qual(token)
        if got:
            return got
        raise SystemExit(f"class {token!r} not found in the five source trees")
    cands = sorted(index.by_name.get(token, []))
    if not cands:
        raise SystemExit(f"class {token!r} not found in the five source trees")
    if len(cands) > 1:
        raise SystemExit(f"class name {token!r} is ambiguous; use one of: {', '.join(cands)}")
    return cands[0]


def uml_select(
    models: dict[str, dict],
    seeds: list[str],
    depth: int,
    kinds: set[str],
    incoming: bool,
    prune: set[str],
) -> dict[str, int]:
    """Return ``{qname: depth}`` for the BFS closure of ``seeds`` over chosen edge kinds."""
    out = {s: 0 for s in seeds}
    rev: dict[str, list[str]] = defaultdict(list)
    if incoming:
        for q, m in models.items():
            for e in m["edges"]:
                if e["kind"] in kinds:
                    rev[e["target"]].append(q)
    frontier = list(seeds)
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for q in frontier:
            if q in prune and out[q] > 0:
                continue
            outs = [e["target"] for e in models[q]["edges"] if e["kind"] in kinds]
            for t in [*outs, *rev.get(q, [])]:
                if t in models and t not in out:
                    out[t] = d
                    nxt.append(t)
        frontier = nxt
    return out


def uml_grouped_edges(
    models: dict[str, dict], selected: dict[str, int], kinds: set[str]
) -> list[dict]:
    """Merge edges between selected classes into one per (src, dst, kind)."""
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for q in selected:
        for e in models[q]["edges"]:
            if e["kind"] in kinds and e["target"] in selected:
                key = (q, e["target"], e["kind"])
                if e["label"] and e["label"] not in groups[key]:
                    groups[key].append(e["label"])
                else:
                    groups[key]
    out = []
    for (s, t, k), labels in sorted(groups.items()):
        label = ", ".join(labels[:3]) + (f" +{len(labels) - 3}" if len(labels) > 3 else "")
        out.append({"src": s, "dst": t, "kind": k, "label": label})
    return out


def uml_member_lines(m: dict, mode: str) -> tuple[list[str], list[str]]:
    """Return display ``(field lines, method lines)`` for a class under ``--members``."""
    if mode == "none":
        return [], []

    def show(name: str) -> bool:
        """Whether a member name passes the public/all filter."""
        return (
            mode == "all"
            or not name.startswith("_")
            or (name.startswith("__") and name.endswith("__"))
        )

    flines = []
    for f in m["fields"]:
        if show(f["name"]):
            flines.append(f["name"] + (f": {f['type']}" if f["type"] else ""))
    mlines = []
    for me in m["methods"]:
        if not show(me["name"]):
            continue
        sig = me["sig"]
        if len(sig) > MAX_SIG:
            sig = sig[: MAX_SIG - 1] + "…"
        pre = "async " if me["async"] else ""
        if me["kind"] == "property":
            mlines.append(f"{me['name']}{sig.replace('()', '', 1)} [property]")
        else:
            tag = {"static": " [static]", "classmethod": " [classmethod]"}.get(me["kind"], "")
            tag += " [abstract]" if me["abstract"] else ""
            mlines.append(f"{pre}{me['name']}{sig}{tag}")
    return flines, mlines


def uml_mermaid(
    models: dict[str, dict], selected: dict[str, int], edges: list[dict], members: str, nbr: str
) -> str:
    """Render a mermaid ``classDiagram``."""

    def ident(q: str) -> str:
        """Return a mermaid-safe identifier for a qualified name."""
        return re.sub(r"\W", "_", q)

    def clean(s: str) -> str:
        """Replace characters mermaid member syntax cannot carry."""
        return s.translate(
            str.maketrans({"[": "‹", "]": "›", "<": "‹", ">": "›", "{": "(", "}": ")", "~": "-"})
        )

    lines = ["classDiagram"]
    for q in sorted(selected):
        m = models[q]
        mode = members if selected[q] == 0 else nbr
        fl, ml = uml_member_lines(m, mode)
        lines.append(f'    class {ident(q)}["{m["name"]}"] {{')
        for s in m["stereotypes"]:
            lines.append(f"        <<{s}>>")
        lines += [f"        +{clean(x)}" for x in fl]
        for x in ml:
            lines.append(f"        +{clean(x)}")
        lines.append("    }")
    arrow = {
        "inherits": "{dst} <|-- {src}",
        "has-field": "{src} --> {dst}",
        "constructs": "{src} ..> {dst}",
        "param-type": "{src} ..> {dst}",
    }
    for e in edges:
        text = arrow[e["kind"]].format(src=ident(e["src"]), dst=ident(e["dst"]))
        label = e["kind"] + (f" {e['label']}" if e["label"] else "")
        lines.append(f"    {text} : {clean(label)}")
    return "\n".join(lines) + "\n"


def uml_dot_escape(s: str) -> str:
    """Escape a string for use inside a DOT record label."""
    return re.sub(r'([{}|<>"\\])', r"\\\1", s)


def uml_dot(
    models: dict[str, dict],
    selected: dict[str, int],
    edges: list[dict],
    members: str,
    nbr: str,
    mark: str | None,
    rankdir: str,
) -> str:
    """Render DOT with record nodes, Courier text and svg_theme-compatible classes."""
    out = [
        "digraph uml {",
        f'  rankdir={rankdir}; node [shape=record, fontname="Courier", fontsize=10];',
        '  edge [fontname="Courier", fontsize=9];',
    ]

    def nid(q: str) -> str:
        """Return a quoted DOT node id."""
        return f'"{q}"'

    for q in sorted(selected):
        m = models[q]
        mode = members if selected[q] == 0 else nbr
        fl, ml = uml_member_lines(m, mode)
        title = ""
        if m["stereotypes"]:
            title = "«" + ", ".join(m["stereotypes"]) + "»\\n"
        title += uml_dot_escape(m["name"])
        parts = [title]
        if mode != "none":
            parts.append("".join(uml_dot_escape(x) + "\\l" for x in fl) or " ")
            parts.append("".join(uml_dot_escape(x) + "\\l" for x in ml) or " ")
        cls = "mark" if q == mark else ("muted" if selected[q] > 0 else "")
        attr = f', class="{cls}"' if cls else ""
        out.append(f'  {nid(q)} [label="{{{"|".join(parts)}}}"{attr}];')
    for e in edges:
        attrs = [f'label="{e["label"]}"'] if e["label"] else []
        if e["kind"] == "inherits":
            attrs.append("arrowhead=empty")
        elif e["kind"] == "has-field":
            attrs.append("arrowhead=vee")
        elif e["kind"] == "constructs":
            attrs += ['class="lazy"', "arrowhead=vee", "style=dashed"]
        else:
            attrs += ['class="soft"', "arrowhead=vee", "style=dashed"]
        out.append(f"  {nid(e['src'])} -> {nid(e['dst'])} [{', '.join(attrs)}];")
    out.append("}")
    return "\n".join(out) + "\n"


def uml_diff(old: dict, new: dict) -> list[str]:
    """Return human-readable added / removed / changed lines between two model dumps."""
    oc, nc = old["classes"], new["classes"]
    lines: list[str] = []
    for q in sorted(set(nc) - set(oc)):
        lines.append(f"+ class {q}")
    for q in sorted(set(oc) - set(nc)):
        lines.append(f"- class {q}")
    for q in sorted(set(oc) & set(nc)):
        o, n = oc[q], nc[q]
        for key in ("bases", "stereotypes"):
            if o[key] != n[key]:
                lines.append(f"~ {q}: {key} {o[key]} -> {n[key]}")
        for what, key in (("field", "fields"), ("method", "methods")):
            om = {x["name"]: x for x in o[key]}
            nm = {x["name"]: x for x in n[key]}
            for name in sorted(set(nm) - set(om)):
                lines.append(f"+ {q}.{name} ({what}) {uml_desc(nm[name])}")
            for name in sorted(set(om) - set(nm)):
                lines.append(f"- {q}.{name} ({what}) {uml_desc(om[name])}")
            for name in sorted(set(om) & set(nm)):
                if uml_desc(om[name]) != uml_desc(nm[name]):
                    lines.append(
                        f"~ {q}.{name} ({what}) {uml_desc(om[name])}  ->  {uml_desc(nm[name])}"
                    )
        oe = {(e["kind"], e["target"], e["label"]) for e in o["edges"]}
        ne = {(e["kind"], e["target"], e["label"]) for e in n["edges"]}
        for k, t, lab in sorted(ne - oe):
            lines.append(f"+ edge {q} -{k}-> {t} {lab}".rstrip())
        for k, t, lab in sorted(oe - ne):
            lines.append(f"- edge {q} -{k}-> {t} {lab}".rstrip())
    return lines


def uml_desc(member: dict) -> str:
    """Return a comparable one-line description of a field or method record."""
    if "sig" in member:
        return f"{'async ' if member['async'] else ''}{member['kind']} {member['sig']}"
    return str(member["type"])


def uml_render(dot_text: str, out_svg: Path, themed: Path | None, title: str) -> None:
    """Run ``dot -Tsvg`` and optionally the svg_theme pipeline."""
    if shutil.which("dot") is None:
        raise SystemExit("graphviz `dot` not on PATH; install it (apt install graphviz)")
    svg = subprocess.run(
        ["dot", "-Tsvg"], input=dot_text, capture_output=True, text=True, check=True
    ).stdout
    out_svg.write_text(svg, encoding="utf-8")
    if themed is not None:
        theme = Path(__file__).with_name("svg_theme.py")
        subprocess.run(
            [
                sys.executable,
                str(theme),
                str(out_svg),
                str(themed),
                "--title",
                title,
                "--desc",
                title,
            ],
            check=True,
        )


def uml_main(argv: list[str] | None = None) -> int:
    """Entry point: select classes, write all artefacts, print counts and any diff."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--classes", default="", help="comma list of class names or qualified names")
    ap.add_argument("--module", action="append", default=[], help="module path (and submodules)")
    ap.add_argument("--depth", type=int, default=0, help="association hops to include")
    ap.add_argument("--incoming", action="store_true", help="also follow edges into the selection")
    ap.add_argument("--edges", default=",".join(EDGE_KINDS), help="edge kinds to draw and follow")
    ap.add_argument("--prune", default="", help="classes whose outbound edges are not followed")
    ap.add_argument("--members", choices=["public", "all", "none"], default="public")
    ap.add_argument("--neighbour-members", choices=["public", "all", "none"], default="none")
    ap.add_argument("--mark", help="class name to mark with class=mark")
    ap.add_argument("--rankdir", default="BT")
    ap.add_argument("--name", default="uml", help="basename of output files")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "arch" / "uml")
    ap.add_argument("--themed-svg", action="store_true", help="also write NAME.themed.svg")
    ap.add_argument("--diff-against", type=Path, help="earlier NAME.json to compare with")
    args = ap.parse_args(argv)
    if not args.classes and not args.module:
        ap.error("give --classes and/or --module")
    kinds = set(args.edges.split(","))
    if not kinds <= set(EDGE_KINDS):
        ap.error(f"--edges must be a subset of {','.join(EDGE_KINDS)}")
    index = Index(ROOT)
    models = uml_all_models(index)
    seeds = [uml_lookup(index, models, t) for t in args.classes.split(",") if t]
    for mp in args.module:
        found = [
            q for q, m in models.items() if m["module"] == mp or m["module"].startswith(mp + ".")
        ]
        if not found:
            raise SystemExit(f"module {mp!r} holds no classes")
        seeds += found
    seeds = sorted(dict.fromkeys(seeds))
    prune = {uml_lookup(index, models, t) for t in args.prune.split(",") if t}
    selected = uml_select(models, seeds, args.depth, kinds, args.incoming, prune)
    mark = uml_lookup(index, models, args.mark) if args.mark else None
    edges = uml_grouped_edges(models, selected, kinds)
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.out / args.name
    mm = uml_mermaid(models, selected, edges, args.members, args.neighbour_members)
    dt = uml_dot(models, selected, edges, args.members, args.neighbour_members, mark, args.rankdir)
    base.with_suffix(".mmd").write_text(mm, encoding="utf-8")
    base.with_suffix(".dot").write_text(dt, encoding="utf-8")
    dump = {
        "meta": {"nodes": len(selected), "edges": len(edges), "seeds": seeds, "depth": args.depth},
        "classes": {q: {**models[q], "depth": selected[q]} for q in sorted(selected)},
    }
    base.with_suffix(".json").write_text(
        json.dumps(dump, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    themed = base.with_suffix(".themed.svg") if args.themed_svg else None
    uml_render(dt, base.with_suffix(".svg"), themed, f"Class diagram: {args.name}")
    print(f"{args.name}: {len(selected)} nodes, {len(edges)} edges -> {base}.*")
    if args.diff_against:
        old = json.loads(args.diff_against.read_text(encoding="utf-8"))
        diff = uml_diff(old, dump)
        print(f"diff vs {args.diff_against}: {len(diff)} change(s)")
        print("\n".join(diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(uml_main())
