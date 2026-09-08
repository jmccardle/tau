"""Intra-module symbol reference graph for a single Python file.

Answers the question a module dependency graph cannot answer while everything
lives in one file: if these top-level definitions were split into N files, how
many edges would cross a file boundary, and which pairs form a cycle?

Usage:  python scripts/symbol_graph.py <file.py> [plan.json]

plan.json maps a target module name -> list of top-level symbol names.
Symbols not named in the plan land in a residual group.

Kept rather than thrown away because module-level tools cannot answer this:
grimp, tach and import-linter all see modules, and the question is about the
symbols inside one. It scored four candidate plans for the `app.py` split,
three of them distinctly, and its verdict on each held after execution.
"""

import ast
import json
import sys
from collections import defaultdict


def top_level_symbols(tree):
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out[n.target.id] = n
    return out


def refs_of(node, universe, own_name):
    """Top-level names referenced anywhere inside `node`, excluding itself."""
    found = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name) and x.id in universe and x.id != own_name:
            found.add(x.id)
        elif isinstance(x, ast.Attribute):
            base = x
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in universe and base.id != own_name:
                found.add(base.id)
        elif isinstance(x, ast.Constant) and isinstance(x.value, str):
            # forward references in annotations, e.g. -> "Parley"
            if x.value in universe and x.value != own_name:
                found.add(x.value)
    return found


def size_of(node):
    return getattr(node, "end_lineno", node.lineno) - node.lineno + 1


def tarjan(graph):
    """Strongly connected components, iterative."""
    index = {}
    low = {}
    on = set()
    stack = []
    out = []
    counter = [0]
    for root in graph:
        if root in index:
            continue
        work = [(root, iter(graph[root]))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(graph[w])))
                    advanced = True
                    break
                elif w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return [c for c in out if len(c) > 1]


def main():
    path = sys.argv[1]
    tree = ast.parse(open(path).read())
    syms = top_level_symbols(tree)
    graph = {name: refs_of(node, syms, name) for name, node in syms.items()}

    plan = {}
    if len(sys.argv) > 2:
        plan = json.load(open(sys.argv[2]))

    where = {}
    for module, names in plan.items():
        for n in names:
            where[n] = module
    residual = [n for n in syms if n not in where]
    for n in residual:
        where[n] = "(unassigned)"

    modules = defaultdict(list)
    for n in syms:
        modules[where[n]].append(n)

    print(f"file: {path}")
    print(f"top-level symbols: {len(syms)}   edges: {sum(len(v) for v in graph.values())}\n")

    print("=== proposed modules ===")
    for m in sorted(modules, key=lambda k: -sum(size_of(syms[n]) for n in modules[k])):
        lines = sum(size_of(syms[n]) for n in modules[m])
        print(f"  {m:22s} {len(modules[m]):3d} symbols  {lines:5d} lines")

    print("\n=== cross-module edges (would become imports) ===")
    cross = defaultdict(list)
    intra = 0
    for src, dsts in graph.items():
        for d in dsts:
            if where[src] == where[d]:
                intra += 1
            else:
                cross[(where[src], where[d])].append(f"{src}->{d}")
    total_cross = sum(len(v) for v in cross.values())
    print(f"  intra-module: {intra}   cross-module: {total_cross}")
    for (a, b), edges in sorted(cross.items(), key=lambda kv: -len(kv[1])):
        print(f"  {a:22s} -> {b:22s} {len(edges):3d}  {', '.join(sorted(edges)[:6])}"
              + (" …" if len(edges) > 6 else ""))

    print("\n=== module-level cycles (a split here needs a shared module or a late import) ===")
    mgraph = defaultdict(set)
    for (a, b) in cross:
        mgraph[a].add(b)
    for m in modules:
        mgraph.setdefault(m, set())
    cycles = tarjan(mgraph)
    if not cycles:
        print("  none — the plan is a DAG")
    for c in cycles:
        print(f"  cycle: {' <-> '.join(sorted(c))}")
        for (a, b), edges in cross.items():
            if a in c and b in c:
                print(f"      {a} -> {b}: {', '.join(sorted(edges))}")

    print("\n=== most-referenced symbols (splitting these costs the most imports) ===")
    indeg = defaultdict(int)
    for src, dsts in graph.items():
        for d in dsts:
            indeg[d] += 1
    for n, c in sorted(indeg.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {c:3d}  {n:28s} ({where[n]})")


main()
