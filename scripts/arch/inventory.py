"""AST inventory of the five ``src`` trees: the term candidates for the glossary.

Measures, without importing any τ module:

* every class: module, line, bases, decorators, stereotype (dataclass, Enum,
  Protocol, ABC, TypedDict, NamedTuple, Exception, BaseModel, plain), annotated
  fields with their annotation text and default, and public methods;
* every module-level ``Literal[...]`` alias (``X = Literal[...]`` and
  ``X: TypeAlias = Literal[...]`` and ``type X = Literal[...]``) with its values;
* every ``Enum`` subclass with its members and values;
* every string vocabulary: an UPPER_CASE module or class name bound to a
  frozenset/set/tuple/list literal of strings, or a dict literal whose keys are
  all strings;
* every discriminator: a class field named ``type``/``kind``/``event``/
  ``role``/``op``/``command``/``state``/``status``/``method``/``customType``
  annotated ``Literal[...]`` or defaulted to a string constant;
* per class name, the distributions that import it by name (``from m import N``)
  or reference it as ``module.N``, giving the terms-by-package table.

Usage:
    python scripts/arch/inventory.py [--out DIR]

Outputs (in ``--out``, default ``build/arch/inventory``):
    inventory.json   all records, sorted
    inventory.md     human-readable tables
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

DISTRIBUTIONS = {
    "tau-llm": "tau-llm/src",
    "tau-agent-core": "tau-agent-core/src",
    "tau-coding-agent": "tau-coding-agent/src",
    "tau-jmfts": "tau-jmfts/src",
    "tau-meta": "tau-meta/src",
}

DISCRIMINATOR_FIELDS = frozenset(
    {
        "type",
        "kind",
        "event",
        "role",
        "op",
        "command",
        "state",
        "status",
        "method",
        "customType",
        "custom_type",
        "stopReason",
        "stop_reason",
    }
)

TAG_KEYS = frozenset({"type", "kind", "customType", "role", "op", "status"})

JSON_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "items",
        "enum",
        "$schema",
    }
)

STEREOTYPE_BASES = {
    "Enum": "Enum",
    "StrEnum": "Enum",
    "IntEnum": "Enum",
    "Flag": "Enum",
    "IntFlag": "Enum",
    "Protocol": "Protocol",
    "ABC": "ABC",
    "TypedDict": "TypedDict",
    "NamedTuple": "NamedTuple",
    "BaseModel": "BaseModel",
    "Exception": "Exception",
    "BaseException": "Exception",
    "RuntimeError": "Exception",
    "ValueError": "Exception",
    "TypeError": "Exception",
    "KeyError": "Exception",
    "LookupError": "Exception",
    "OSError": "Exception",
    "TimeoutError": "Exception",
    "PermissionError": "Exception",
    "FileNotFoundError": "Exception",
    "NotImplementedError": "Exception",
    "AssertionError": "Exception",
    "Generic": "Generic",
}


def inv_src_files() -> list[tuple[str, Path]]:
    """Return ``(distribution, repo-relative path)`` for every src ``.py`` file, sorted."""
    out: list[tuple[str, Path]] = []
    for dist, tree in DISTRIBUTIONS.items():
        base = ROOT / tree
        if not base.is_dir():
            sys.exit(f"inventory: missing src tree {tree}; run from a full τ checkout")
        out += [(dist, p.relative_to(ROOT)) for p in base.rglob("*.py")]
    return sorted(out, key=lambda t: str(t[1]))


def inv_module_name(rel: Path) -> str:
    """Return the dotted module name for a repo-relative src path."""
    parts = list(rel.parts[2:])
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def inv_unparse(node: ast.AST | None) -> str | None:
    """Return the source text of an AST node, or ``None``."""
    return None if node is None else ast.unparse(node)


def inv_base_name(node: ast.expr) -> str:
    """Return the last dotted component of a base-class expression, subscripts stripped."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ast.unparse(node)


def inv_literal_values(node: ast.AST) -> list[Any] | None:
    """Return the constant values of a ``Literal[...]`` expression, or ``None``."""
    if not isinstance(node, ast.Subscript):
        return None
    if inv_base_name(node.value) != "Literal":
        return None
    sl = node.slice
    elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
    vals: list[Any] = []
    for e in elts:
        if isinstance(e, ast.Constant):
            vals.append(e.value)
        else:
            vals.append(ast.unparse(e))
    return vals


def inv_find_literals(node: ast.AST) -> list[Any]:
    """Return every ``Literal`` value nested anywhere in an annotation."""
    vals: list[Any] = []
    for sub in ast.walk(node):
        lv = inv_literal_values(sub)
        if lv is not None:
            vals += lv
    return vals


def inv_string_collection(node: ast.AST) -> list[str] | None:
    """Return the strings of a frozenset/set/tuple/list/dict-keys literal of strings."""
    if isinstance(node, ast.Call) and inv_base_name(node.func) in ("frozenset", "set", "tuple"):
        if len(node.args) == 1:
            return inv_string_collection(node.args[0])
        return None
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        elts = node.elts
    elif isinstance(node, ast.Dict):
        elts = [k for k in node.keys if k is not None]
    else:
        return None
    if not elts or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
        return None
    strs = [e.value for e in elts if isinstance(e, ast.Constant)]
    if isinstance(node, ast.Dict) and set(strs) <= JSON_SCHEMA_KEYWORDS:
        return None
    return strs


def inv_is_upper(name: str) -> bool:
    """Return whether a name is UPPER_CASE (leading underscores allowed)."""
    core = name.lstrip("_")
    return bool(core) and core.upper() == core and any(c.isalpha() for c in core)


def inv_stereotypes(cls: ast.ClassDef) -> list[str]:
    """Return the sorted stereotypes a class carries from its decorators and bases."""
    st: set[str] = set()
    for d in cls.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        name = inv_base_name(target)
        if name in ("dataclass", "define", "frozen"):
            st.add("dataclass")
    for b in cls.bases:
        name = inv_base_name(b)
        if name in STEREOTYPE_BASES:
            st.add(STEREOTYPE_BASES[name])
        elif name.endswith("Error") or name.endswith("Exception"):
            st.add("Exception")
    for kw in cls.keywords:
        if kw.arg == "metaclass" and inv_base_name(kw.value) == "ABCMeta":
            st.add("ABC")
    return sorted(st)


def inv_class_record(
    cls: ast.ClassDef, module: str, rel: Path, dist: str, outer: str | None
) -> dict[str, Any]:
    """Return the inventory record for one class definition."""
    qual = f"{outer}.{cls.name}" if outer else cls.name
    fields: list[dict[str, Any]] = []
    methods: list[str] = []
    enum_members: list[dict[str, Any]] = []
    vocab: list[dict[str, Any]] = []
    discriminators: list[dict[str, Any]] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fname = stmt.target.id
            default = inv_unparse(stmt.value)
            fields.append(
                {
                    "name": fname,
                    "annotation": inv_unparse(stmt.annotation),
                    "default": default,
                    "line": stmt.lineno,
                }
            )
            if fname in DISCRIMINATOR_FIELDS:
                lits = inv_find_literals(stmt.annotation)
                if (
                    not lits
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    lits = [stmt.value.value]
                if not lits and isinstance(stmt.value, ast.Call):
                    for kw in stmt.value.keywords:
                        if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                            lits = [kw.value.value]
                if lits:
                    discriminators.append({"field": fname, "values": lits, "line": stmt.lineno})
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if not isinstance(t, ast.Name):
                    continue
                if "Enum" in inv_stereotypes(cls) and not t.id.startswith("_"):
                    enum_members.append(
                        {"name": t.id, "value": inv_unparse(stmt.value), "line": stmt.lineno}
                    )
                strs = inv_string_collection(stmt.value)
                if strs is not None and inv_is_upper(t.id):
                    vocab.append({"name": f"{qual}.{t.id}", "values": strs, "line": stmt.lineno})
                if t.id in DISCRIMINATOR_FIELDS and isinstance(stmt.value, ast.Constant):
                    discriminators.append(
                        {"field": t.id, "values": [stmt.value.value], "line": stmt.lineno}
                    )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not stmt.name.startswith("_") or (
                stmt.name.startswith("__") and stmt.name.endswith("__")
            ):
                if stmt.name not in ("__init__", "__post_init__", "__repr__", "__eq__", "__hash__"):
                    methods.append(stmt.name)
    return {
        "name": qual,
        "module": module,
        "dist": dist,
        "path": str(rel),
        "line": cls.lineno,
        "bases": [ast.unparse(b) for b in cls.bases],
        "decorators": [ast.unparse(d) for d in cls.decorator_list],
        "stereotypes": inv_stereotypes(cls),
        "fields": fields,
        "methods": sorted(methods),
        "enum_members": enum_members,
        "vocabularies": vocab,
        "discriminators": discriminators,
        "doc": (ast.get_docstring(cls) or "").split("\n", 1)[0],
    }


def inv_scan_file(dist: str, rel: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the classes, aliases, vocabularies and imports found in one file."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
    module = inv_module_name(rel)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def walk_classes(body: list[ast.stmt], outer: str | None) -> None:
        """Record every class in ``body``, recursing into nested classes."""
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                rec = inv_class_record(stmt, module, rel, dist, outer)
                out["classes"].append(rec)
                walk_classes(stmt.body, rec["name"])

    walk_classes(tree.body, None)
    for stmt in tree.body:
        target: str | None = None
        value: ast.AST | None = None
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            target, value = stmt.targets[0].id, stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            target, value = stmt.target.id, stmt.value
        elif type(stmt).__name__ == "TypeAlias":
            target = getattr(stmt, "name").id
            value = getattr(stmt, "value")
        if target is None or value is None:
            continue
        lits = inv_literal_values(value)
        if lits is None and isinstance(value, ast.BinOp):
            nested = inv_find_literals(value)
            lits = nested or None
        if lits is not None:
            out["literals"].append(
                {
                    "name": target,
                    "module": module,
                    "dist": dist,
                    "path": str(rel),
                    "line": stmt.lineno,
                    "values": lits,
                }
            )
            continue
        strs = inv_string_collection(value)
        if strs is not None and inv_is_upper(target):
            out["vocabularies"].append(
                {
                    "name": target,
                    "module": module,
                    "dist": dist,
                    "path": str(rel),
                    "line": stmt.lineno,
                    "values": strs,
                }
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                out["imports"].append(
                    {"dist": dist, "path": str(rel), "from": node.module, "name": a.name}
                )
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            out["attr_refs"].append({"dist": dist, "name": node.attr})
        if isinstance(node, ast.Name):
            out["names"].append({"name": node.id})
        elif isinstance(node, ast.Attribute):
            out["names"].append({"name": node.attr})
        elif isinstance(node, ast.ImportFrom):
            out["names"] += [{"name": a.name} for a in node.names]
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value in TAG_KEYS
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                ):
                    out["tags"].append(
                        inv_tag(k.value, v.value, "construct", dist, rel, node.lineno)
                    )
        elif isinstance(node, ast.Compare):
            key = inv_tag_key(node.left)
            if key is not None:
                for comp in node.comparators:
                    for val in inv_string_collection(comp) or (
                        [comp.value]
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str)
                        else []
                    ):
                        out["tags"].append(inv_tag(key, val, "read", dist, rel, node.lineno))
    return out


def inv_tag_key(node: ast.expr) -> str | None:
    """Return the tag key read by ``x.get("type")`` or ``x["type"]``, else ``None``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in TAG_KEYS
    ):
        return str(node.args[0].value)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in TAG_KEYS
    ):
        return str(node.slice.value)
    return None


def inv_tag(key: str, value: str, mode: str, dist: str, rel: Path, line: int) -> dict[str, Any]:
    """Return one dict-tag site record (a ``{"type": "x"}`` build or a ``["type"] == "x"`` read)."""
    return {"key": key, "value": value, "mode": mode, "dist": dist, "site": f"{rel}:{line}"}


def inv_tag_table(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate dict-tag sites into one sorted row per ``(key, value)``."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sites:
        row = rows.setdefault(
            (s["key"], s["value"]),
            {"key": s["key"], "value": s["value"], "construct": [], "read": [], "dists": set()},
        )
        row[s["mode"]].append(s["site"])
        row["dists"].add(s["dist"])
    out = []
    for _, row in sorted(rows.items()):
        row["dists"] = sorted(row["dists"])
        row["construct"] = sorted(set(row["construct"]))
        row["read"] = sorted(set(row["read"]))
        out.append(row)
    return out


def inv_ref_counts(refs: list[tuple[str, str]], own_path: str) -> dict[str, int]:
    """Count files per distribution that mention a name, excluding the defining file."""
    counts: dict[str, int] = defaultdict(int)
    for dist, path in refs:
        if path != own_path:
            counts[dist] += 1
    return dict(sorted(counts.items()))


def inv_inherit_stereotypes(classes: list[dict[str, Any]]) -> None:
    """Propagate base-class stereotypes (not ``dataclass``) through in-tree bases, in place."""
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in classes:
        by_name[c["name"].split(".")[-1]].append(c)
    changed = True
    while changed:
        changed = False
        for c in classes:
            for b in c["bases"]:
                bname = b.split("[", 1)[0].rsplit(".", 1)[-1]
                parents = [p for p in by_name.get(bname, []) if p["module"] == c["module"]]
                parents = parents or by_name.get(bname, [])
                for p in parents[:1]:
                    extra = set(p["stereotypes"]) - {"dataclass"} - set(c["stereotypes"])
                    if extra:
                        c["stereotypes"] = sorted(set(c["stereotypes"]) | extra)
                        changed = True


def inv_duplicate_names(classes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one row per class basename defined in more than one place."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for c in classes:
        by_name[c["name"].split(".")[-1]].append(
            f"{c['path']}:{c['line']} [{', '.join(c['stereotypes']) or 'plain'}]"
        )
    return [{"name": n, "sites": sorted(s)} for n, s in sorted(by_name.items()) if len(s) > 1]


def inv_collect() -> dict[str, Any]:
    """Return the full inventory across all src trees."""
    classes: list[dict[str, Any]] = []
    literals: list[dict[str, Any]] = []
    vocabularies: list[dict[str, Any]] = []
    imported_by: dict[str, set[str]] = defaultdict(set)
    attr_by: dict[str, set[str]] = defaultdict(set)
    file_refs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    tags: list[dict[str, Any]] = []
    files = inv_src_files()
    for dist, rel in files:
        res = inv_scan_file(dist, rel)
        tags += res["tags"]
        classes += res["classes"]
        literals += res["literals"]
        vocabularies += res["vocabularies"]
        for imp in res["imports"]:
            imported_by[imp["name"]].add(imp["dist"])
        for ref in res["attr_refs"]:
            attr_by[ref["name"]].add(ref["dist"])
        for name in {n["name"] for n in res["names"]}:
            file_refs[name].append((dist, str(rel)))
    inv_inherit_stereotypes(classes)
    for c in classes:
        top = c["name"].split(".")[0]
        users = (imported_by.get(top, set()) | attr_by.get(top, set())) - {c["dist"]}
        c["used_by_other_dists"] = sorted(users)
        c["referencing_files_by_dist"] = inv_ref_counts(file_refs.get(top, []), c["path"])
    for lit in literals:
        users = (imported_by.get(lit["name"], set()) | attr_by.get(lit["name"], set())) - {
            lit["dist"]
        }
        lit["used_by_other_dists"] = sorted(users)
        lit["referencing_files_by_dist"] = inv_ref_counts(
            file_refs.get(lit["name"], []), lit["path"]
        )
    enums = [
        {
            "name": c["name"],
            "module": c["module"],
            "path": c["path"],
            "line": c["line"],
            "members": c["enum_members"],
        }
        for c in classes
        if "Enum" in c["stereotypes"]
    ]
    discriminators = [
        {"class": c["name"], "module": c["module"], "path": c["path"], **d}
        for c in classes
        for d in c["discriminators"]
    ]
    for c in classes:
        vocabularies += [
            {"module": c["module"], "dist": c["dist"], "path": c["path"], **v}
            for v in c["vocabularies"]
        ]
    key = lambda r: (r["path"], r["line"], r.get("name", ""))  # noqa: E731
    return {
        "file_count": len(files),
        "classes": sorted(classes, key=key),
        "literals": sorted(literals, key=key),
        "enums": sorted(enums, key=key),
        "vocabularies": sorted(vocabularies, key=key),
        "discriminators": sorted(discriminators, key=lambda r: (r["path"], r["line"], r["class"])),
        "dict_tags": inv_tag_table(tags),
        "duplicate_class_names": inv_duplicate_names(classes),
    }


def inv_fmt_vals(vals: list[Any], limit: int = 40) -> str:
    """Return a compact, pipe-safe rendering of a value list."""
    shown = ", ".join(f"`{v}`" for v in vals[:limit])
    if len(vals) > limit:
        shown += f", … (+{len(vals) - limit})"
    return shown.replace("|", "\\|")


def inv_markdown(inv: dict[str, Any]) -> str:
    """Return the markdown rendering of the inventory."""
    lines = ["# τ AST inventory", ""]
    lines.append(
        f"Files: {inv['file_count']}. Classes: {len(inv['classes'])}. Literal aliases: {len(inv['literals'])}. "
        f"Enums: {len(inv['enums'])}. Vocabularies: {len(inv['vocabularies'])}. "
        f"Discriminators: {len(inv['discriminators'])}. Dict tags: {len(inv['dict_tags'])}."
    )
    dists = list(DISTRIBUTIONS)
    lines += [
        "",
        "## Terms by package",
        "",
        "Public classes and Literal aliases mentioned outside their defining file. Cells are the number "
        "of other files in that distribution mentioning the name (a name token match, so two same-named "
        "classes share counts).",
        "",
        "| Term | Defined in | " + " | ".join(dists) + " |",
        "|---|---|" + "---|" * len(dists),
    ]
    terms = [c for c in inv["classes"] if "." not in c["name"]] + inv["literals"]
    for t in sorted(terms, key=lambda r: (r["name"].lower(), r["path"])):
        refs = t["referencing_files_by_dist"]
        if t["name"].startswith("_") or not refs:
            continue
        cells = " | ".join(str(refs.get(d, "")) for d in dists)
        lines.append(f"| `{t['name']}` | {t['dist']} ({t['path']}:{t['line']}) | {cells} |")
    lines += ["", "## Class names defined more than once", "", "| Name | Sites |", "|---|---|"]
    for d in inv["duplicate_class_names"]:
        lines.append(f"| `{d['name']}` | {'<br>'.join(d['sites'])} |")
    lines += [
        "",
        "## Dict tags (string-keyed `type`/`kind`/`role`/`op`/`status`/`customType` values)",
        "",
        "Built = a dict literal carrying the tag; read = a comparison against `x.get(key)` / `x[key]`.",
        "",
        "| Key | Value | Built | Read | Dists | First sites |",
        "|---|---|---|---|---|---|",
    ]
    for t in inv["dict_tags"]:
        first = ", ".join((t["construct"] + t["read"])[:3])
        lines.append(
            f"| `{t['key']}` | `{t['value']}` | {len(t['construct'])} | {len(t['read'])} | "
            f"{', '.join(t['dists'])} | {first} |"
        )
    lines += [
        "",
        "## Literal aliases",
        "",
        "| Name | Where | Values | Used by other dists |",
        "|---|---|---|---|",
    ]
    for r in inv["literals"]:
        lines.append(
            f"| `{r['name']}` | {r['path']}:{r['line']} | {inv_fmt_vals(r['values'])} | "
            f"{', '.join(r['used_by_other_dists'])} |"
        )
    lines += ["", "## Enums", "", "| Name | Where | Members |", "|---|---|---|"]
    for r in inv["enums"]:
        mem = ", ".join(f"`{m['name']}={m['value']}`" for m in r["members"]).replace("|", "\\|")
        lines.append(f"| `{r['name']}` | {r['path']}:{r['line']} | {mem} |")
    lines += [
        "",
        "## Discriminators",
        "",
        "| Class | Field | Values | Where |",
        "|---|---|---|---|",
    ]
    for r in inv["discriminators"]:
        lines.append(
            f"| `{r['class']}` | `{r['field']}` | {inv_fmt_vals(r['values'])} | {r['path']}:{r['line']} |"
        )
    lines += ["", "## String vocabularies", "", "| Name | Where | Values |", "|---|---|---|"]
    for r in inv["vocabularies"]:
        lines.append(f"| `{r['name']}` | {r['path']}:{r['line']} | {inv_fmt_vals(r['values'])} |")
    lines += [
        "",
        "## Classes",
        "",
        "| Class | Where | Stereotypes | Bases | Fields | Public methods | Used by other dists |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in inv["classes"]:
        fields = ", ".join(f["name"] for f in c["fields"])
        lines.append(
            f"| `{c['name']}` | {c['path']}:{c['line']} | {', '.join(c['stereotypes'])} | "
            f"{', '.join(c['bases'])} | {fields} | {', '.join(c['methods'])} | "
            f"{', '.join(c['used_by_other_dists'])} |".replace("\n", " ")
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Parse arguments, build the inventory, and write JSON and markdown."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "arch" / "inventory")
    args = ap.parse_args()
    inv = inv_collect()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "inventory.json").write_text(
        json.dumps(inv, indent=1, sort_keys=True, default=str) + "\n"
    )
    (args.out / "inventory.md").write_text(inv_markdown(inv))
    print(
        f"inventory: {len(inv['classes'])} classes, {len(inv['literals'])} literals, "
        f"{len(inv['enums'])} enums, {len(inv['vocabularies'])} vocabularies, "
        f"{len(inv['discriminators'])} discriminators -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
