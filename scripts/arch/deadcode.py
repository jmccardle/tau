"""Find dead-code candidates, unused fields, and unused parameters in the src trees.

Combines three views: vulture candidates (min-confidence 60, with the 100%
subset marked) each classified by an AST/word-boundary reference scan over
src, tests, examples and scripts; ruff ARG/F401/F841 findings; and own AST
checks for fields written but never read, ``self.x`` read but never written,
and parameters never used in the function body.

Categories for vulture candidates:
  a  referenced nowhere (outside its own definition span)
  b  referenced only from tests
  c  referenced only from examples/ or scripts/ (no test reference)
  d  referenced by string or convention only (Textual naming, registering
     decorator, ``__all__``, pyproject entry point, exact string literal)
  e  referenced from src by name (vulture still reports it: recursion or
     references that sit in other dead code)

Usage:
    python scripts/arch/deadcode.py [--out DIR] [--ruff-select CODES]

Outputs (in DIR, default ``build/arch/deadcode``): ``deadcode.json``,
``deadcode.md``, ``vulture_raw.txt``, ``ruff_raw.json``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from hyg_common import (
    DISTRIBUTIONS,
    ROOT,
    SRC_TREES,
    dist_of,
    extra_files,
    src_files,
    test_files,
    tool,
    write_json,
)

CONVENTION = re.compile(
    r"^(action_|on_|_on_|watch_|_watch_|validate_|_validate_|compute_|key_|test_)\w*$|^(compose|render|render_line|"
    r"check_action|get_content_width|get_content_height|get_css_variables|get_theme_variable_defaults|"
    r"BINDINGS|DEFAULT_CSS|CSS|CSS_PATH|TITLE|SUB_TITLE|COMPONENT_CLASSES|SCREENS|MODES|AUTO_FOCUS|"
    r"ALLOW_SELECT|ALLOW_IN_MAXIMIZED_VIEW|DEFAULT_CLASSES|NO_MATCH_MSG|BORDER_TITLE|"
    r"setUp|tearDown|__\w+__)$"
)
PLAIN_DECORATORS = {
    "property", "staticmethod", "classmethod", "abstractmethod", "override", "cached_property",
    "overload", "dataclass", "final", "setter", "getter", "deleter", "wraps", "agent_facing",
    "runtime_checkable", "total_ordering", "contextmanager", "asynccontextmanager", "lru_cache", "cache",
}  # fmt: skip
NEUTRAL_BASES = {"object", "ABC", "Protocol", "Generic", "NamedTuple", "TypedDict"}
V_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): (?P<msg>.*) \((?P<conf>\d+)% confidence(?:, \d+ lines?)?\)$"
)


def cat_of(rel: str) -> str:
    """Return the tree category (src/tests/examples/scripts) of a repo-relative path."""
    if rel.startswith(("examples/", "scripts/")):
        return rel.split("/")[0]
    return "tests" if "/tests/" in rel else "src"


def dname(node: ast.expr) -> str:
    """Return the trailing name of a decorator or base expression."""
    while isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return dname(node.value)
    return ""


class Index:
    """Whole-tree name index: loads, stores, string hits, definitions."""

    def __init__(self) -> None:
        """Parse every python file under src, tests, examples and scripts."""
        self.trees: dict[str, ast.Module] = {}
        for p in [*src_files(), *test_files(), *extra_files()]:
            self.trees[str(p)] = ast.parse((ROOT / p).read_text(), filename=str(p))
        self.loads: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        self.stores: dict[str, int] = defaultdict(int)
        self.strings: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        self.all_exports: set[str] = set()
        self.callargs: set[str] = set()
        self.entry_tokens: set[str] = set()
        for pp in sorted(ROOT.glob("*/pyproject.toml")):
            self.entry_tokens |= set(re.findall(r"[A-Za-z_]\w*", pp.read_text()))
        for rel, tree in self.trees.items():
            self._scan(rel, tree)

    def _scan(self, rel: str, tree: ast.Module) -> None:
        """Record name loads, stores and string literals for one file."""
        cat = cat_of(rel)
        docs: set[int] = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
                    docs.add(id(b[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                self.loads[n.id].append((cat, rel, n.lineno))
            elif isinstance(n, ast.Attribute):
                if isinstance(n.ctx, ast.Store):
                    self.stores[n.attr] += 1
                else:
                    self.loads[n.attr].append((cat, rel, n.lineno))
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    self.loads[a.name.split(".")[-1]].append((cat, rel, n.lineno))
            elif isinstance(n, ast.keyword) and n.arg:
                self.loads[n.arg].append((cat, rel, n.value.lineno))
            elif isinstance(n, ast.Call):
                for a in [*n.args, *[k.value for k in n.keywords]]:
                    if isinstance(a, ast.Name):
                        self.callargs.add(a.id)
                    elif isinstance(a, ast.Attribute):
                        self.callargs.add(a.attr)
                if isinstance(n.func, ast.Name) and n.func.id == "setattr" and len(n.args) > 1:
                    if isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
                        self.stores[n.args[1].value] += 1
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                s = n.value
                if len(s) <= 160 and re.fullmatch(r"[\w.:/\-]+", s):
                    for tok in set(re.findall(r"[A-Za-z_]\w*", s)):
                        self.strings[tok].append((cat, rel, n.lineno))
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if (
                        isinstance(t, ast.Name)
                        and t.id == "__all__"
                        and isinstance(n.value, (ast.List, ast.Tuple))
                    ):
                        for e in n.value.elts:
                            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                self.all_exports.add(e.value)


class Defs:
    """Per-file definition table mapping lines to qualified definitions and class spans."""

    def __init__(self, rel: str, tree: ast.Module) -> None:
        """Walk one src file collecting defs with qualnames, spans and decorators."""
        self.by_line: dict[int, dict] = {}
        self.classes: list[tuple[int, int, str]] = []
        self._walk(tree, "", None)

    def _walk(self, node: ast.AST, prefix: str, cls: str | None) -> None:
        """Recurse through classes and functions recording each definition."""
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + ch.name
                rec = {
                    "qualname": q,
                    "kind": "class" if isinstance(ch, ast.ClassDef) else "function",
                    "start": ch.lineno,
                    "end": ch.end_lineno or ch.lineno,
                    "decorators": sorted({dname(d) for d in ch.decorator_list} - {""}),
                    "class": cls,
                    "node": ch,
                }
                self.by_line[ch.lineno] = rec
                for d in ch.decorator_list:
                    self.by_line.setdefault(d.lineno, rec)
                if isinstance(ch, ast.ClassDef):
                    self.classes.append((ch.lineno, ch.end_lineno or ch.lineno, q))
                    self._walk(ch, q + ".", q)
                else:
                    self._walk(ch, q + ".", cls)
            else:
                self._walk(ch, prefix, cls)

    def enclosing_class(self, line: int) -> str | None:
        """Return the innermost class qualname containing ``line``."""
        c = [x for x in self.classes if x[0] <= line <= x[1]]
        return min(c, key=lambda x: x[1] - x[0])[2] if c else None


def run_vulture(out: Path) -> list[dict]:
    """Run vulture at confidence 60 and 100 and return parsed candidates."""
    res: dict[int, str] = {}
    for conf in (60, 100):
        p = subprocess.run(
            [*tool("vulture"), *SRC_TREES, "--min-confidence", str(conf)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if p.returncode not in (0, 3):
            sys.exit(f"vulture failed ({p.returncode}): {p.stderr}")
        res[conf] = p.stdout
    (out / "vulture_raw.txt").write_text(
        "== min-confidence 60 ==\n" + res[60] + "\n== min-confidence 100 ==\n" + res[100]
    )
    hundred = {ln for ln in res[100].splitlines()}
    cands = []
    for ln in res[60].splitlines():
        m = V_RE.match(ln)
        if not m:
            sys.exit(f"unparsed vulture line: {ln!r}")
        msg = m["msg"]
        km = re.match(r"unused (\w+(?: \w+)?) '(.+)'$", msg)
        if km:
            kind, name = km[1], km[2]
        else:
            kind, name = msg.split(" '")[0], (msg.split("'")[1] if "'" in msg else msg)
        cands.append(
            {
                "file": m["file"],
                "line": int(m["line"]),
                "kind": kind,
                "name": name,
                "confidence": int(m["conf"]),
                "at100": ln in hundred,
                "message": msg,
            }
        )
    return sorted(cands, key=lambda c: (c["file"], c["line"], c["name"]))


def classify(cands: list[dict], idx: Index) -> None:
    """Attach category, reasons and reference counts to each vulture candidate in place."""
    defs = {rel: Defs(rel, t) for rel, t in idx.trees.items() if cat_of(rel) == "src"}
    for c in cands:
        d = defs[c["file"]]
        rec = d.by_line.get(c["line"])
        name = c["name"]
        if rec is not None and rec["node"].name == name:
            span, deco, qual, cls = (
                (rec["start"], rec["end"]),
                rec["decorators"],
                rec["qualname"],
                rec["class"],
            )
        else:
            cls = d.enclosing_class(c["line"])
            span, deco, qual = (c["line"], c["line"]), [], (f"{cls}.{name}" if cls else name)
        c["qualname"] = qual
        c["span"] = list(span)
        c["class_scope"] = cls
        crec = next(
            (r for r in d.by_line.values() if r["kind"] == "class" and r["qualname"] == cls), None
        )
        c["class_bases"] = [dname(b) for b in crec["node"].bases] if crec else []
        c["decorators"] = deco
        refs = [
            r
            for r in idx.loads.get(name, [])
            if not (r[1] == c["file"] and span[0] <= r[2] <= span[1])
        ]
        counts = {
            k: sum(1 for r in refs if r[0] == k) for k in ("src", "tests", "examples", "scripts")
        }
        strs = [
            s
            for s in idx.strings.get(name, [])
            if not (s[1] == c["file"] and span[0] <= s[2] <= span[1])
        ]
        reasons = []
        if CONVENTION.match(name):
            reasons.append("convention-name")
        reg = [x for x in deco if x not in PLAIN_DECORATORS]
        if reg:
            reasons.append("decorator:" + ",".join(reg))
        if name in idx.all_exports:
            reasons.append("__all__")
        if name in idx.entry_tokens:
            reasons.append("pyproject-token")
        c["ref_counts"] = counts
        c["string_hits"] = len(strs)
        c["reasons"] = reasons
        if counts["src"]:
            c["category"] = "e"
        elif reasons:
            c["category"] = "d"
        elif counts["tests"]:
            c["category"] = "b"
        elif counts["examples"] or counts["scripts"]:
            c["category"] = "c"
        elif strs:
            c["category"] = "d"
            c["reasons"] = ["exact-string"]
        else:
            c["category"] = "a"
        c["ref_sample"] = [f"{r[1]}:{r[2]}" for r in sorted(refs)[:3]]


def run_ruff(out: Path, select: str) -> list[dict]:
    """Run ruff with a CLI-only rule selection and return findings."""
    cmd = [
        *tool("ruff"),
        "check",
        "--isolated",
        "--no-cache",
        "--select",
        select,
        "--output-format",
        "json",
        "--exit-zero",
        *SRC_TREES,
    ]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"ruff failed: {p.stderr}")
    raw = json.loads(p.stdout)
    (out / "ruff_raw.json").write_text(p.stdout)
    rows = []
    for r in raw:
        fn = str(Path(r["filename"]).resolve().relative_to(ROOT))
        arg = re.search(r"`([^`]+)`", r["message"])
        rows.append(
            {
                "file": fn,
                "line": r["location"]["row"],
                "col": r["location"]["column"],
                "code": r["code"],
                "message": r["message"],
                "name": arg[1] if arg else "",
            }
        )
    return sorted(rows, key=lambda r: (r["file"], r["line"], r["col"], r["code"]))


def class_info(idx: Index) -> dict[str, list[dict]]:
    """Collect src classes: bases, fields, instance attrs, self-loads, defined names."""
    out: dict[str, list[dict]] = defaultdict(list)
    for rel, tree in idx.trees.items():
        if cat_of(rel) != "src":
            continue

        def visit(node: ast.AST, prefix: str, rel: str = rel) -> None:
            """Walk nested classes collecting one record per ClassDef."""
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    q = prefix + ch.name
                    out[ch.name].append(summarise_class(ch, q, rel))
                    visit(ch, q + ".")
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(ch, prefix + ch.name + ".")

        visit(tree, "")
    return out


def summarise_class(cls: ast.ClassDef, q: str, rel: str) -> dict:
    """Return a record of a class's fields, attribute stores, self loads and names."""
    bases = [dname(b) for b in cls.bases]
    deco = [dname(d) for d in cls.decorator_list]
    flavor = "plain"
    for b in bases:
        if b in ("NamedTuple", "TypedDict", "BaseModel", "Enum", "IntEnum", "StrEnum", "Flag"):
            flavor = b
    if "dataclass" in deco:
        flavor = "dataclass"
    if "define" in deco or "attrs" in deco:
        flavor = "attrs"
    fields: list[dict] = []
    defined: set[str] = set()
    stores: dict[str, int] = {}
    self_loads: dict[str, int] = {}
    special = False
    for st in cls.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(st.name)
            if st.name in ("__getattr__", "__getattribute__", "__setattr__"):
                special = True
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            defined.add(st.target.id)
            fields.append({"name": st.target.id, "line": st.lineno, "origin": "class-annotation"})
        elif isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
                    if t.id == "__slots__":
                        special = True
                    conv = bool(CONVENTION.match(t.id)) or t.id.isupper()
                    reactive = isinstance(st.value, ast.Call) and dname(st.value.func) in (
                        "reactive",
                        "var",
                    )
                    fields.append(
                        {
                            "name": t.id,
                            "line": st.lineno,
                            "origin": "class-assign",
                            "convention": conv or reactive,
                        }
                    )
    for fn in [n for n in ast.walk(cls) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in ("self", "cls")
            ):
                if isinstance(n.ctx, ast.Store):
                    stores.setdefault(n.attr, n.lineno)
                elif isinstance(n.ctx, ast.Load):
                    self_loads.setdefault(n.attr, n.lineno)
    for k, ln in stores.items():
        if k not in defined:
            fields.append({"name": k, "line": ln, "origin": "self-attribute"})
    return {
        "qualname": q,
        "file": rel,
        "line": cls.lineno,
        "bases": bases,
        "flavor": flavor,
        "fields": fields,
        "defined": sorted(defined),
        "stores": sorted(stores),
        "self_loads": self_loads,
        "special": special,
    }


def has_external_ancestor(c: dict, classes: dict[str, list[dict]], seen: set[str]) -> bool:
    """Return True if any transitive base of ``c`` is not a src class or a neutral typing base."""
    for b in c["bases"]:
        if b in NEUTRAL_BASES:
            continue
        if b not in classes or b in seen:
            return b not in seen
        seen.add(b)
        if any(has_external_ancestor(bc, classes, seen) for bc in classes[b]):
            return True
    return False


def field_report(idx: Index) -> dict:
    """Report fields written but never read and self attributes read but never written."""
    classes = class_info(idx)
    wnr, rnw, tests_only = [], [], []
    skipped_external = 0
    for recs in classes.values():
        for c in recs:
            if c["flavor"] in ("Enum", "IntEnum", "StrEnum", "Flag"):
                continue
            for f in c["fields"]:
                n = f["name"]
                if n.startswith("__") and n.endswith("__"):
                    continue
                reads = idx.loads.get(n, [])
                counts = {
                    k: sum(1 for r in reads if r[0] == k)
                    for k in ("src", "tests", "examples", "scripts")
                }
                sh = len(idx.strings.get(n, []))
                row = {
                    "file": c["file"],
                    "class": c["qualname"],
                    "flavor": c["flavor"],
                    "field": n,
                    "line": f["line"],
                    "origin": f["origin"],
                    "convention": bool(f.get("convention")),
                    "reads": counts,
                    "string_hits": sh,
                }
                if sum(counts.values()) == 0:
                    wnr.append(row)
                elif counts["src"] == 0:
                    tests_only.append(row)
            if (
                has_external_ancestor(c, classes, set())
                or c["special"]
                or any(b in ("Exception", "BaseException") for b in c["bases"])
            ):
                skipped_external += 1
                continue
            known = set(c["defined"]) | set(c["stores"])
            for b in c["bases"]:
                for bc in classes.get(b, []):
                    known |= set(bc["defined"]) | set(bc["stores"])
            for k, ln in c["self_loads"].items():
                if k in known or idx.stores.get(k, 0) or (k.startswith("__") and k.endswith("__")):
                    continue
                rnw.append(
                    {
                        "file": c["file"],
                        "class": c["qualname"],
                        "attr": k,
                        "line": ln,
                        "bases": c["bases"],
                    }
                )
    key = lambda r: (r["file"], r["line"], r.get("field", r.get("attr")))  # noqa: E731
    return {
        "written_never_read": sorted(wnr, key=key),
        "read_only_from_tests": sorted(tests_only, key=key),
        "read_never_written": sorted(rnw, key=key),
        "classes_skipped_unresolved_bases": skipped_external,
    }


def is_stub(fn: ast.AST) -> bool:
    """Return True if the function body is only docstring/pass/ellipsis/raise NotImplementedError."""
    body = list(fn.body)  # type: ignore[attr-defined]
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    for s in body:
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            continue
        if isinstance(s, ast.Raise) and dname(s.exc or ast.Constant(0)) == "NotImplementedError":
            continue
        return False
    return True


def param_report(idx: Index, ruff: list[dict]) -> dict:
    """Find parameters never used in their function body and compare with ruff ARG."""
    classes = class_info(idx)
    method_names: dict[str, set[str]] = {
        k: set().union(*[set(c["defined"]) for c in v]) for k, v in classes.items()
    }
    rows = []
    for rel, tree in sorted(idx.trees.items()):
        if cat_of(rel) != "src":
            continue

        def visit(node: ast.AST, prefix: str, cls: ast.ClassDef | None, rel: str = rel) -> None:
            """Walk functions; inspect each one's parameters against its body."""
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    visit(ch, prefix + ch.name + ".", ch)
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inspect(ch, prefix + ch.name, cls, rel)
                    visit(ch, prefix + ch.name + ".", None)
                else:
                    visit(ch, prefix, cls)

        def inspect(
            fn: ast.FunctionDef | ast.AsyncFunctionDef, q: str, cls: ast.ClassDef | None, rel: str
        ) -> None:
            """Append a row per unused parameter of ``fn``."""
            a = fn.args
            params = [(x.arg, "positional") for x in [*a.posonlyargs, *a.args]] + [
                (x.arg, "kwonly") for x in a.kwonlyargs
            ]
            if a.vararg:
                params.append((a.vararg.arg, "vararg"))
            if a.kwarg:
                params.append((a.kwarg.arg, "kwarg"))
            used = {n.id for s in fn.body for n in ast.walk(s) if isinstance(n, ast.Name)}
            dyn = any(
                isinstance(n, ast.Call) and dname(n.func) in ("locals", "vars")
                for s in fn.body
                for n in ast.walk(s)
            )
            deco = sorted({dname(d) for d in fn.decorator_list} - {""})
            for p, kind in params:
                if p in ("self", "cls") or p.startswith("_") or p in used or dyn:
                    continue
                cat = "free-function"
                bases: list[str] = []
                if cls is not None:
                    bases = [dname(b) for b in cls.bases]
                    over = [b for b in bases if b in method_names and fn.name in method_names[b]]
                    ext = [b for b in bases if b not in method_names and b not in NEUTRAL_BASES]
                    cat = (
                        "override-in-tree-base"
                        if over
                        else (
                            "method-external-base"
                            if ext
                            else ("method-protocol-or-abc-base" if bases else "method-no-base")
                        )
                    )
                    if "staticmethod" in deco and not bases:
                        cat = "method-no-base"
                reg = [x for x in deco if x not in PLAIN_DECORATORS]
                if reg:
                    cat = "decorated:" + ",".join(reg)
                if is_stub(fn):
                    cat = "stub-body"
                public = not any(part.startswith("_") for part in q.split(".")) or (
                    fn.name.startswith("__") and fn.name.endswith("__")
                )
                nested = cls is None and "." in q
                public = public and not nested
                rows.append(
                    {
                        "nested": nested,
                        "file": rel,
                        "line": fn.lineno,
                        "function": q,
                        "param": p,
                        "kind": kind,
                        "category": cat,
                        "public": public,
                        "passed_as_callback": fn.name in idx.callargs,
                        "bases": bases,
                        "end": fn.end_lineno,
                    }
                )

        visit(tree, "", None)
    arg = [r for r in ruff if r["code"].startswith("ARG")]
    both = ruff_only = 0
    ruff_only_rows = []
    own_keys = {(r["file"], r["param"], r["line"], r["end"]) for r in rows}
    for r in arg:
        hit = any(
            r["file"] == f and r["name"] == p and s <= r["line"] <= e for f, p, s, e in own_keys
        )
        if hit:
            both += 1
        else:
            ruff_only += 1
            ruff_only_rows.append(r)
    ruff_hit_keys = set()
    for r in arg:
        for f, p, s, e in own_keys:
            if r["file"] == f and r["name"] == p and s <= r["line"] <= e:
                ruff_hit_keys.add((f, p, s, e))
    own_only = [
        r for r in rows if (r["file"], r["param"], r["line"], r["end"]) not in ruff_hit_keys
    ]
    for r in rows:
        r["ruff_arg"] = (r["file"], r["param"], r["line"], r["end"]) in ruff_hit_keys
    return {
        "rows": rows,
        "ruff_arg_total": len(arg),
        "ruff_arg_matched_by_own_check": both,
        "ruff_arg_not_in_own_check": ruff_only,
        "own_check_not_in_ruff": len(own_only),
        "ruff_only_rows": ruff_only_rows,
    }


def md_table(rows: list[dict], cols: list[str]) -> list[str]:
    """Render rows as markdown table lines for the given columns."""
    L = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        L.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols) + " |")
    return L


def render(data: dict) -> str:
    """Render the markdown report."""
    V = data["vulture"]
    L = ["# Dead-code report", ""]
    L += [
        "## Vulture candidates by category",
        "",
        "| category | count | at 100% |",
        "|---|---|---|",
    ]
    for k in "abcde":
        vs = [c for c in V if c["category"] == k]
        L.append(f"| {k} | {len(vs)} | {sum(1 for c in vs if c['at100'])} |")
    L += ["", f"Total at >=60%: {len(V)}; at 100%: {sum(1 for c in V if c['at100'])}", ""]
    L += [
        "### By kind and category",
        "",
        "| kind | a | b | c | d | e |",
        "|---|---|---|---|---|---|",
    ]
    for kind in sorted({c["kind"] for c in V}):
        L.append(
            f"| {kind} | "
            + " | ".join(
                str(sum(1 for c in V if c["kind"] == kind and c["category"] == k)) for k in "abcde"
            )
            + " |"
        )
    L += [
        "",
        "### Per distribution",
        "",
        "| distribution | a | b | c | d | e |",
        "|---|---|---|---|---|---|",
    ]
    for d in DISTRIBUTIONS:
        L.append(
            f"| {d} | "
            + " | ".join(
                str(sum(1 for c in V if dist_of(c["file"]) == d and c["category"] == k))
                for k in "abcde"
            )
            + " |"
        )
    L += ["", "## Ruff", "", "| code | count |", "|---|---|"]
    for code in sorted({r["code"] for r in data["ruff"]}):
        L.append(f"| {code} | {sum(1 for r in data['ruff'] if r['code'] == code)} |")
    P = data["params"]
    L += ["", f"ruff selection: `{data['ruff_select']}`", ""]
    L += [
        "## Unused parameters",
        "",
        f"- own-check rows: {len(P['rows'])}",
        f"- ruff ARG findings: {P['ruff_arg_total']}; matched by own check: {P['ruff_arg_matched_by_own_check']}; ruff-only: {P['ruff_arg_not_in_own_check']}; own-check-only: {P['own_check_not_in_ruff']}",
        "",
        "| category | rows | public |",
        "|---|---|---|",
    ]
    cats = sorted({r["category"] for r in P["rows"]})
    for c in cats:
        rs = [r for r in P["rows"] if r["category"] == c]
        L.append(f"| {c} | {len(rs)} | {sum(1 for r in rs if r['public'])} |")
    F = data["fields"]
    L += [
        "",
        "## Fields",
        "",
        f"- written, never read anywhere: {len(F['written_never_read'])}",
        f"- written, read only from tests/examples/scripts: {len(F['read_only_from_tests'])}",
        f"- self attribute read, never written: {len(F['read_never_written'])}",
        f"- classes skipped for read-never-written (unresolved/external bases or dynamic attribute hooks): {F['classes_skipped_unresolved_bases']}",
        "",
    ]
    L += ["### Written, never read", ""] + md_table(
        F["written_never_read"],
        ["file", "line", "class", "flavor", "field", "origin", "convention", "string_hits"],
    )
    L += ["", "### Read only outside src", ""] + md_table(
        F["read_only_from_tests"], ["file", "line", "class", "field", "reads"]
    )
    L += ["", "### Read, never written", ""] + md_table(
        F["read_never_written"], ["file", "line", "class", "attr"]
    )
    L += ["", "## Unused parameters (public functions, not stubs)", ""]
    pub = [r for r in P["rows"] if r["public"] and r["category"] != "stub-body"]
    L += md_table(
        sorted(pub, key=lambda r: (r["category"], r["file"], r["line"])),
        ["category", "file", "line", "function", "param", "kind", "passed_as_callback", "ruff_arg"],
    )
    L += ["", "## Vulture candidates (sorted by category, file)", ""]
    order = sorted(V, key=lambda c: (c["category"], c["file"], c["line"]))
    for c in order:
        c["refs"] = "/".join(
            str(c["ref_counts"][k]) for k in ("src", "tests", "examples", "scripts")
        )
        c["why"] = ";".join(c["reasons"]) + (
            " [bases:" + ",".join(c["class_bases"]) + "]" if c["class_bases"] else ""
        )
    L += md_table(
        order,
        [
            "category",
            "file",
            "line",
            "kind",
            "qualname",
            "confidence",
            "refs",
            "string_hits",
            "why",
        ],
    )
    L += [
        "",
        "(refs column is src/tests/examples/scripts name-reference counts outside the definition's own span)",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build/arch/deadcode")
    ap.add_argument("--ruff-select", default="ARG,F401,F841")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    idx = Index()
    cands = run_vulture(a.out)
    classify(cands, idx)
    ruff = run_ruff(a.out, a.ruff_select)
    data = {
        "ruff_select": a.ruff_select,
        "vulture": cands,
        "ruff": ruff,
        "fields": field_report(idx),
        "params": param_report(idx, ruff),
    }
    write_json(a.out / "deadcode.json", data)
    (a.out / "deadcode.md").write_text(render(data))
    print(
        f"wrote {a.out / 'deadcode.md'}: {len(cands)} vulture candidates, {len(ruff)} ruff findings"
    )


if __name__ == "__main__":
    main()
