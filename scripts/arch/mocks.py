"""Test-isolation measurements over every ``tau-*/tests`` tree.

Measures, by AST (no test is executed):

1. patch sites (``monkeypatch.setattr/setitem/delattr/delitem``, ``mock.patch``,
   ``patch.object``, ``patch.dict``, ``patch.multiple``) resolved to a source
   module and attribute where the target can be resolved;
2. test-defined fakes and stubs (subclasses of source classes, classes named
   Fake*/Stub*/Dummy*/Scripted*/..., and fake factory functions) and how many
   test files define their own fake of the same thing;
3. imports of one package's modules by another package's tests, with names that
   start with an underscore counted separately;
4. reads and writes of ``obj._private`` attributes, per source class where the
   receiver's class can be inferred from an assignment or annotation, otherwise
   per attribute name.

Usage:
    python scripts/arch/mocks.py [--out DIR]

Outputs (in ``--out``, default ``build/arch/mocks``): ``mocks.json`` and
``mocks_summary.md``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DISTS = {
    "tau_llm": "tau-llm",
    "tau_agent_core": "tau-agent-core",
    "tau_coding_agent": "tau-coding-agent",
    "tau_jmfts": "tau-jmfts",
    "tau_meta": "tau-meta",
}
MONKEY_METHODS = {"setattr", "setitem", "delattr", "delitem"}
PATCH_FQ = {
    "unittest.mock.patch": "patch",
    "unittest.mock.patch.object": "patch.object",
    "unittest.mock.patch.dict": "patch.dict",
    "unittest.mock.patch.multiple": "patch.multiple",
}
MOCK_FQ = {
    "unittest.mock.Mock",
    "unittest.mock.MagicMock",
    "unittest.mock.AsyncMock",
    "unittest.mock.NonCallableMock",
    "unittest.mock.create_autospec",
}
FAKE_PREFIX = r"(Fake|Stub|Dummy|Scripted|Mock|Canned|Spy|Recording|Null|Noop)"
FAKE_CLASS = re.compile(rf"^_*{FAKE_PREFIX}(?=[A-Z0-9_])|(Fake|Stub|Dummy|Double)$")
FAKE_FUNC = re.compile(r"^_*(fake|stub|scripted|dummy|mock|canned)_|_(fake|stub)$")
PRIVATE = re.compile(r"^_(?!_)\w*$|^__(?!\w*__$)\w+$")


def dist_of(mod: str) -> str | None:
    """Return the tau distribution package a dotted module belongs to."""
    top = mod.split(".", 1)[0]
    return top if top in DISTS else None


def modname(path: Path, root: Path) -> str:
    """Return the dotted module name for ``path`` under ``root``."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class SourceIndex:
    """Index of source modules, their top-level names and their classes."""

    def __init__(self) -> None:
        """Walk the five ``src`` trees and parse every module."""
        self.modules: set[str] = set()
        self.defined: dict[str, set[str]] = {}
        self.imported: dict[str, set[str]] = {}
        self.classes: dict[str, set[str]] = defaultdict(set)
        self.class_fq: set[str] = set()
        self.testing_fakes: list[dict[str, Any]] = []
        for pkg, d in DISTS.items():
            root = REPO / d / "src"
            for p in sorted((root / pkg).rglob("*.py")):
                m = modname(p, root)
                tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
                self.modules.add(m)
                defd: set[str] = set()
                imp: set[str] = set()
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defd.add(node.name)
                    elif isinstance(node, ast.ClassDef):
                        defd.add(node.name)
                        self.classes[node.name].add(f"{m}.{node.name}")
                        self.class_fq.add(f"{m}.{node.name}")
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                        tg = node.targets if isinstance(node, ast.Assign) else [node.target]
                        defd |= {t.id for t in tg if isinstance(t, ast.Name)}
                    elif isinstance(node, ast.ImportFrom):
                        imp |= {a.asname or a.name for a in node.names}
                    elif isinstance(node, ast.Import):
                        imp |= {(a.asname or a.name).split(".")[0] for a in node.names}
                self.defined[m] = defd
                self.imported[m] = imp
                if ".testing" in m or m.endswith(".testing"):
                    for node in tree.body:
                        if isinstance(node, ast.ClassDef) and FAKE_CLASS.search(node.name):
                            self.testing_fakes.append(
                                {"class": f"{m}.{node.name}", "line": node.lineno}
                            )

    def split(self, fq: str) -> tuple[str | None, str]:
        """Split a dotted name into (longest source module prefix, remainder)."""
        parts = fq.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in self.modules:
                return cand, ".".join(parts[i:])
        return None, fq

    def attr_origin(self, module: str, attr: str) -> str:
        """Say whether ``attr`` is defined in or imported into ``module``."""
        first = attr.split(".")[0]
        if first in self.defined[module]:
            return "defined-here"
        if first in self.imported[module]:
            return "import-site"
        return "not-found"


class TestFile(ast.NodeVisitor):
    """Collect patch sites, fakes, imports and private accesses from one test file."""

    def __init__(self, path: Path, src: SourceIndex, tests_pkg: str) -> None:
        """Prepare per-file state and pre-scan imports and local classes."""
        self.path = str(path.relative_to(REPO))
        self.src = src
        self.tests_pkg = tests_pkg
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.imap: dict[str, str] = {}
        self.imports: list[dict[str, Any]] = []
        self.local_classes: set[str] = set()
        self.patches: list[dict[str, Any]] = []
        self.fakes: list[dict[str, Any]] = []
        self.privates: list[dict[str, Any]] = []
        self.mocks: list[dict[str, Any]] = []
        self.n_tests = 0
        self.relative_imports = 0
        self.scopes: list[dict[str, tuple[str, str]]] = [{}]
        self._prescan()

    def _prescan(self) -> None:
        """Build the import map and local class set for the whole file."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.imap[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.imap[top] = top
                    self._record_import(node.lineno, a.name, [], node)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.relative_imports += 1
                    continue
                m = node.module or ""
                for a in node.names:
                    self.imap[a.asname or a.name] = f"{m}.{a.name}"
                self._record_import(node.lineno, m, [a.name for a in node.names], node)
            elif isinstance(node, ast.ClassDef):
                self.local_classes.add(node.name)

    def _record_import(self, line: int, module: str, names: list[str], node: ast.AST) -> None:
        """Record an import of a tau module."""
        if dist_of(module) is None:
            return
        self.imports.append({"line": line, "module": module, "names": names})

    def resolve(self, node: ast.AST) -> str | None:
        """Resolve a Name/Attribute chain to a dotted name through the import map."""
        if isinstance(node, ast.Name):
            return self.imap.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def classify(self, node: ast.AST) -> tuple[str, str]:
        """Return (kind, name) for an expression: module / class / local / unknown."""
        fq = self.resolve(node)
        if fq:
            mod, rest = self.src.split(fq)
            if mod and not rest:
                return "module", mod
            if mod and fq in self.src.class_fq:
                return "class", fq
            if mod:
                return "source-attr", fq
            return "external", fq
        if isinstance(node, ast.Name):
            for sc in reversed(self.scopes):
                if node.id in sc:
                    return sc[node.id]
            if node.id in self.local_classes:
                return "local-class", node.id
        return "unknown", ast.unparse(node)

    def type_of_value(self, value: ast.AST) -> tuple[str, str] | None:
        """Infer the type of an assigned value when it is a constructor call."""
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            kind, name = self.classify(value.func)
            if kind == "class":
                return ("class", name)
            if kind == "local-class":
                return ("local", name)
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Enter a function scope with annotation-typed parameters."""
        if node.name.startswith("test"):
            self.n_tests += 1
        if FAKE_FUNC.search(node.name):
            self.fakes.append({"kind": "function", "name": node.name, "line": node.lineno})
        scope: dict[str, tuple[str, str]] = {}
        for a in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            if a.annotation is not None:
                kind, name = self.classify(a.annotation)
                if kind == "class":
                    scope[a.arg] = ("class", name)
        self.scopes.append(scope)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        """Remember ``x = Cls(...)`` bindings in the current scope."""
        t = self.type_of_value(node.value)
        if t:
            for tg in node.targets:
                if isinstance(tg, ast.Name):
                    self.scopes[-1][tg.id] = t
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Remember ``x: Cls = ...`` bindings in the current scope."""
        if isinstance(node.target, ast.Name):
            kind, name = self.classify(node.annotation)
            if kind == "class":
                self.scopes[-1][node.target.id] = ("class", name)
            elif node.value is not None and (t := self.type_of_value(node.value)):
                self.scopes[-1][node.target.id] = t
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        """Remember ``with Cls(...) as x`` bindings."""
        for item in node.items:
            if isinstance(item.optional_vars, ast.Name):
                t = self.type_of_value(item.context_expr)
                if t:
                    self.scopes[-1][item.optional_vars.id] = t
        self.generic_visit(node)

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Record fakes: subclasses of source classes and fake-named classes."""
        bases = []
        for b in node.bases:
            kind, name = self.classify(b)
            bases.append((kind, name))
        src_bases = [n for k, n in bases if k == "class"]
        named = bool(FAKE_CLASS.search(node.name))
        if src_bases or named:
            self.fakes.append(
                {
                    "kind": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "source_bases": src_bases,
                    "fake_named": named,
                    "bases_text": [ast.unparse(b) for b in node.bases],
                }
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Record patch calls, mock constructions and getattr of private names."""
        f = node.func
        fq = self.resolve(f)
        if isinstance(f, ast.Attribute) and f.attr in MONKEY_METHODS and self._is_monkey(f.value):
            self._monkey(node, f.attr)
        elif fq in PATCH_FQ:
            self._patch(node, PATCH_FQ[fq])
        elif fq in MOCK_FQ:
            kws = {k.arg for k in node.keywords}
            self.mocks.append(
                {
                    "line": node.lineno,
                    "ctor": fq.rsplit(".", 1)[1],
                    "spec": bool(kws & {"spec", "spec_set", "autospec"})
                    or fq.endswith("create_autospec"),
                }
            )
        elif isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2:
            a = node.args[1]
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and PRIVATE.match(a.value):
                self._private(node.args[0], a.value, node.lineno, "getattr")
        self.generic_visit(node)

    @staticmethod
    def _is_monkey(v: ast.AST) -> bool:
        """Return True when ``v`` looks like the pytest monkeypatch fixture or a context of it."""
        return isinstance(v, ast.Name) and (
            v.id in {"mp", "mpatch", "m"} or "monkeypatch" in v.id.lower()
        )

    def _add_patch(self, node: ast.Call, api: str, target: str, attr: str, how: str) -> None:
        """Resolve ``target``/``attr`` and append a patch-site record."""
        rec: dict[str, Any] = {
            "line": node.lineno,
            "api": api,
            "target_text": target,
            "attr": attr,
            "form": how,
        }
        rec.update(self._resolved(how, target, attr))
        self.patches.append(rec)

    def _resolved(self, how: str, target: str, attr: str) -> dict[str, Any]:
        """Return module/attr resolution for a patch target."""
        if how == "path":
            mod, rest = self.src.split(target)
            if mod:
                full = rest if rest else "<module>"
                return {
                    "module": mod,
                    "attribute": full,
                    "status": "resolved",
                    "origin": self.src.attr_origin(mod, rest) if rest else "-",
                }
            return {
                "module": "external:" + target.split(".")[0],
                "attribute": ".".join(target.split(".")[1:]),
                "status": "external",
                "origin": "-",
            }
        raise AssertionError(how)

    def _patch_obj(self, node: ast.Call, api: str, obj: ast.AST, attr: str) -> None:
        """Record an ``(object, "attr")`` form patch."""
        kind, name = self.classify(obj)
        rec: dict[str, Any] = {
            "line": node.lineno,
            "api": api,
            "target_text": ast.unparse(obj),
            "attr": attr,
            "form": "object",
        }
        if kind == "module":
            rec.update(
                module=name,
                attribute=attr,
                status="resolved",
                origin=self.src.attr_origin(name, attr),
            )
        elif kind in {"class", "source-attr"}:
            mod, rest = self.src.split(name)
            assert mod
            rec.update(
                module=mod,
                attribute=f"{rest}.{attr}",
                status="resolved",
                origin=self.src.attr_origin(mod, rest),
            )
        elif kind == "external":
            rec.update(
                module="external:" + name.split(".")[0],
                attribute=attr,
                status="external",
                origin="-",
            )
        elif kind in {"local-class", "local"}:
            rec.update(module="test-local:" + name, attribute=attr, status="test-local", origin="-")
        else:
            rec.update(module="unresolved", attribute=attr, status="unresolved", origin="-")
        self.patches.append(rec)

    @staticmethod
    def _const_str(n: ast.AST | None) -> str | None:
        """Return the literal string value of ``n`` or None."""
        return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None

    def _monkey(self, node: ast.Call, method: str) -> None:
        """Record a monkeypatch call."""
        api = f"monkeypatch.{method}"
        args = node.args
        if not args:
            return
        first = self._const_str(args[0])
        if method in {"setitem", "delitem"}:
            kind, name = self.classify(args[0])
            key = self._const_str(args[1]) if len(args) > 1 else None
            rec: dict[str, Any] = {
                "line": node.lineno,
                "api": api,
                "target_text": ast.unparse(args[0]),
                "attr": key or "<dynamic>",
                "form": "item",
            }
            if kind in {"class", "source-attr", "module"}:
                mod, rest = self.src.split(name)
                assert mod
                rec.update(module=mod, attribute=rest or "<module>", status="resolved", origin="-")
            elif kind == "external":
                rec.update(
                    module="external:" + name.split(".")[0],
                    attribute=name,
                    status="external",
                    origin="-",
                )
            else:
                rec.update(
                    module="unresolved",
                    attribute=ast.unparse(args[0]),
                    status="unresolved",
                    origin="-",
                )
            self.patches.append(rec)
            return
        if first is not None and (
            len(args) == 1
            or method == "delattr"
            and len(args) == 1
            or (len(args) == 2 and method == "setattr")
        ):
            self._add_patch(node, api, first, "", "path")
            return
        name = self._const_str(args[1]) if len(args) > 1 else None
        self._patch_obj(node, api, args[0], name or "<dynamic>")

    def _patch(self, node: ast.Call, api: str) -> None:
        """Record a unittest.mock patch call."""
        args = node.args
        if api in {"patch", "patch.dict", "patch.multiple"}:
            if not args:
                return
            first = self._const_str(args[0])
            if first is not None:
                self._add_patch(node, api, first, "", "path")
            else:
                self._patch_obj(
                    node, api, args[0], "<multiple>" if api == "patch.multiple" else "<object>"
                )
        else:
            if len(args) >= 2:
                name = self._const_str(args[1])
                self._patch_obj(node, api, args[0], name or "<dynamic>")
            elif args and (first := self._const_str(args[0])):
                self._add_patch(node, api, first, "", "path")

    def _private(self, recv: ast.AST, attr: str, line: int, ctx: str) -> None:
        """Record a private attribute access on a receiver expression."""
        if isinstance(recv, ast.Name) and recv.id in {"self", "cls"}:
            return
        kind, name = self.classify(recv)
        self.privates.append(
            {
                "line": line,
                "attr": attr,
                "ctx": ctx,
                "receiver_text": ast.unparse(recv),
                "receiver_kind": kind,
                "receiver": name,
            }
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record ``obj._name`` accesses."""
        if PRIVATE.match(node.attr):
            ctx = type(node.ctx).__name__.lower()
            self._private(node.value, node.attr, node.lineno, ctx)
        self.generic_visit(node)


def tests_files() -> list[tuple[str, Path]]:
    """Return (package, path) for every .py under each ``tau-*/tests`` tree."""
    out = []
    for pkg, d in DISTS.items():
        t = REPO / d / "tests"
        if t.is_dir():
            out.extend((pkg, p) for p in sorted(t.rglob("*.py")))
    return out


def fake_key(rec: dict[str, Any], src: SourceIndex) -> tuple[str, str]:
    """Return (key, how) naming what a fake stands for."""
    if rec["kind"] == "class" and rec["source_bases"]:
        return ",".join(sorted(rec["source_bases"])), "subclass"
    name = rec["name"]
    if rec["kind"] == "class":
        stem = re.sub(rf"^_*{FAKE_PREFIX}", "", name)
        stem = re.sub(r"(Fake|Stub|Dummy|Double)$", "", stem).strip("_") or name
    else:
        stem = re.sub(r"^_*(fake|stub|scripted|dummy|mock|canned)_|_(fake|stub)$", "", name)
    cands = sorted(src.classes.get(stem, ()))
    if cands:
        return f"~{stem} (same-named source classes: {', '.join(cands)})", "name-only"
    return f"~{stem}", "no-source-class-of-that-name"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    """Return a Markdown table."""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def analyse() -> dict[str, Any]:
    """Run all four measurements and return the result dict."""
    src = SourceIndex()
    files = tests_files()
    parsed: list[tuple[str, TestFile]] = []
    for pkg, p in files:
        tf = TestFile(p, src, pkg)
        tf.visit(tf.tree)
        parsed.append((pkg, tf))
    res: dict[str, Any] = {
        "denominators": {
            pkg: {
                "test_files": sum(1 for q, _ in parsed if q == pkg),
                "test_functions": sum(t.n_tests for q, t in parsed if q == pkg),
                "relative_imports": sum(t.relative_imports for q, t in parsed if q == pkg),
            }
            for pkg in DISTS
        }
    }
    sites = []
    for pkg, tf in parsed:
        for r in tf.patches:
            sites.append({"tests_pkg": pkg, "file": tf.path, **r})
    sites.sort(key=lambda r: (r["file"], r["line"], r["attr"]))
    by_mod: dict[str, dict[str, Any]] = {}
    for s in sites:
        d = by_mod.setdefault(
            s["module"],
            {
                "sites": 0,
                "attrs": defaultdict(int),
                "files": set(),
                "private_attrs": 0,
                "origins": defaultdict(int),
                "apis": defaultdict(int),
                "test_pkgs": set(),
            },
        )
        d["sites"] += 1
        d["attrs"][s["attribute"]] += 1
        d["files"].add(s["file"])
        d["origins"][s["origin"]] += 1
        d["apis"][s["api"]] += 1
        d["test_pkgs"].add(s["tests_pkg"])
        if PRIVATE.match(s["attribute"].split(".")[-1]):
            d["private_attrs"] += 1
    res["patch_sites"] = sites
    res["patch_by_module"] = {
        m: {
            "sites": d["sites"],
            "distinct_attributes": len(d["attrs"]),
            "attributes": dict(sorted(d["attrs"].items(), key=lambda kv: (-kv[1], kv[0]))),
            "n_files": len(d["files"]),
            "files": sorted(d["files"]),
            "private_attribute_sites": d["private_attrs"],
            "origins": dict(sorted(d["origins"].items())),
            "apis": dict(sorted(d["apis"].items())),
            "tests_packages": sorted(d["test_pkgs"]),
        }
        for m, d in sorted(by_mod.items(), key=lambda kv: (-kv[1]["sites"], kv[0]))
    }
    status = defaultdict(int)
    for s in sites:
        status[s["status"]] += 1
    res["patch_status"] = dict(sorted(status.items()))
    res["mock_constructions"] = [
        {"tests_pkg": pkg, "file": tf.path, **m} for pkg, tf in parsed for m in tf.mocks
    ]
    fakes = []
    for pkg, tf in parsed:
        for fk in tf.fakes:
            key, how = fake_key(fk, src)
            fakes.append({"tests_pkg": pkg, "file": tf.path, "key": key, "how": how, **fk})
    fakes.sort(key=lambda r: (r["key"], r["file"], r["line"]))
    groups: dict[str, dict[str, Any]] = {}
    for fk in fakes:
        g = groups.setdefault(
            fk["key"], {"files": set(), "definitions": [], "hows": set(), "kinds": set()}
        )
        g["files"].add(fk["file"])
        g["definitions"].append(f"{fk['file']}:{fk['line']} {fk['name']}")
        g["hows"].add(fk["how"])
        g["kinds"].add(fk["kind"])
    res["fakes"] = fakes
    res["fake_groups"] = {
        k: {
            "n_files": len(g["files"]),
            "n_definitions": len(g["definitions"]),
            "how": sorted(g["hows"]),
            "kinds": sorted(g["kinds"]),
            "definitions": g["definitions"],
        }
        for k, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]["files"]), kv[0]))
    }
    res["src_testing_fakes"] = src.testing_fakes
    cross: dict[tuple[str, str], dict[str, Any]] = {}
    for pkg, tf in parsed:
        for im in tf.imports:
            tgt = dist_of(im["module"])
            assert tgt
            kind = "own" if tgt == pkg else "cross"
            c = cross.setdefault(
                (pkg, tgt),
                {
                    "relation": kind,
                    "statements": 0,
                    "files": set(),
                    "root": 0,
                    "submodule": 0,
                    "names": set(),
                    "underscore": [],
                },
            )
            c["statements"] += 1
            c["files"].add(tf.path)
            is_root = im["module"] == tgt
            c["root" if is_root else "submodule"] += 1
            mod_us = any(
                p.startswith("_") and not p.endswith("__") for p in im["module"].split(".")
            )
            for n in im["names"] or [""]:
                if n:
                    c["names"].add(f"{im['module']}.{n}")
                if mod_us or (
                    n and n.startswith("_") and not (n.startswith("__") and n.endswith("__"))
                ):
                    c["underscore"].append(
                        f"{tf.path}:{im['line']} {im['module']}{'.' + n if n else ''}"
                    )
    res["test_imports"] = {
        f"{a}->{b}": {
            "relation": c["relation"],
            "statements": c["statements"],
            "n_files": len(c["files"]),
            "root_package_imports": c["root"],
            "submodule_imports": c["submodule"],
            "distinct_names": len(c["names"]),
            "underscore_imports": len(c["underscore"]),
            "underscore_sites": sorted(c["underscore"]),
        }
        for (a, b), c in sorted(cross.items())
    }
    privs = []
    for pkg, tf in parsed:
        for pr in tf.privates:
            privs.append({"tests_pkg": pkg, "file": tf.path, **pr})
    privs.sort(key=lambda r: (r["file"], r["line"], r["attr"]))
    by_cls: dict[str, dict[str, Any]] = {}
    by_attr: dict[str, dict[str, Any]] = {}
    own = defaultdict(int)
    for pr in privs:
        k = pr["receiver_kind"]
        if k in {"class", "module"}:
            key = f"{k}:{pr['receiver']}"
            d = by_cls.setdefault(
                key, {"sites": 0, "attrs": defaultdict(int), "files": set(), "writes": 0}
            )
            d["sites"] += 1
            d["attrs"][pr["attr"]] += 1
            d["files"].add(pr["file"])
            d["writes"] += pr["ctx"] in {"store", "del"}
        elif k in {"local", "local-class"}:
            own[pr["receiver"]] += 1
        else:
            d = by_attr.setdefault(
                pr["attr"],
                {
                    "sites": 0,
                    "files": set(),
                    "receivers": defaultdict(int),
                    "kinds": defaultdict(int),
                    "writes": 0,
                },
            )
            d["sites"] += 1
            d["files"].add(pr["file"])
            d["receivers"][pr["receiver_text"]] += 1
            d["kinds"][k] += 1
            d["writes"] += pr["ctx"] in {"store", "del"}
    res["private_total_sites"] = len(privs)
    res["private_by_resolved"] = {
        k: {
            "sites": d["sites"],
            "distinct_attrs": len(d["attrs"]),
            "attrs": dict(sorted(d["attrs"].items())),
            "n_files": len(d["files"]),
            "write_sites": d["writes"],
        }
        for k, d in sorted(by_cls.items(), key=lambda kv: (-kv[1]["sites"], kv[0]))
    }
    res["private_on_test_local_classes"] = dict(sorted(own.items(), key=lambda kv: (-kv[1], kv[0])))
    res["private_unresolved_by_attr"] = {
        k: {
            "sites": d["sites"],
            "n_files": len(d["files"]),
            "write_sites": d["writes"],
            "receiver_kinds": dict(sorted(d["kinds"].items())),
            "top_receivers": dict(
                sorted(d["receivers"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            ),
        }
        for k, d in sorted(by_attr.items(), key=lambda kv: (-kv[1]["sites"], kv[0]))
    }
    res["private_sites"] = privs
    return res


def summary(res: dict[str, Any]) -> str:
    """Render the Markdown summary."""
    L = ["# mocks.py summary", "", "## Denominators", ""]
    L.append(
        table(
            ["tests of", "test files", "test functions", "relative imports"],
            [
                [p, d["test_files"], d["test_functions"], d["relative_imports"]]
                for p, d in res["denominators"].items()
            ],
        )
    )
    L += [
        "",
        "## 1. Patch sites",
        "",
        f"Total patch sites: {len(res['patch_sites'])}; by resolution status: {res['patch_status']}",
        "",
    ]
    L.append(
        table(
            [
                "target module",
                "sites",
                "distinct attributes",
                "files",
                "private-attr sites",
                "origin (defined-here/import-site/not-found)",
                "tests packages",
            ],
            [
                [
                    m,
                    d["sites"],
                    d["distinct_attributes"],
                    d["n_files"],
                    d["private_attribute_sites"],
                    "/".join(
                        str(d["origins"].get(o, 0))
                        for o in ("defined-here", "import-site", "not-found")
                    ),
                    ",".join(d["tests_packages"]),
                ]
                for m, d in res["patch_by_module"].items()
            ],
        )
    )
    L += ["", "### Patched attributes and files per module (top attributes)", ""]
    for m, d in res["patch_by_module"].items():
        attrs = ", ".join(f"`{a}` x{n}" for a, n in list(d["attributes"].items())[:8])
        L.append(
            f"- `{m}` ({d['sites']} sites): {attrs}; files: "
            + ", ".join(f"`{f}`" for f in d["files"][:6])
            + (f" (+{len(d['files']) - 6} more)" if len(d["files"]) > 6 else "")
        )
    cm = res["mock_constructions"]
    L += [
        "",
        f"Mock/MagicMock/AsyncMock/create_autospec constructions: {len(cm)} (with spec/autospec: {sum(1 for m in cm if m['spec'])})",
        "",
    ]
    L += ["## 2. Fakes and stubs defined in tests", ""]
    L.append(
        table(
            ["stands for", "how matched", "kinds", "test files defining one", "definitions"],
            [
                [k, ",".join(g["how"]), ",".join(g["kinds"]), g["n_files"], g["n_definitions"]]
                for k, g in res["fake_groups"].items()
            ],
        )
    )
    L += ["", "### Definitions of the groups defined in 2+ files", ""]
    for k, g in res["fake_groups"].items():
        if g["n_files"] >= 2:
            L.append(f"- `{k}`: " + "; ".join(g["definitions"]))
    L += ["", "### Fake-named classes shipped in src `testing` modules", ""]
    L.append(", ".join(f"`{x['class']}`" for x in res["src_testing_fakes"]) or "none")
    L += ["", "## 3. Test imports of tau packages", ""]
    L.append(
        table(
            [
                "tests of -> imports",
                "relation",
                "statements",
                "files",
                "root-package",
                "submodule",
                "distinct names",
                "underscore",
            ],
            [
                [
                    k,
                    v["relation"],
                    v["statements"],
                    v["n_files"],
                    v["root_package_imports"],
                    v["submodule_imports"],
                    v["distinct_names"],
                    v["underscore_imports"],
                ]
                for k, v in res["test_imports"].items()
            ],
        )
    )
    L += ["", "### Underscore imports (module path component or name begins with _)", ""]
    for k, v in res["test_imports"].items():
        if v["underscore_sites"]:
            L.append(f"- {k} ({v['underscore_imports']}):")
            L += [f"  - {s}" for s in v["underscore_sites"][:40]]
            if len(v["underscore_sites"]) > 40:
                L.append(f"  - ... +{len(v['underscore_sites']) - 40} more in mocks.json")
    L += [
        "",
        "## 4. Private-attribute reach",
        "",
        f"Total `obj._x` sites (excluding self/cls receivers): {res['private_total_sites']}",
        "",
    ]
    L += ["### Receiver resolved to a source class or module", ""]
    L.append(
        table(
            ["receiver", "sites", "distinct attrs", "files", "write/del sites", "attrs"],
            [
                [
                    k,
                    d["sites"],
                    d["distinct_attrs"],
                    d["n_files"],
                    d["write_sites"],
                    ", ".join(f"`{a}` x{n}" for a, n in list(d["attrs"].items())[:10]),
                ]
                for k, d in res["private_by_resolved"].items()
            ],
        )
    )
    L += ["", "### Receiver is a class defined in the same test file", ""]
    L.append(
        ", ".join(f"`{k}` x{n}" for k, n in res["private_on_test_local_classes"].items()) or "none"
    )
    un = res["private_unresolved_by_attr"]
    L += [
        "",
        f"### Receiver unresolved, grouped by attribute name ({len(un)} names, {sum(d['sites'] for d in un.values())} sites)",
        "",
    ]
    L.append(
        table(
            [
                "attribute",
                "sites",
                "files",
                "write/del sites",
                "receiver kinds",
                "top receiver expressions",
            ],
            [
                [
                    f"`{k}`",
                    d["sites"],
                    d["n_files"],
                    d["write_sites"],
                    d["receiver_kinds"],
                    ", ".join(f"`{r}` x{n}" for r, n in d["top_receivers"].items()),
                ]
                for k, d in list(un.items())[:80]
            ],
        )
    )
    if len(un) > 80:
        L.append(f"\n(+{len(un) - 80} more attribute names in mocks.json)")
    return "\n".join(L) + "\n"


def main() -> None:
    """Parse arguments, analyse, and write outputs."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "build/arch/mocks", help="output directory")
    args = ap.parse_args()
    if not any((REPO / d / "tests").is_dir() for d in DISTS.values()):
        sys.exit("no tau-*/tests directories found")
    args.out.mkdir(parents=True, exist_ok=True)
    res = analyse()
    (args.out / "mocks.json").write_text(
        json.dumps(res, indent=1, sort_keys=True, default=sorted) + "\n", encoding="utf-8"
    )
    (args.out / "mocks_summary.md").write_text(summary(res), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
