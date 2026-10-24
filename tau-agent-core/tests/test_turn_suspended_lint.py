"""The lint that keeps ``TurnSuspended`` reaching the loop (docs/STEP-DEBUGGER.md §3.2.2).

Walks the AST of the five ``src`` trees and fails on any construct that could
swallow or rewrite a ``BaseException``: ``except BaseException``, a bare
``except:``, ``except*``, ``contextlib.suppress(BaseException)``, a ``return``
in ``finally``, and ``asyncio.gather(..., return_exceptions=True)``. A handler
is compliant when every path through it ends in a bare ``raise``, or an earlier
handler of the same ``try`` re-raises ``TurnSuspended``. A ``gather`` site is
compliant when its function re-raises ``TurnSuspended`` from the results. Any
other site must be on :data:`ALLOWED`, keyed by file and enclosing function,
with the reason it is mapped or unreachable from a suspension.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC_TREES = [
    REPO / "tau-llm" / "src",
    REPO / "tau-agent-core" / "src",
    REPO / "tau-coding-agent" / "src",
    REPO / "tau-jmfts" / "src",
    REPO / "tau-meta" / "src",
]

ALLOWED: dict[tuple[str, str, str], str] = {
    (
        "tau-agent-core/src/tau_agent_core/agent_session.py",
        "AgentSession._run_threadsafe_submission",
        "except TurnSuspended",
    ): "mapped: resolves the caller's future as accepted with end_reason 'suspended'",
    (
        "tau-agent-core/src/tau_agent_core/codemode/_cpython_child.py",
        "_run",
        "except BaseException",
    ): "runs in code mode's child process, which has no hook dispatch to suspend in",
    (
        "tau-llm/src/tau_llm/abort.py",
        "_cancel",
        "gather",
    ): "waits out a provider read it cancelled; tau_llm runs no hooks",
    (
        "tau-agent-core/src/tau_agent_core/agent_session.py",
        "AgentSession.wait_for_deliveries",
        "gather",
    ): "only waits; each delivery task's outcome reaches _on_delivery_done",
    (
        "tau-agent-core/src/tau_agent_core/compare.py",
        "Comparison.end",
        "gather",
    ): "discards turns it just aborted or cancelled; their outcome is abandoned on purpose",
    (
        "tau-agent-core/src/tau_agent_core/codemode/cpython.py",
        "CPythonProvider.close",
        "gather",
    ): "awaits a spare child's spawn, which runs no hooks",
    (
        "tau-agent-core/src/tau_agent_core/codemode/scoped.py",
        "ScopedProvider.run",
        "gather",
    ): "awaits a killed script; its nested tool calls refuse api.suspend (§3.2)",
    (
        "tau-coding-agent/src/tau_coding_agent/serve/client.py",
        "ServeClient.close",
        "gather",
    ): "awaits a websocket reader task, which runs no hooks",
    (
        "tau-coding-agent/src/tau_coding_agent/serve/daemon.py",
        "Daemon.handle",
        "gather",
    ): "awaits a websocket sender task, which runs no hooks",
}


def _is_bare_raise(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Raise) and stmt.exc is None


def _ends_in_raise(body: list[ast.stmt]) -> bool:
    """Whether every path through ``body`` ends in a bare ``raise``."""
    if not body:
        return False
    last = body[-1]
    if _is_bare_raise(last):
        return True
    if isinstance(last, ast.If):
        return _ends_in_raise(last.body) and _ends_in_raise(last.orelse)
    return False


def _escapes(body: list[ast.stmt]) -> bool:
    """Whether ``body`` holds a ``return``, ``break`` or ``continue`` outside a nested scope."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Return, ast.Break, ast.Continue)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            stack.extend(n for n in ast.walk(node) if isinstance(n, ast.Return))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_names(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _mentions_turn_suspended(func: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "TurnSuspended" for n in ast.walk(func))


class _Scanner(ast.NodeVisitor):
    """Collects every non-compliant site in one module, with its enclosing qualname."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.funcs: list[ast.AST] = []
        self.hits: list[tuple[int, str, str]] = []

    def _qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _flag(self, node: ast.AST, kind: str, detail: str) -> None:
        key = (self.rel, self._qualname(), kind)
        if key not in ALLOWED:
            self.hits.append((getattr(node, "lineno", 0), kind, detail))

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.funcs.append(node)
        self.generic_visit(node)
        self.funcs.pop()
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def _check_try(self, node: ast.Try | ast.TryStar, star: bool) -> None:
        if star:
            self._flag(node, "except*", "except* can split a TurnSuspended out of its group")
        reraised = False
        for handler in node.handlers:
            names = _names(handler.type)
            compliant = _ends_in_raise(handler.body) and not _escapes(handler.body)
            if "TurnSuspended" in names:
                if compliant:
                    reraised = True
                else:
                    self._flag(handler, "except TurnSuspended", "catches it and does not re-raise")
                continue
            if handler.type is None or "BaseException" in names:
                kind = "bare except" if handler.type is None else "except BaseException"
                if not (compliant or reraised):
                    self._flag(handler, kind, "a path does not end in a bare raise")
        for stmt in node.finalbody:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Return):
                    self._flag(
                        sub, "return in finally", "a return in finally discards the exception"
                    )

    def visit_Try(self, node: ast.Try) -> None:
        self._check_try(node, star=False)
        self.generic_visit(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._check_try(node, star=True)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "suppress" and any("BaseException" in _names(a) for a in node.args):
            self._flag(node, "suppress(BaseException)", "suppresses every BaseException")
        if name == "gather" and any(
            kw.arg == "return_exceptions"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
            for kw in node.keywords
        ):
            if not (self.funcs and _mentions_turn_suspended(self.funcs[-1])):
                self._flag(
                    node, "gather", "return_exceptions=True turns TurnSuspended into a value"
                )
        self.generic_visit(node)


def scan_source(rel: str, source: str) -> list[tuple[int, str, str]]:
    """Every non-compliant site in ``source``, as ``(line, kind, detail)``."""
    scanner = _Scanner(rel)
    scanner.visit(ast.parse(source))
    return scanner.hits


def _offenders() -> list[str]:
    found = []
    for tree in SRC_TREES:
        for path in sorted(tree.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            for line, kind, detail in scan_source(rel, path.read_text(encoding="utf-8")):
                found.append(f"{rel}:{line}: {kind}: {detail}")
    return found


def test_no_handler_in_the_five_src_trees_can_swallow_turn_suspended():
    offenders = _offenders()
    assert not offenders, "TurnSuspended could be swallowed (STEP-DEBUGGER §3.2.2):\n" + "\n".join(
        offenders
    )


def test_every_allowlist_entry_still_names_a_site():
    seen: set[tuple[str, str, str]] = set()
    for tree in SRC_TREES:
        for path in sorted(tree.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            source = path.read_text(encoding="utf-8")
            seen.update((rel, qualname, kind) for qualname, kind in _qualified_hits(rel, source))
    stale = sorted(set(ALLOWED) - seen)
    assert not stale, f"allowlist entries name no site any more: {stale}"


def _qualified_hits(rel: str, source: str) -> list[tuple[str, str]]:
    """Every site the scanner would flag with no allowlist, as ``(qualname, kind)``."""
    hits: list[tuple[str, str]] = []

    class _Recording(_Scanner):
        def _flag(self, node: ast.AST, kind: str, detail: str) -> None:
            hits.append((self._qualname(), kind))

    _Recording(rel).visit(ast.parse(source))
    return hits


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (
            "async def f(xs):\n    import asyncio\n    await asyncio.gather(*xs, return_exceptions=True)\n",
            "gather",
        ),
        ("def f():\n    try:\n        g()\n    except:\n        pass\n", "bare except"),
        (
            "def f():\n    try:\n        g()\n    except BaseException:\n        log()\n",
            "except BaseException",
        ),
        (
            "def f():\n    try:\n        g()\n    except BaseException as e:\n        raise RuntimeError() from e\n",
            "except BaseException",
        ),
        (
            "def f():\n    try:\n        g()\n    except BaseException:\n        if x:\n            return 1\n        raise\n",
            "except BaseException",
        ),
        ("def f():\n    try:\n        g()\n    finally:\n        return 1\n", "return in finally"),
        (
            "import contextlib\ndef f():\n    with contextlib.suppress(BaseException):\n        g()\n",
            "suppress(BaseException)",
        ),
        ("def f():\n    try:\n        g()\n    except* ValueError:\n        pass\n", "except*"),
        (
            "def f():\n    try:\n        g()\n    except TurnSuspended:\n        pass\n",
            "except TurnSuspended",
        ),
    ],
)
def test_the_lint_flags_a_new_offender_with_its_line(source, kind):
    hits = scan_source("x.py", source)
    assert [k for _, k, _ in hits] == [kind]
    assert all(line > 0 for line, _, _ in hits)


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    try:\n        g()\n    except BaseException:\n        cleanup()\n        raise\n",
        "def f():\n    try:\n        g()\n    except TurnSuspended:\n        raise\n    except BaseException:\n        pass\n",
        "async def f(xs):\n    import asyncio\n    rs = await asyncio.gather(*xs, return_exceptions=True)\n"
        "    for r in rs:\n        if isinstance(r, TurnSuspended):\n            raise r\n",
        "def f():\n    try:\n        g()\n    except BaseException:\n        if x:\n            raise\n        else:\n            raise\n",
    ],
)
def test_the_lint_passes_cleanup_then_reraise_and_explicit_handling(source):
    assert scan_source("x.py", source) == []
