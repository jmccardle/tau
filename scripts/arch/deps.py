"""Import-graph measurements for τ's five distributions.

Measures, from the source trees as they are on disk:

* every internal import edge, classified module-level / lazy (inside a
  function) / ``TYPE_CHECKING``-guarded, with file:line;
* the 5x5 distribution matrix per kind, and every edge outside the declared
  dependency direction;
* per-module and per-subpackage fan-in, fan-out, Martin instability, import
  cycles (strongly connected components) and propagation cost, each under three
  edge sets (all / runtime = module+lazy / module-level only);
* hub modules by fan-in, fan-out and transitive dependents;
* the ``tau_llm`` public surface and which names the other distributions and
  the sibling projects import;
* third-party imports per distribution versus the distribution's pyproject.

Usage:
    python scripts/arch/deps.py [--out DIR] [--sibling DIR ...]

Outputs (in ``--out``, default ``build/arch/deps``): ``deps.json``,
``deps_summary.md``, ``package_graph.dot/.svg`` and ``modules_<dist>.dot/.svg``.
Requires ``grimp`` and graphviz ``dot``.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import grimp
except ImportError:
    sys.exit("grimp is required: pip install grimp")

REPO = Path(__file__).resolve().parents[2]
DISTS: dict[str, str] = {
    "tau_llm": "tau-llm",
    "tau_agent_core": "tau-agent-core",
    "tau_coding_agent": "tau-coding-agent",
    "tau_jmfts": "tau-jmfts",
    "tau_meta": "tau-meta",
}
DIST_NAME = {
    "tau_llm": "ffwf-tau-llm",
    "tau_agent_core": "ffwf-tau-agent-core",
    "tau_coding_agent": "ffwf-tau-coding-agent",
    "tau_jmfts": "ffwf-tau-jmfts",
    "tau_meta": "ffwf-tau",
}
KINDS = ("module", "lazy", "type_checking")
VARIANTS = {
    "all": {"module", "lazy", "type_checking"},
    "runtime": {"module", "lazy"},
    "module": {"module"},
}
SKIP_DIRS = {
    ".git",
    ".claude",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    "build",
}


def dist_of(mod: str) -> str | None:
    """Return the distribution package a dotted module name belongs to."""
    top = mod.split(".", 1)[0]
    return top if top in DISTS else None


def src_root(pkg: str) -> Path:
    """Return the ``src`` directory of a distribution package."""
    return REPO / DISTS[pkg] / "src"


def module_name(path: Path, root: Path) -> str:
    """Return the dotted module name for ``path`` under ``root``."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class Visitor(ast.NodeVisitor):
    """Collect import statements with their kind and dynamic-import calls."""

    def __init__(self) -> None:
        """Start with empty collections and module-level context."""
        self.stmts: list[dict[str, Any]] = []
        self.dynamic: list[dict[str, Any]] = []
        self.depth_fn = 0
        self.tc = 0
        self.attrs: list[tuple[str, str, int]] = []
        self.aliases: dict[str, str] = {}

    def kind(self) -> str:
        """Return the kind of an import at the current position."""
        if self.tc:
            return "type_checking"
        return "lazy" if self.depth_fn else "module"

    def visit_Import(self, node: ast.Import) -> None:
        """Record ``import a.b``."""
        for a in node.names:
            self.stmts.append(
                {
                    "line": node.lineno,
                    "kind": self.kind(),
                    "level": 0,
                    "module": a.name,
                    "names": [],
                    "plain": True,
                }
            )
            self.aliases[a.asname or a.name.split(".")[0]] = (
                a.name if a.asname else a.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record ``from a import b``."""
        self.stmts.append(
            {
                "line": node.lineno,
                "kind": self.kind(),
                "level": node.level,
                "module": node.module or "",
                "names": [a.name for a in node.names],
                "plain": False,
            }
        )

    def _fn(self, node: ast.AST) -> None:
        """Visit a function body with the lazy flag set."""
        self.depth_fn += 1
        self.generic_visit(node)
        self.depth_fn -= 1

    visit_FunctionDef = _fn
    visit_AsyncFunctionDef = _fn
    visit_Lambda = _fn

    @staticmethod
    def _is_tc(test: ast.expr) -> bool:
        """Return True when ``test`` is ``TYPE_CHECKING`` or ``x.TYPE_CHECKING``."""
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    def visit_If(self, node: ast.If) -> None:
        """Mark the body of ``if TYPE_CHECKING`` (and else of ``if not``)."""
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        inner = test.operand if negated and isinstance(test, ast.UnaryOp) else test
        if self._is_tc(inner):
            self.visit(test)
            for branch, guarded in ((node.body, not negated), (node.orelse, negated)):
                self.tc += guarded
                for child in branch:
                    self.visit(child)
                self.tc -= guarded
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Record ``importlib.import_module(...)`` and ``__import__(...)`` calls."""
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name in {"import_module", "__import__"}:
            arg = node.args[0] if node.args else None
            lit = (
                arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
            )
            self.dynamic.append(
                {"line": node.lineno, "call": name, "literal": lit, "kind": self.kind()}
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record ``alias.name`` where alias came from ``import tau_xxx``."""
        if isinstance(node.value, ast.Name) and node.value.id in self.aliases:
            self.attrs.append((self.aliases[node.value.id], node.attr, node.lineno))
        self.generic_visit(node)


def scan(path: Path) -> Visitor:
    """Parse ``path`` and return the filled Visitor."""
    v = Visitor()
    v.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return v


def collect_files() -> dict[str, tuple[Path, Visitor]]:
    """Scan every source file of the five distributions, keyed by module name."""
    out: dict[str, tuple[Path, Visitor]] = {}
    for pkg in DISTS:
        root = src_root(pkg)
        for p in sorted((root / pkg).rglob("*.py")):
            out[module_name(p, root)] = (p, scan(p))
    return out


def build_edges(files: dict[str, tuple[Path, Visitor]]) -> list[dict[str, Any]]:
    """Build the grimp graph and return one record per import statement edge."""
    for pkg in DISTS:
        sys.path.insert(0, str(src_root(pkg)))
    graph = grimp.build_graph(*DISTS, cache_dir=None)
    for pkg in DISTS:
        loaded = importlib.import_module(pkg)
        if not Path(loaded.__file__ or "").resolve().is_relative_to(REPO):
            sys.exit(f"{pkg} resolves outside the repo: {loaded.__file__}")
    kinds_at: dict[str, dict[int, set[str]]] = {}
    for mod, (_, v) in files.items():
        d: dict[int, set[str]] = defaultdict(set)
        for s in v.stmts:
            d[s["line"]].add(s["kind"])
        kinds_at[mod] = d
    edges: list[dict[str, Any]] = []
    for importer in sorted(graph.modules):
        if importer not in files:
            sys.exit(f"grimp module {importer} has no scanned file")
        for imported in sorted(graph.find_modules_directly_imported_by(importer)):
            for det in graph.get_import_details(importer=importer, imported=imported):
                line = det["line_number"]
                ks = kinds_at[importer].get(line)
                if not ks or len(ks) != 1:
                    sys.exit(f"cannot classify import at {files[importer][0]}:{line} ({ks})")
                edges.append(
                    {
                        "importer": importer,
                        "imported": imported,
                        "line": line,
                        "kind": next(iter(ks)),
                        "file": str(files[importer][0].relative_to(REPO)),
                        "text": det["line_contents"].strip(),
                    }
                )
    edges.sort(key=lambda e: (e["importer"], e["imported"], e["line"]))
    return edges


def parse_pyprojects() -> dict[str, dict[str, Any]]:
    """Return declared base and extra requirements per distribution package."""
    out = {}
    for pkg, d in DISTS.items():
        data = tomllib.loads((REPO / d / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        out[pkg] = {
            "base": list(data.get("dependencies", [])),
            "extras": {k: list(v) for k, v in data.get("optional-dependencies", {}).items()},
        }
    return out


def req_name(req: str) -> str:
    """Return the normalised distribution name of a requirement string."""
    m = re.match(r"[A-Za-z0-9_.\-]+", req)
    assert m
    return re.sub(r"[-_.]+", "-", m.group(0)).lower()


def declared_tau_deps(pyp: dict[str, dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    """Return per distribution the tau packages it declares: base and optional."""
    by_name = {DIST_NAME[p]: p for p in DISTS}
    out: dict[str, dict[str, set[str]]] = {}
    for pkg, d in pyp.items():
        base = {by_name[req_name(r)] for r in d["base"] if req_name(r) in by_name}
        opt: set[str] = set()
        for k, reqs in d["extras"].items():
            if k == "dev":
                continue
            opt |= {
                by_name[req_name(r)]
                for r in reqs
                if req_name(r) in by_name and by_name[req_name(r)] != pkg
            }
        out[pkg] = {"base": base, "optional": opt - base}
    return out


def closure(decl: dict[str, dict[str, set[str]]], pkg: str, key: str) -> set[str]:
    """Return the transitive tau dependencies of ``pkg`` through ``base`` (+ optional if key=='all')."""
    seen: set[str] = set()
    stack = [pkg]
    while stack:
        cur = stack.pop()
        nxt = set(decl[cur]["base"]) | (set(decl[cur]["optional"]) if key == "all" else set())
        for n in nxt - seen:
            seen.add(n)
            stack.append(n)
    return seen


def classify_dist_edge(decl: dict[str, dict[str, set[str]]], a: str, b: str) -> str:
    """Classify a cross-distribution edge a->b against the declared dependencies."""
    if a == b:
        return "same"
    if b in decl[a]["base"]:
        return "declared-direct"
    if b in decl[a]["optional"]:
        return "declared-optional-extra"
    if b in closure(decl, a, "base"):
        return "declared-transitive"
    if b in closure(decl, a, "all"):
        return "declared-transitive-via-extra"
    return "UNDECLARED"


def scc(nodes: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    """Return strongly connected components (Tarjan), each sorted, in sorted order."""
    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    res: list[list[str]] = []
    counter = [0]

    def go(v: str) -> None:
        """Visit ``v`` in Tarjan's algorithm."""
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in index:
                go(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            res.append(sorted(comp))

    for n in sorted(nodes):
        if n not in index:
            go(n)
    return sorted(res, key=lambda c: (-len(c), c))


def reach(nodes: list[str], adj: dict[str, set[str]]) -> dict[str, set[str]]:
    """Return, per node, the set of nodes reachable by one or more edges."""
    comps = scc(nodes, adj)
    comp_of = {n: i for i, c in enumerate(comps) for n in c}
    cadj: dict[int, set[int]] = defaultdict(set)
    for v in nodes:
        for w in adj.get(v, ()):
            if comp_of[v] != comp_of[w]:
                cadj[comp_of[v]].add(comp_of[w])
    memo: dict[int, set[str]] = {}

    def comp_reach(c: int) -> set[str]:
        """Return nodes reachable from component ``c`` including its members."""
        if c in memo:
            return memo[c]
        s = set(comps[c])
        for d in cadj.get(c, ()):
            s |= comp_reach(d)
        memo[c] = s
        return s

    out: dict[str, set[str]] = {}
    for v in nodes:
        c = comp_of[v]
        s: set[str] = set()
        for d in cadj.get(c, ()):
            s |= comp_reach(d)
        if len(comps[c]) > 1 or v in adj.get(v, ()):
            s |= set(comps[c])
        out[v] = s
    return out


def adjacency(edges: list[dict[str, Any]], kinds: set[str], nodes: set[str]) -> dict[str, set[str]]:
    """Return importer -> imported sets restricted to ``nodes`` and ``kinds``."""
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    for e in edges:
        if (
            e["kind"] in kinds
            and e["importer"] in nodes
            and e["imported"] in nodes
            and e["importer"] != e["imported"]
        ):
            adj[e["importer"]].add(e["imported"])
    return adj


def propagation_cost(nodes: list[str], adj: dict[str, set[str]]) -> dict[str, float]:
    """Return MacCormack propagation cost with and without the identity diagonal."""
    n = len(nodes)
    if n == 0:
        return {"n": 0, "with_identity": 0.0, "without_identity": 0.0, "reachable_pairs": 0}
    r = reach(nodes, adj)
    pairs = sum(len(s - {v}) for v, s in r.items())
    return {
        "n": n,
        "reachable_pairs": pairs,
        "with_identity": (pairs + n) / (n * n),
        "without_identity": pairs / (n * (n - 1)) if n > 1 else 0.0,
    }


def metrics(nodes: list[str], adj: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    """Return fan-in, fan-out, instability and transitive counts per node."""
    radj: dict[str, set[str]] = {n: set() for n in nodes}
    for v, ws in adj.items():
        for w in ws:
            radj[w].add(v)
    fwd = reach(nodes, adj)
    rev = reach(nodes, radj)
    out = {}
    for n in nodes:
        ca, ce = len(radj[n]), len(adj[n])
        out[n] = {
            "fan_in": ca,
            "fan_out": ce,
            "instability": (ce / (ca + ce)) if ca + ce else None,
            "transitive_dependents": len(rev[n] - {n}),
            "transitive_dependencies": len(fwd[n] - {n}),
        }
    return out


def unit_of(mod: str) -> str:
    """Return the depth-1 unit (subpackage or module) a module belongs to."""
    parts = mod.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else parts[0]


def analyse_graph(
    edges: list[dict[str, Any]], files: dict[str, tuple[Path, Visitor]]
) -> dict[str, Any]:
    """Compute matrices, cycles, metrics and hubs from the edge list."""
    pyp = parse_pyprojects()
    decl = declared_tau_deps(pyp)
    res: dict[str, Any] = {
        "declared_tau_deps": {k: {a: sorted(b) for a, b in v.items()} for k, v in decl.items()}
    }
    mat: dict[str, dict[str, dict[str, int]]] = {
        a: {b: dict.fromkeys(KINDS, 0) for b in DISTS} for a in DISTS
    }
    pairs: dict[str, dict[str, dict[str, set[tuple[str, str]]]]] = {
        a: {b: {k: set() for k in KINDS} for b in DISTS} for a in DISTS
    }
    cross = []
    for e in edges:
        a, b = dist_of(e["importer"]), dist_of(e["imported"])
        assert a and b
        mat[a][b][e["kind"]] += 1
        pairs[a][b][e["kind"]].add((e["importer"], e["imported"]))
        if a != b:
            cls = classify_dist_edge(decl, a, b)
            cross.append({**e, "from_dist": a, "to_dist": b, "declaration": cls})
    res["matrix_statements"] = mat
    res["matrix_module_pairs"] = {
        a: {b: {k: len(pairs[a][b][k]) for k in KINDS} for b in DISTS} for a in DISTS
    }
    res["cross_dist_edges"] = cross
    res["undeclared_direction_edges"] = [c for c in cross if c["declaration"] == "UNDECLARED"]
    res["optional_extra_edges"] = [
        c for c in cross if c["declaration"] == "declared-optional-extra"
    ]
    allmods = sorted(files)
    res["dists"] = {}
    for pkg in DISTS:
        mods = [m for m in allmods if dist_of(m) == pkg]
        units = sorted({unit_of(m) for m in mods})
        d: dict[str, Any] = {"n_modules": len(mods), "n_units": len(units), "variants": {}}
        for vname, ks in VARIANTS.items():
            adj = adjacency(edges, ks, set(mods))
            full = adjacency(edges, ks, set(allmods))
            m = metrics(mods, adj)
            for n in mods:
                m[n]["cross_dist_fan_out"] = len({w for w in full[n] if dist_of(w) != pkg})
                m[n]["cross_dist_fan_in"] = len(
                    {v for v in allmods if dist_of(v) != pkg and n in full[v]}
                )
            comps = [c for c in scc(mods, adj) if len(c) > 1]
            uadj: dict[str, set[str]] = {u: set() for u in units}
            for v, ws in adj.items():
                for w in ws:
                    if unit_of(v) != unit_of(w):
                        uadj[unit_of(v)].add(unit_of(w))
            ucomps = [c for c in scc(units, uadj) if len(c) > 1]
            d["variants"][vname] = {
                "edges": sum(len(s) for s in adj.values()),
                "modules": m,
                "sccs": comps,
                "propagation_cost": propagation_cost(mods, adj),
                "units": metrics(units, uadj),
                "unit_edges": sum(len(s) for s in uadj.values()),
                "unit_sccs": ucomps,
                "unit_propagation_cost": propagation_cost(units, uadj),
            }
        res["dists"][pkg] = d
    res["whole_graph"] = {}
    for vname, ks in VARIANTS.items():
        adj = adjacency(edges, ks, set(allmods))
        m = metrics(allmods, adj)
        comps = [c for c in scc(allmods, adj) if len(c) > 1]
        res["whole_graph"][vname] = {
            "modules": m,
            "sccs": comps,
            "cross_dist_sccs": [c for c in comps if len({dist_of(x) for x in c}) > 1],
            "propagation_cost": propagation_cost(allmods, adj),
        }
    return res


def tau_name_use(files: dict[str, tuple[Path, Visitor]], target: str) -> dict[str, Any]:
    """Return the names of ``target`` imported by the other distributions."""
    use: dict[str, dict[str, Any]] = {}

    def add(name: str, mod: str, line: int, how: str) -> None:
        """Record one use of ``name`` from importer ``mod``."""
        u = use.setdefault(name, {"modules": {}, "by_dist": defaultdict(set), "how": set()})
        u["modules"].setdefault(mod, []).append(line)
        u["by_dist"][dist_of(mod)].add(mod)
        u["how"].add(how)

    for mod, (_, v) in files.items():
        if dist_of(mod) == target or dist_of(mod) == "tau_meta":
            continue
        for s in v.stmts:
            m = s["module"]
            if s["level"] or not (m == target or m.startswith(target + ".")):
                continue
            if s["plain"]:
                add(m + " (module)", mod, s["line"], "import")
            else:
                for n in s["names"]:
                    add(f"{m}.{n}", mod, s["line"], "from-import")
        for base, attr, line in v.attrs:
            if base == target or base.startswith(target + "."):
                add(f"{base}.{attr}", mod, line, "attribute")
    return {
        k: {
            "importing_modules": len(u["modules"]),
            "by_dist": {d: len(ms) for d, ms in sorted(u["by_dist"].items())},
            "how": sorted(u["how"]),
            "sites": {m: sorted(set(ls)) for m, ls in sorted(u["modules"].items())},
        }
        for k, u in sorted(use.items(), key=lambda kv: (-len(kv[1]["modules"]), kv[0]))
    }


def tau_llm_surface(use: dict[str, Any]) -> dict[str, Any]:
    """Return the tau_llm root exports and how the usage splits against them."""
    root = importlib.import_module("tau_llm")
    exported = sorted(getattr(root, "__all__"))
    bound = sorted(n for n in vars(root) if not n.startswith("__"))
    root_used = {k.split(".", 1)[1] for k in use if k.count(".") == 1 and " " not in k}
    sub_used = {k for k in use if k.count(".") >= 2 or " " in k}
    return {
        "all_exports": exported,
        "n_all_exports": len(exported),
        "bound_names_not_in_all": sorted(set(bound) - set(exported)),
        "root_names_imported_by_other_dists": sorted(root_used),
        "root_exports_never_imported_by_other_dists": sorted(
            set(exported) - root_used - {"__version__"}
        ),
        "root_names_imported_but_not_in_all": sorted(root_used - set(exported)),
        "submodule_paths_imported": len(sub_used),
        "root_paths_imported": len(root_used),
    }


def scan_sibling(root: Path) -> dict[str, Any]:
    """Scan a sibling project for imports of tau packages."""
    hits: dict[str, dict[str, Any]] = {}
    parse_failures = []
    n_files = 0
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        n_files += 1
        try:
            v = scan(p)
        except SyntaxError as exc:
            parse_failures.append(f"{rel}: {exc}")
            continue
        for s in v.stmts:
            m = s["module"]
            if s["level"] or dist_of(m) is None or dist_of(m) == "tau_meta":
                continue
            names = [m + " (module)"] if s["plain"] else [f"{m}.{n}" for n in s["names"]]
            for nm in names:
                h = hits.setdefault(nm, {"files": {}, "kinds": set()})
                h["files"].setdefault(str(rel), []).append(s["line"])
                h["kinds"].add(s["kind"])
        for base, attr, line in v.attrs:
            if dist_of(base):
                h = hits.setdefault(f"{base}.{attr}", {"files": {}, "kinds": set()})
                h["files"].setdefault(str(rel), []).append(line)
                h["kinds"].add("attribute")
    return {
        "root_name": root.name,
        "py_files_scanned": n_files,
        "parse_failures": parse_failures,
        "names": {
            k: {"n_files": len(h["files"]), "kinds": sorted(h["kinds"]), "sites": h["files"]}
            for k, h in sorted(hits.items(), key=lambda kv: (-len(kv[1]["files"]), kv[0]))
        },
    }


def third_party(files: dict[str, tuple[Path, Visitor]]) -> dict[str, Any]:
    """Compare third-party imports per distribution with its pyproject."""
    pyp = parse_pyprojects()
    dmap = importlib.metadata.packages_distributions()
    stdlib = set(sys.stdlib_module_names)
    out: dict[str, Any] = {}
    for pkg in DISTS:
        imps: dict[str, dict[str, Any]] = {}
        for mod, (_, v) in files.items():
            if dist_of(mod) != pkg:
                continue
            for s in v.stmts:
                m = s["module"]
                if (
                    s["level"]
                    or not m
                    or m.split(".")[0] in stdlib
                    or dist_of(m)
                    or m.split(".")[0] == "__future__"
                ):
                    continue
                top = m.split(".")[0]
                rec = imps.setdefault(top, {"dotted": set(), "kinds": defaultdict(set)})
                rec["dotted"].add(m)
                rec["kinds"][s["kind"]].add(mod)
        declared: dict[str, list[str]] = defaultdict(list)
        for r in pyp[pkg]["base"]:
            declared[req_name(r)].append("(base)")
        for ex, reqs in pyp[pkg]["extras"].items():
            for r in reqs:
                n = req_name(r)
                if n not in {re.sub(r"[-_.]+", "-", x).lower() for x in DIST_NAME.values()}:
                    declared[n].append(ex)
        rows = []
        matched: set[str] = set()
        for top, rec in sorted(imps.items()):
            cands = {re.sub(r"[-_.]+", "-", d).lower() for d in dmap.get(top, [])}
            how = "installed-metadata"
            if not cands:
                how = "name-match"
                for dotted in rec["dotted"]:
                    parts = dotted.split(".")
                    for i in range(len(parts), 0, -1):
                        n = re.sub(r"[-_.]+", "-", "_".join(parts[:i])).lower()
                        if n in declared:
                            cands.add(n)
            in_decl = sorted(cands & set(declared))
            matched |= set(in_decl)
            where = sorted({w for n in in_decl for w in declared[n]})
            if not in_decl and how == "installed-metadata":
                for dotted in rec["dotted"]:
                    parts = dotted.split(".")
                    for i in range(len(parts), 0, -1):
                        n = re.sub(r"[-_.]+", "-", "_".join(parts[:i])).lower()
                        if n in declared:
                            cands.add(n)
                            how = "installed-metadata+name-match"
                in_decl = sorted(cands & set(declared))
                matched |= set(in_decl)
                where = sorted({w for n in in_decl for w in declared[n]})
            status = (
                "UNRESOLVED(not installed, no name match)"
                if not cands
                else "UNDECLARED"
                if not in_decl
                else "base"
                if "(base)" in where
                else "extra-only"
            )
            rows.append(
                {
                    "import": top,
                    "dotted": sorted(rec["dotted"]),
                    "distribution": sorted(cands),
                    "resolved_by": how,
                    "status": status,
                    "declared_in": where,
                    "modules_by_kind": {k: sorted(ms) for k, ms in sorted(rec["kinds"].items())},
                }
            )
        unimported = sorted((n, w) for n, w in declared.items() if n not in matched)
        out[pkg] = {
            "imports": rows,
            "declared_not_imported": [{"distribution": n, "declared_in": w} for n, w in unimported],
        }
    return out


def dynamic_imports(files: dict[str, tuple[Path, Visitor]]) -> list[dict[str, Any]]:
    """Return every importlib.import_module / __import__ call site in the source."""
    rows = []
    for mod, (p, v) in sorted(files.items()):
        for d in v.dynamic:
            rows.append({"module": mod, "file": str(p.relative_to(REPO)), **d})
    return rows


def dot_id(s: str) -> str:
    """Quote a DOT identifier."""
    return '"' + s.replace('"', "") + '"'


EDGE_CLASS = {"module": "", "lazy": ' class="lazy"', "type_checking": ' class="soft"'}
HEAD = [
    '  graph [fontname="Courier"];',
    '  node [shape=box, fontname="Courier", fontsize=10];',
    '  edge [fontname="Courier", fontsize=9];',
]
STRENGTH = {"module": 0, "lazy": 1, "type_checking": 2}


def write_package_dot(res: dict[str, Any], path: Path) -> None:
    """Write the distribution-level graph, one edge per (pair, kind) with a statement count."""
    lines = ["digraph packages {", "  rankdir=BT;", *HEAD]
    lines.extend(f"  {dot_id(p)};" for p in DISTS)
    agg: dict[tuple[str, str, str], int] = defaultdict(int)
    bad: set[tuple[str, str, str]] = set()
    for c in res["cross_dist_edges"]:
        k = (c["from_dist"], c["to_dist"], c["kind"])
        agg[k] += 1
        if c["declaration"] == "UNDECLARED":
            bad.add(k)
    for k, n in sorted(agg.items()):
        a, b, kind = k
        cls = ' class="mark"' if k in bad else EDGE_CLASS[kind]
        lines.append(f'  {dot_id(a)} -> {dot_id(b)} [label="{kind} {n}"{cls}];')
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def module_graph_edges(pkg: str, edges: list[dict[str, Any]]) -> dict[tuple[str, str], set[str]]:
    """Return intra-distribution (importer, imported) -> set of kinds."""
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in edges:
        if (
            dist_of(e["importer"]) == pkg == dist_of(e["imported"])
            and e["importer"] != e["imported"]
        ):
            out[(e["importer"], e["imported"])].add(e["kind"])
    return out


def strongest(kinds: set[str]) -> str:
    """Return the most load-bearing kind (module over lazy over type_checking)."""
    return min(kinds, key=lambda k: STRENGTH[k])


def write_module_dot(
    pkg: str, edges: list[dict[str, Any]], res: dict[str, Any], path: Path
) -> None:
    """Write one distribution's module graph: subpackage clusters, SCCs marked or clustered."""
    mods = sorted(res["dists"][pkg]["variants"]["all"]["modules"])
    comps = res["dists"][pkg]["variants"]["all"]["sccs"]
    in_scc = {m for c in comps for m in c}
    lines = [f"digraph {dot_id(pkg)} {{", "  rankdir=LR;", *HEAD]
    single = len(comps) == 1
    ncl = 0
    if not single:
        for i, c in enumerate(comps):
            lines.append(
                f"  subgraph cluster_{ncl} {{ label={dot_id(f'cycle {i} ({len(c)} modules)')};"
            )
            lines.extend(f"    {dot_id(m)};" for m in c)
            lines.append("  }")
            ncl += 1
    groups: dict[str, list[str]] = defaultdict(list)
    for m in mods:
        if m in in_scc and not single:
            continue
        groups[unit_of(m)].append(m)
    for unit, members in sorted(groups.items()):
        if len(members) > 1:
            lines.append(f"  subgraph cluster_{ncl} {{ label={dot_id(unit)};")
            ncl += 1
            for m in members:
                cls = ' [class="mark"]' if single and m in in_scc else ""
                lines.append(f"    {dot_id(m)}{cls};")
            lines.append("  }")
        else:
            m = members[0]
            cls = ' [class="mark"]' if single and m in in_scc else ""
            lines.append(f"  {dot_id(m)}{cls};")
    for (a, b), ks in sorted(module_graph_edges(pkg, edges).items()):
        lines.append(f"  {dot_id(a)} -> {dot_id(b)}[{EDGE_CLASS[strongest(ks)].strip()}];")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_condensed_dot(
    pkg: str, edges: list[dict[str, Any]], res: dict[str, Any], path: Path
) -> int:
    """Write the condensed graph (SCCs and subpackages collapsed) and return its node count."""
    mods = sorted(res["dists"][pkg]["variants"]["all"]["modules"])
    comps = res["dists"][pkg]["variants"]["all"]["sccs"]
    node_of: dict[str, str] = {}
    for i, c in enumerate(comps):
        for m in c:
            node_of[m] = f"cycle {i} ({len(c)} modules)"
    count: dict[str, int] = defaultdict(int)
    for m in mods:
        count[unit_of(m)] += 1
    for m in mods:
        if m not in node_of:
            u = unit_of(m)
            node_of[m] = f"{u} ({count[u]} modules)" if count[u] > 1 else m
    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: dict.fromkeys(KINDS, 0))
    for e in edges:
        if dist_of(e["importer"]) == pkg == dist_of(e["imported"]):
            a, b = node_of[e["importer"]], node_of[e["imported"]]
            if a != b:
                agg[(a, b)][e["kind"]] += 1
    lines = [f"digraph {dot_id(pkg + ' condensed')} {{", "  rankdir=LR;", *HEAD]
    nodes = sorted(set(node_of.values()))
    for n in nodes:
        mark = ' [class="mark"]' if n.startswith("cycle ") and len(comps) == 1 else ""
        lines.append(f"  {dot_id(n)}{mark};")
    for (a, b), cnt in sorted(agg.items()):
        kind = strongest({k for k, v in cnt.items() if v})
        label = "/".join(str(cnt[k]) for k in KINDS)
        lines.append(f'  {dot_id(a)} -> {dot_id(b)} [label="{label}"{EDGE_CLASS[kind]}];')
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(nodes)


def render(dot_path: Path, title: str, desc: str) -> None:
    """Render ``dot_path`` to plain SVG and to a themed ``.themed.svg`` via svg_theme.py."""
    svg = subprocess.run(
        ["dot", "-Tsvg", str(dot_path)], check=True, capture_output=True, text=True
    ).stdout
    dot_path.with_suffix(".svg").write_text(svg, encoding="utf-8")
    theme = Path(__file__).resolve().parent / "svg_theme.py"
    out = dot_path.with_suffix(".themed.svg")
    subprocess.run(
        [sys.executable, str(theme), "-", str(out), "--title", title, "--desc", desc],
        input=svg,
        check=True,
        text=True,
    )


def table(headers: list[str], rows: list[list[Any]]) -> str:
    """Return a Markdown table."""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def fmt(x: float | None) -> str:
    """Format an optional float to 3 places."""
    return "-" if x is None else f"{x:.3f}"


def summary_md(res: dict[str, Any], edges: list[dict[str, Any]]) -> str:
    """Render the human-readable summary."""
    L: list[str] = ["# deps.py summary", ""]
    L.append(f"Internal import statements (edges incl. duplicates by line): {len(edges)}")
    for k in KINDS:
        L.append(f"- {k}: {sum(1 for e in edges if e['kind'] == k)}")
    L += ["", "## Distribution matrix (import statements; rows import columns)", ""]
    for k in KINDS:
        L += [f"### {k}", ""]
        L.append(
            table(
                ["from \\ to", *DISTS],
                [[a, *[res["matrix_statements"][a][b][k] for b in DISTS]] for a in DISTS],
            )
        )
        L.append("")
    L += ["### distinct module pairs (all kinds, by kind m/l/t)", ""]
    mp = res["matrix_module_pairs"]
    L.append(
        table(
            ["from \\ to", *DISTS],
            [[a, *["/".join(str(mp[a][b][k]) for k in KINDS) for b in DISTS]] for a in DISTS],
        )
    )
    L += ["", "## Declared dependencies (pyproject)", ""]
    for p, d in res["declared_tau_deps"].items():
        L.append(f"- {p}: base={d['base']} optional-extra={d['optional']}")
    L += ["", "## Cross-distribution edges not declared (upward / undeclared)", ""]
    und = res["undeclared_direction_edges"]
    L.append(
        "none"
        if not und
        else table(
            ["file:line", "importer", "imported", "kind", "text"],
            [
                [
                    f"{e['file']}:{e['line']}",
                    e["importer"],
                    e["imported"],
                    e["kind"],
                    f"`{e['text']}`",
                ]
                for e in und
            ],
        )
    )
    L += ["", "## Cross-distribution edges by declaration class", ""]
    cnt: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for c in res["cross_dist_edges"]:
        cnt[(c["from_dist"], c["to_dist"], c["declaration"], c["kind"])] += 1
    L.append(
        table(
            ["from", "to", "declaration", "kind", "statements"],
            [[*k, v] for k, v in sorted(cnt.items())],
        )
    )
    L += ["", "### Edges declared only as optional extra", ""]
    L.append(
        table(
            ["file:line", "importer", "imported", "kind"],
            [
                [f"{e['file']}:{e['line']}", e["importer"], e["imported"], e["kind"]]
                for e in res["optional_extra_edges"]
            ],
        )
    )
    L += ["", "## Per-distribution graph measures", ""]
    rows = []
    for pkg, d in res["dists"].items():
        for v in VARIANTS:
            x = d["variants"][v]
            pc = x["propagation_cost"]
            big = x["sccs"][0] if x["sccs"] else []
            rows.append(
                [
                    pkg,
                    v,
                    d["n_modules"],
                    x["edges"],
                    len(x["sccs"]),
                    len(big),
                    sum(len(c) for c in x["sccs"]),
                    fmt(pc["with_identity"]),
                    fmt(pc["without_identity"]),
                ]
            )
    L.append(
        table(
            [
                "dist",
                "edge set",
                "modules",
                "module edges",
                "SCCs>1",
                "largest SCC",
                "modules in SCCs",
                "PC (with identity)",
                "PC (no identity)",
            ],
            rows,
        )
    )
    L += ["", "### Subpackage-level (depth-1 units) graph measures", ""]
    rows = []
    for pkg, d in res["dists"].items():
        for v in VARIANTS:
            x = d["variants"][v]
            pc = x["unit_propagation_cost"]
            rows.append(
                [
                    pkg,
                    v,
                    d["n_units"],
                    x["unit_edges"],
                    len(x["unit_sccs"]),
                    max((len(c) for c in x["unit_sccs"]), default=0),
                    fmt(pc["with_identity"]),
                    fmt(pc["without_identity"]),
                ]
            )
    L.append(
        table(
            [
                "dist",
                "edge set",
                "units",
                "unit edges",
                "SCCs>1",
                "largest SCC",
                "PC (with identity)",
                "PC (no identity)",
            ],
            rows,
        )
    )
    L += ["", "### Import cycles (modules), per distribution and edge set", ""]
    for pkg, d in res["dists"].items():
        for v in VARIANTS:
            for i, c in enumerate(d["variants"][v]["sccs"]):
                L.append(
                    f"- {pkg} [{v}] cycle {i} size {len(c)}: " + ", ".join(f"`{m}`" for m in c)
                )
    L += ["", "### Import cycles (units)", ""]
    for pkg, d in res["dists"].items():
        for v in VARIANTS:
            for i, c in enumerate(d["variants"][v]["unit_sccs"]):
                L.append(
                    f"- {pkg} [{v}] unit cycle {i} size {len(c)}: " + ", ".join(f"`{m}`" for m in c)
                )
    L += ["", "### Unit table (all edges; intra-distribution Ca/Ce, I = Ce/(Ca+Ce))", ""]
    for pkg, d in res["dists"].items():
        L += [f"#### {pkg}", ""]
        u = d["variants"]["all"]["units"]
        um = d["variants"]["module"]["units"]
        L.append(
            table(
                [
                    "unit",
                    "fan-in",
                    "fan-out",
                    "I",
                    "trans. dependents",
                    "fan-in (module-level only)",
                    "fan-out (module-level only)",
                ],
                [
                    [
                        k,
                        v["fan_in"],
                        v["fan_out"],
                        fmt(v["instability"]),
                        v["transitive_dependents"],
                        um[k]["fan_in"],
                        um[k]["fan_out"],
                    ]
                    for k, v in sorted(u.items())
                ],
            )
        )
        L.append("")
    L += ["## Cross-distribution cycles (whole graph)", ""]
    for v in VARIANTS:
        cc = res["whole_graph"][v]["cross_dist_sccs"]
        L.append(f"- [{v}] SCCs spanning more than one distribution: {len(cc)}")
        for c in cc:
            L.append(f"  - size {len(c)}: " + ", ".join(f"`{m}`" for m in c))
    wp = res["whole_graph"]
    L.append("")
    L.append(
        table(
            ["edge set", "PC (with identity)", "PC (no identity)"],
            [
                [
                    v,
                    fmt(wp[v]["propagation_cost"]["with_identity"]),
                    fmt(wp[v]["propagation_cost"]["without_identity"]),
                ]
                for v in VARIANTS
            ],
        )
    )
    L += ["", "## Hub modules (whole graph; fan counts are distinct neighbouring modules)", ""]
    for v in ("all", "module"):
        m = wp[v]["modules"]
        for key, title in (
            ("fan_in", "fan-in"),
            ("fan_out", "fan-out"),
            ("transitive_dependents", "transitive dependents"),
        ):
            top = sorted(m.items(), key=lambda kv: (-kv[1][key], kv[0]))[:15]
            L += [f"### Top 15 by {title} [{v} edges]", ""]
            L.append(
                table(
                    ["module", title, "fan-in", "fan-out", "trans. dependents", "I"],
                    [
                        [
                            k,
                            x[key],
                            x["fan_in"],
                            x["fan_out"],
                            x["transitive_dependents"],
                            fmt(x["instability"]),
                        ]
                        for k, x in top
                    ],
                )
            )
            L.append("")
    L += ["## tau_llm public surface", ""]
    s = res["tau_llm_surface"]
    L.append(f"- `tau_llm.__all__` has {s['n_all_exports']} names.")
    L.append(
        f"- Root-path names imported by other distributions: {s['root_paths_imported']}; submodule-path names/modules: {s['submodule_paths_imported']}."
    )
    L.append(f"- Names bound in `tau_llm` but absent from `__all__`: {s['bound_names_not_in_all']}")
    L.append(
        f"- Root names imported by other distributions but absent from `__all__`: {s['root_names_imported_but_not_in_all']}"
    )
    L.append(
        f"- `__all__` names never imported by another distribution's source: {s['root_exports_never_imported_by_other_dists']}"
    )
    L += ["", "### tau_llm names imported by other distributions (importing-module count)", ""]
    L.append(
        table(
            ["name", "modules", "by distribution", "how"],
            [
                [k, v["importing_modules"], v["by_dist"], ",".join(v["how"])]
                for k, v in res["tau_llm_use"].items()
            ],
        )
    )
    for tgt in ("tau_agent_core", "tau_coding_agent"):
        L += ["", f"### {tgt} names imported by other distributions", ""]
        use = res[f"{tgt}_use"]
        L.append(
            table(
                ["name", "modules", "by distribution", "how"],
                [
                    [k, v["importing_modules"], v["by_dist"], ",".join(v["how"])]
                    for k, v in use.items()
                ],
            )
            if use
            else "none"
        )
    for sib in res["siblings"]:
        L += [
            "",
            f"## Sibling project `{sib['root_name']}` ({sib['py_files_scanned']} .py files scanned)",
            "",
        ]
        if sib["parse_failures"]:
            L.append(f"Parse failures: {sib['parse_failures']}")
        L.append(
            table(
                ["name", "files", "kinds", "sites"],
                [
                    [
                        k,
                        v["n_files"],
                        ",".join(v["kinds"]),
                        "; ".join(
                            f"{f}:{','.join(map(str, ls))}"
                            for f, ls in list(v["sites"].items())[:6]
                        ),
                    ]
                    for k, v in sib["names"].items()
                ],
            )
            if sib["names"]
            else "no tau imports found"
        )
    L += ["", "## Third-party imports vs pyproject", ""]
    for pkg, d in res["third_party"].items():
        L += [f"### {pkg}", ""]
        L.append(
            table(
                [
                    "import",
                    "dotted",
                    "distribution",
                    "status",
                    "declared in",
                    "modules (module/lazy/tc)",
                ],
                [
                    [
                        r["import"],
                        ", ".join(r["dotted"]),
                        ",".join(r["distribution"]),
                        r["status"],
                        ",".join(r["declared_in"]),
                        "/".join(str(len(r["modules_by_kind"].get(k, []))) for k in KINDS),
                    ]
                    for r in d["imports"]
                ],
            )
            if d["imports"]
            else "no third-party imports"
        )
        L.append("")
        L.append(
            "Declared but not imported: "
            + (
                ", ".join(
                    f"{x['distribution']} [{','.join(x['declared_in'])}]"
                    for x in d["declared_not_imported"]
                )
                or "none"
            )
        )
        L.append("")
    L += ["## Dynamic imports invisible to the graph (importlib.import_module / __import__)", ""]
    dy = res["dynamic_imports"]
    L.append(
        table(
            ["file:line", "call", "literal", "kind"],
            [[f"{r['file']}:{r['line']}", r["call"], r["literal"], r["kind"]] for r in dy],
        )
        if dy
        else "none"
    )
    return "\n".join(L) + "\n"


def main() -> None:
    """Run all measurements and write outputs."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "build/arch/deps", help="output directory")
    ap.add_argument(
        "--sibling",
        type=Path,
        action="append",
        default=[],
        help="sibling project to scan (repeatable)",
    )
    args = ap.parse_args()
    if shutil.which("dot") is None:
        sys.exit("graphviz 'dot' not found: apt install graphviz")
    args.out.mkdir(parents=True, exist_ok=True)
    files = collect_files()
    edges = build_edges(files)
    res = analyse_graph(edges, files)
    res["edges"] = edges
    res["tau_llm_use"] = tau_name_use(files, "tau_llm")
    res["tau_agent_core_use"] = tau_name_use(files, "tau_agent_core")
    res["tau_coding_agent_use"] = tau_name_use(files, "tau_coding_agent")
    res["tau_llm_surface"] = tau_llm_surface(res["tau_llm_use"])
    res["siblings"] = [scan_sibling(p) for p in args.sibling]
    res["third_party"] = third_party(files)
    res["dynamic_imports"] = dynamic_imports(files)
    (args.out / "deps.json").write_text(
        json.dumps(res, indent=1, sort_keys=True, default=sorted) + "\n", encoding="utf-8"
    )
    (args.out / "deps_summary.md").write_text(summary_md(res, edges), encoding="utf-8")
    write_package_dot(res, args.out / "package_graph.dot")
    render(
        args.out / "package_graph.dot",
        "Distribution import graph",
        "Import statements between the five distributions, one edge per kind with a statement count.",
    )
    for pkg in DISTS:
        dp = args.out / f"modules_{pkg}.dot"
        write_module_dot(pkg, edges, res, dp)
        render(
            dp, f"{pkg} module graph", f"Module import graph of {pkg}; cycles marked or clustered."
        )
        if len(res["dists"][pkg]["variants"]["all"]["modules"]) > 80:
            cp = args.out / f"modules_{pkg}_condensed.dot"
            n = write_condensed_dot(pkg, edges, res, cp)
            render(
                cp,
                f"{pkg} condensed graph",
                f"{pkg} with cycles and subpackages collapsed to {n} nodes; edge labels are module/lazy/type_checking statement counts.",
            )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
