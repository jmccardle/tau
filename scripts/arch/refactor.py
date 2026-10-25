"""IDE-style find-references and rename for the five tau packages, on rope.

The rope project root is the repo root; its source folders are the five ``src``
trees plus every ``tests`` tree, ``examples`` and ``scripts``. No ``.ropeproject``
directory is written. A symbol is given as a dotted path
(``tau_agent_core.agent_session.AgentSession.set_model``; re-exports such as
``tau_llm.Model.context_window`` resolve through ``from ... import`` chains) or
as ``path/to/file.py:LINE:COL`` (1-based line, 0-based column of the name).

Subcommands:
    refs SYMBOL [--no-hierarchy] [--compare-grep]
        List every occurrence as ``file:line:col``; ``?`` marks occurrences rope
        cannot type-check (attribute access on an unknown receiver). With
        ``--compare-grep`` also list word-boundary text hits that rope did not
        report, classified as docstring / string literal / comment / docs / code.
    rename SYMBOL NEW_NAME [--unsure] [--no-hierarchy] [--apply]
        Print a unified diff of rope's changes, then the text hits it will NOT
        change. Nothing is written unless ``--apply`` is given.

Usage:
    python scripts/arch/refactor.py refs tau_agent_core.agent_session.AgentSession.set_model
    python scripts/arch/refactor.py rename tau_llm.types.Model.context_window ctx_window
    python scripts/arch/refactor.py rename SYMBOL NEW --apply

Output goes to stdout (``--json FILE`` additionally writes the data).
"""

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

try:
    from rope.base.project import Project
    from rope.contrib.findit import find_occurrences
    from rope.refactor.rename import Rename
except ImportError:
    raise SystemExit("rope is required: pip install rope") from None

ROOT = Path(__file__).resolve().parents[2]
SRC_TREES = ("tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta")
EXTRA_FOLDERS = (
    "tau-llm/tests",
    "tau-agent-core/tests",
    "tau-coding-agent/tests",
    "tau-jmfts/tests",
    "examples",
    "scripts",
)
IGNORED = (
    "*.pyc",
    ".git",
    ".claude",
    ".ropeproject",
    "__pycache__",
    "venv",
    ".venv",
    "node_modules",
    "build",
    "dist",
    "*.egg-info",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
)
TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".tcss", ".json", ".cfg", ".rst", ".sh"}
SKIP_DIRS = {
    ".git",
    ".claude",
    "venv",
    ".venv",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


def refactor_project() -> Project:
    """Open the rope project on the repo root with the tau source folders."""
    folders = [f"{t}/src" for t in SRC_TREES] + list(EXTRA_FOLDERS)
    missing = [f for f in folders if not (ROOT / f).is_dir()]
    if missing:
        raise SystemExit(f"missing source folders: {missing}")
    return Project(
        str(ROOT),
        ropefolder=None,
        source_folders=folders,
        ignored_resources=list(IGNORED),
        save_objectdb=False,
        validate_objectdb=False,
    )


def refactor_module_file(dotted: str) -> Path | None:
    """Return the file of module ``dotted`` under the five source trees, or None."""
    rel = Path(*dotted.split("."))
    for tree in SRC_TREES:
        base = ROOT / tree / "src" / rel
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
    return None


def refactor_module_name(path: Path, tree: ast.Module, level: int, module: str | None) -> str:
    """Resolve a (possibly relative) ``from`` import target to an absolute module name."""
    if not level:
        return module or ""
    for t in SRC_TREES:
        src = ROOT / t / "src"
        if src in path.parents:
            parts = list(path.relative_to(src).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            else:
                parts = parts[:-1]
            parts = parts[: len(parts) - (level - 1)]
            return ".".join(parts + ([module] if module else []))
    raise SystemExit(f"{path} is not under a source tree")


def refactor_find_in_body(
    body: list[ast.stmt], name: str, lines: list[str]
) -> tuple[ast.stmt, int, int] | None:
    """Find the definition of ``name`` in ``body``: ``(node, line, col)`` of the name."""
    for node in body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            text = lines[node.lineno - 1].encode("utf-8")
            m = re.search(rb"\b(?:def|class)\s+(" + name.encode() + rb")\b", text)
            if m is None:
                raise SystemExit(f"cannot locate the name of {name!r} on line {node.lineno}")
            return node, node.lineno, m.start(1)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node, node.target.lineno, node.target.col_offset
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node, t.lineno, t.col_offset
        if isinstance(node, (ast.If, ast.Try)):
            got = refactor_find_in_body(node.body + getattr(node, "orelse", []), name, lines)
            if got:
                return got
    return None


def refactor_find_self_attr(cls: ast.ClassDef, name: str) -> tuple[int, int] | None:
    """Find the first ``self.NAME = ...`` assignment in a class's methods."""
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                targets = []
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, ast.AnnAssign):
                    targets = [sub.target]
                for t in targets:
                    if (
                        isinstance(t, ast.Attribute)
                        and t.attr == name
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                    ):
                        return t.end_lineno or t.lineno, (t.end_col_offset or 0) - len(name)
    return None


def refactor_resolve_symbol(spec: str) -> tuple[Path, int, int]:
    """Resolve a symbol spec to ``(file, line, col)`` of its defining name, or exit."""
    m = re.fullmatch(r"(.+\.py):(\d+):(\d+)", spec)
    if m:
        path = (ROOT / m.group(1)).resolve()
        if not path.is_file():
            raise SystemExit(f"no such file: {m.group(1)}")
        return path, int(m.group(2)), int(m.group(3))
    parts = spec.split(".")
    path, rest = None, []
    for i in range(len(parts), 0, -1):
        path = refactor_module_file(".".join(parts[:i]))
        if path:
            rest = parts[i:]
            break
    if path is None:
        raise SystemExit(f"no module found for {spec!r} in the five source trees")
    if not rest:
        raise SystemExit(f"{spec!r} names a module; give a class, function or attribute")
    for _ in range(20):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        body, hit = tree.body, None
        for depth, name in enumerate(rest):
            hit = refactor_find_in_body(body, name, lines)
            if hit is None:
                break
            node, line, col = hit
            if depth == len(rest) - 1:
                return path, line, col
            if not isinstance(node, ast.ClassDef):
                raise SystemExit(f"{name!r} in {path} is not a class; cannot descend into it")
            body = node.body
            if depth == len(rest) - 2:
                last = rest[-1]
                if refactor_find_in_body(body, last, lines) is None:
                    got = refactor_find_self_attr(node, last)
                    if got:
                        return path, got[0], got[1]
        if hit is None and len(rest) >= 1:
            name = rest[0]
            target = None
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if (a.asname or a.name) == name:
                            mod = refactor_module_name(path, tree, node.level, node.module)
                            target = f"{mod}.{a.name}"
            if target is None:
                raise SystemExit(f"cannot find {'.'.join(rest)!r} in {path}")
            sub = target.split(".")
            for i in range(len(sub), 0, -1):
                nxt = refactor_module_file(".".join(sub[:i]))
                if nxt:
                    path, rest = nxt, sub[i:] + rest[1:]
                    break
            else:
                raise SystemExit(f"re-export target {target!r} is outside the five trees")
    raise SystemExit(f"re-export chain too deep for {spec!r}")


def refactor_offset(path: Path, line: int, col: int) -> int:
    """Convert ast (line, utf-8 byte col) to a character offset into the file text."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    prefix = "".join(lines[: line - 1])
    raw = lines[line - 1].encode("utf-8")[:col].decode("utf-8")
    return len(prefix) + len(raw)


def refactor_line_col(text: str, offset: int) -> tuple[int, int]:
    """Return 1-based line and 0-based column for a character offset."""
    line = text.count("\n", 0, offset) + 1
    return line, offset - (text.rfind("\n", 0, offset) + 1)


def refactor_occurrences(project: Project, spec: str, hierarchy: bool) -> tuple[str, list[dict]]:
    """Return ``(name, occurrences)`` with file, line, col, offset and unsure flag."""
    path, line, col = refactor_resolve_symbol(spec)
    resource = project.get_resource(str(path.relative_to(ROOT)))
    offset = refactor_offset(path, line, col)
    name = path.read_text(encoding="utf-8")[offset:].split("(")[0]
    name = re.match(r"\w+", name).group(0)  # type: ignore[union-attr]
    found = find_occurrences(project, resource, offset, unsure=True, in_hierarchy=hierarchy)
    out = []
    for occ in found:
        text = occ.resource.read()
        start = occ.offset
        ln, cl = refactor_line_col(text, start)
        out.append(
            {
                "file": str(Path(occ.resource.real_path).relative_to(ROOT)),
                "line": ln,
                "col": cl,
                "offset": start,
                "unsure": bool(occ.unsure),
                "definition": start == offset and occ.resource == resource,
            }
        )
    out.sort(key=lambda o: (o["file"], o["line"], o["col"]))
    return name, out


def refactor_docstring_lines(tree: ast.Module) -> set[int]:
    """Return line numbers covered by docstrings (module, class, function)."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return lines


def refactor_classify(path: Path, text: str, hits: list[int]) -> dict[int, str]:
    """Classify word hits (character offsets) as docstring / string / comment / code / docs."""
    if path.suffix != ".py":
        return {h: "docs" if path.suffix == ".md" else "text-file" for h in hits}
    try:
        tree = ast.parse(text)
        doc_lines = refactor_docstring_lines(tree)
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError):
        return {h: "unparsed-file" for h in hits}
    starts = [0]
    for ln in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(ln))
    spans = []
    for t in toks:
        if t.type in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE):
            s = starts[t.start[0] - 1] + t.start[1]
            e = starts[t.end[0] - 1] + t.end[1]
            spans.append((s, e, t.type, t.start[0]))
    out = {}
    for h in hits:
        kind = "code"
        for s, e, ty, ln in spans:
            if s <= h < e:
                if ty == tokenize.COMMENT:
                    kind = "comment"
                elif refactor_line_of(starts, h) in doc_lines:
                    kind = "docstring"
                else:
                    kind = "string-literal"
                break
        out[h] = kind
    return out


def refactor_line_of(starts: list[int], offset: int) -> int:
    """Return the 1-based line holding a character offset."""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def refactor_text_hits(name: str, skip: set[tuple[str, int]]) -> list[dict]:
    """Word-boundary hits of ``name`` in repo text files, excluding ``skip`` (file, offset)."""
    rx = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts) or any(p.endswith(".egg-info") for p in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offsets = [m.start() for m in rx.finditer(text)]
        offsets = [o for o in offsets if (str(rel), o) not in skip]
        if not offsets:
            continue
        kinds = refactor_classify(path, text, offsets)
        for o in offsets:
            ln, cl = refactor_line_col(text, o)
            out.append({"file": str(rel), "line": ln, "col": cl, "kind": kinds[o]})
    return out


def refactor_print_occurrences(occs: list[dict]) -> None:
    """Print occurrences one per line as ``file:line:col``."""
    for o in occs:
        flag = " ?" if o["unsure"] else ""
        mark = " [def]" if o["definition"] else ""
        print(f"{o['file']}:{o['line']}:{o['col']}{flag}{mark}")


def refactor_print_unchanged(hits: list[dict]) -> None:
    """Print text hits that rope reported no occurrence for, grouped by kind."""
    kinds: dict[str, int] = {}
    for h in hits:
        kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
    print(f"\ntext hits rope will NOT change: {len(hits)}  {dict(sorted(kinds.items()))}")
    for h in hits:
        print(f"  {h['file']}:{h['line']}:{h['col']}  {h['kind']}")


def refactor_refs(args: argparse.Namespace) -> int:
    """Implement the ``refs`` subcommand."""
    project = refactor_project()
    try:
        name, occs = refactor_occurrences(project, args.symbol, not args.no_hierarchy)
        sure = sum(1 for o in occs if not o["unsure"])
        print(f"# {name}: {len(occs)} occurrences ({sure} certain, {len(occs) - sure} unsure ?)")
        refactor_print_occurrences(occs)
        data: dict = {"name": name, "occurrences": occs}
        if args.compare_grep:
            skip = {(o["file"], o["offset"]) for o in occs}
            hits = refactor_text_hits(name, skip)
            refactor_print_unchanged(hits)
            data["unreported_text_hits"] = hits
        if args.json:
            Path(args.json).write_text(
                json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
    finally:
        project.close()
    return 0


def refactor_rename(args: argparse.Namespace) -> int:
    """Implement the ``rename`` subcommand (dry run unless ``--apply``)."""
    if not re.fullmatch(r"[A-Za-z_]\w*", args.new_name):
        raise SystemExit(f"{args.new_name!r} is not a valid identifier")
    project = refactor_project()
    try:
        path, line, col = refactor_resolve_symbol(args.symbol)
        resource = project.get_resource(str(path.relative_to(ROOT)))
        offset = refactor_offset(path, line, col)
        name, occs = refactor_occurrences(project, args.symbol, not args.no_hierarchy)
        changes = Rename(project, resource, offset).get_changes(
            args.new_name,
            in_hierarchy=not args.no_hierarchy,
            unsure=(lambda _occ: True) if args.unsure else None,
            docs=False,
        )
        changed = [str(Path(c.resource.real_path).relative_to(ROOT)) for c in changes.changes]
        applied = {(o["file"], o["offset"]) for o in occs if args.unsure or not o["unsure"]}
        unsure = [o for o in occs if o["unsure"] and not args.unsure]
        print(f"# rename {name} -> {args.new_name}: {len(changed)} file(s) change")
        print(changes.get_description())
        if unsure:
            print(
                f"\nunsure occurrences rope SKIPS (rerun with --unsure to include): {len(unsure)}"
            )
            for o in unsure:
                print(f"  {o['file']}:{o['line']}:{o['col']}")
        hits = refactor_text_hits(name, applied)
        refactor_print_unchanged(
            [
                h
                for h in hits
                if (h["file"], h["line"], h["col"])
                not in {(u["file"], u["line"], u["col"]) for u in unsure}
            ]
        )
        if args.json:
            data = {
                "name": name,
                "new": args.new_name,
                "files": sorted(changed),
                "unsure": unsure,
                "unchanged_text": hits,
            }
            Path(args.json).write_text(
                json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
        if args.apply:
            project.do(changes)
            print("\napplied.")
        else:
            print("\ndry run: nothing written (use --apply).")
    finally:
        project.close()
    return 0


def refactor_main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a subcommand."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refs", help="list references to a symbol")
    r.add_argument("symbol")
    r.add_argument("--no-hierarchy", action="store_true", help="do not follow overriding methods")
    r.add_argument("--compare-grep", action="store_true", help="list text hits rope did not report")
    r.add_argument("--json", help="also write the data to this file")
    r.set_defaults(fn=refactor_refs)
    n = sub.add_parser("rename", help="rename a symbol (dry run by default)")
    n.add_argument("symbol")
    n.add_argument("new_name")
    n.add_argument(
        "--unsure", action="store_true", help="also change occurrences rope is unsure of"
    )
    n.add_argument("--no-hierarchy", action="store_true", help="do not rename overriding methods")
    n.add_argument("--apply", action="store_true", help="write the changes")
    n.add_argument("--json", help="also write the data to this file")
    n.set_defaults(fn=refactor_rename)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(refactor_main())
