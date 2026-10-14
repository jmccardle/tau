"""A stateful Monty sandbox whose state lives in the τ tree (experiment, 2026-10-05).

Each script a cursor runs writes one ``customEntry`` of type ``sandbox_state`` at
that cursor's leaf. The entry holds the interpreter's dump (inline, or a
content-addressed side file), a listing of the variables, and the nested tool
calls with their recorded results. The sandbox at any entry is the newest such
entry on that entry's ancestry, so rollback, fork and resume come from the tree's
ordinary ``parentId`` walk. A live interpreter is only a cache of one state entry.

See RESULTS.md beside this file for what was measured and what it means.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pydantic_monty as monty

CUSTOM_TYPE = "sandbox_state"
"""The ``customType`` of a sandbox state entry."""

FORMAT_VERSION = 1
"""The layout of an entry's ``data``; bumped when a reader would misread it."""

INLINE_LIMIT = 64 * 1024
"""Largest compressed dump kept inline as base64; anything larger goes to a side file."""

SHORT_REPR = 120
"""Characters of a value's repr shown in a listing."""

OUTPUT_CAP = 4096
"""Characters of printed output kept in an entry and returned to the model."""


class SandboxError(RuntimeError):
    """A state cannot be restored as recorded; raised rather than starting empty."""


class BlobStore:
    """Content-addressed side files for dumps too large to inline.

    A file is named by the sha256 of the raw dump and holds it zlib-compressed. The
    entry names the hash, so the tree still pins the exact bytes; a missing or
    altered file is an error on read, never an empty sandbox.
    """

    def __init__(self, root: Path) -> None:
        """Keep blobs under ``root``, created on first write."""
        self.root = root

    @classmethod
    def beside(cls, session_path: Path) -> BlobStore:
        """The store for a session file: ``<file>.sandbox/`` next to it."""
        return cls(session_path.with_name(session_path.name + ".sandbox"))

    def put(self, raw: bytes, compressed: bytes) -> str:
        """Write ``compressed`` under ``raw``'s hash unless present; return the hash."""
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / f"{digest}.zlib"
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(compressed)
            tmp.replace(path)
        return digest

    def get(self, digest: str) -> bytes:
        """The raw dump stored under ``digest``.

        Raises:
            SandboxError: the file is missing or does not hash to ``digest``.
        """
        path = self.root / f"{digest}.zlib"
        if not path.exists():
            raise SandboxError(f"sandbox blob {digest} is missing from {self.root}")
        raw = zlib.decompress(path.read_bytes())
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SandboxError(f"sandbox blob {digest} does not match its hash")
        return raw


def encode_state(raw: bytes, blobs: BlobStore | None) -> dict[str, Any]:
    """The ``state`` field for a dump: inline when small, a blob reference otherwise.

    Raises:
        SandboxError: the dump is too large to inline and no blob store was given.
    """
    compressed = zlib.compress(raw, 6)
    digest = hashlib.sha256(raw).hexdigest()
    ref: dict[str, Any] = {
        "engine": "monty",
        "engine_version": monty.__version__,
        "sha256": digest,
        "raw_bytes": len(raw),
        "compressed_bytes": len(compressed),
    }
    if len(compressed) <= INLINE_LIMIT:
        ref["inline"] = base64.b64encode(compressed).decode("ascii")
        return ref
    if blobs is None:
        raise SandboxError(
            f"dump compresses to {len(compressed)} bytes, over the inline limit, "
            "and this sandbox has no blob store"
        )
    blobs.put(raw, compressed)
    ref["blob"] = digest
    return ref


def decode_state(ref: dict[str, Any], blobs: BlobStore | None) -> bytes:
    """The raw dump a ``state`` field names, verified against its hash.

    Raises:
        SandboxError: wrong engine version, a blob with no store, or a hash mismatch.
    """
    if ref.get("engine") != "monty" or ref.get("engine_version") != monty.__version__:
        raise SandboxError(
            f"state was written by {ref.get('engine')} {ref.get('engine_version')}; "
            f"this is monty {monty.__version__}. Rebuild it by replay (RESULTS.md §5)."
        )
    if "inline" in ref:
        raw = zlib.decompress(base64.b64decode(ref["inline"]))
    elif blobs is None:
        raise SandboxError(f"state {ref['sha256']} is a side file and no blob store was given")
    else:
        raw = blobs.get(ref["blob"])
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise SandboxError(f"state {ref['sha256']} does not match its hash")
    return raw


def _ancestry(entries: list[dict[str, Any]], leaf: str | None) -> list[dict[str, Any]]:
    """``leaf``'s path, leaf first, by ``parentId``."""
    by_id = {str(e["id"]): e for e in entries if e.get("id") is not None}
    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = leaf
    while current is not None and current not in seen and current in by_id:
        seen.add(current)
        path.append(by_id[current])
        parent = by_id[current].get("parentId")
        current = str(parent) if parent is not None else None
    return path


def _is_sandbox(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "customEntry" and entry.get("customType") == CUSTOM_TYPE


def last_sandbox_entry_at(entries: list[dict[str, Any]], leaf: str | None) -> dict[str, Any] | None:
    """The newest ``sandbox_state`` entry on ``leaf``'s path, whatever its outcome."""
    return next((e for e in _ancestry(entries, leaf) if _is_sandbox(e)), None)


def state_entry_at(entries: list[dict[str, Any]], leaf: str | None) -> dict[str, Any] | None:
    """The newest ``sandbox_state`` on ``leaf``'s path that carries a state.

    A failed script's entry records its calls but no state, so it is skipped: the
    sandbox after a failure is the sandbox before it.
    """
    return next(
        (e for e in _ancestry(entries, leaf) if _is_sandbox(e) and e["data"].get("state")),
        None,
    )


def state_chain(
    entries: list[dict[str, Any]], leaf: str | None, stop: str | None = None
) -> list[dict[str, Any]]:
    """Every ``sandbox_state`` entry on ``leaf``'s path after ``stop``, oldest first."""
    chain: list[dict[str, Any]] = []
    for entry in _ancestry(entries, leaf):
        if stop is not None and str(entry["id"]) == stop:
            break
        if _is_sandbox(entry):
            chain.append(entry)
    chain.reverse()
    return chain


class _Bindings(ast.NodeVisitor):
    """Collects the module-level names a script may bind; function bodies are skipped
    except for their ``global`` declarations."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.sources: dict[str, ast.AST] = {}

    def _add(self, name: str) -> None:
        if name not in self.names:
            self.names.append(name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add(node.name)
        self.sources[node.name] = node
        for inner in ast.walk(node):
            if isinstance(inner, ast.Global):
                for name in inner.names:
                    self._add(name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node.name)
        self.sources[node.name] = node

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.AST) -> None:
        return

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._add(alias.asname or alias.name)


def script_bindings(code: str) -> tuple[list[str], dict[str, str]]:
    """The module-level names ``code`` may bind, and a source digest per def/class.

    Needed because Monty 1.1.0 has no ``globals()``, ``dir()`` or ``vars()``: the
    host cannot ask the interpreter what exists, only whether a named thing does.
    """
    tree = ast.parse(code)
    visitor = _Bindings()
    visitor.visit(tree)
    digests = {
        name: hashlib.sha256(ast.unparse(node).encode()).hexdigest()[:16]
        for name, node in visitor.sources.items()
    }
    return visitor.names, digests


@dataclass
class RecordedCall:
    """One nested tool call a script made, as the tree records it."""

    name: str
    args: list[Any]
    kwargs: dict[str, Any]
    result: Any = None
    error: str | None = None
    ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape written to the entry."""
        return {
            "name": self.name,
            "args": self.args,
            "kwargs": self.kwargs,
            "result": self.result,
            "error": self.error,
            "ms": round(self.ms, 3),
        }


def _jsonable(value: Any) -> Any:
    """``value`` if it survives JSON, else its repr; a recorded result must be replayable text."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


HostTool = Callable[..., Any] | Callable[..., Awaitable[Any]]


def _recording(name: str, fn: HostTool, calls: list[RecordedCall]) -> Callable[..., Any]:
    """Wrap a host tool so each call is appended to ``calls`` with its result.

    The wrapper keeps ``fn``'s colour: Monty hands a script a future for a
    coroutine function, so an async tool needs ``await`` in the script and a sync
    one must not get it.
    """

    def begin(args: tuple[Any, ...], kwargs: dict[str, Any]) -> RecordedCall:
        record = RecordedCall(name, [_jsonable(a) for a in args], {k: _jsonable(v) for k, v in kwargs.items()})
        calls.append(record)
        return record

    if asyncio.iscoroutinefunction(fn):

        async def acall(*args: Any, **kwargs: Any) -> Any:
            record = begin(args, kwargs)
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                record.ms = (time.perf_counter() - start) * 1000
            record.result = _jsonable(result)
            return result

        return acall

    def call(*args: Any, **kwargs: Any) -> Any:
        record = begin(args, kwargs)
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.ms = (time.perf_counter() - start) * 1000
        record.result = _jsonable(result)
        return result

    return call


@dataclass
class RunResult:
    """What one script produced, and where the tree recorded it."""

    entry_id: str
    ok: bool
    value: Any
    output: str
    error: str | None
    listing: dict[str, dict[str, Any]]
    delta: dict[str, Any]
    calls: list[RecordedCall]
    timings: dict[str, float] = field(default_factory=dict)

    def model_text(self) -> str:
        """The tool result the model reads: output, value or error, and the sandbox delta."""
        parts = []
        if self.output:
            parts.append(self.output[:OUTPUT_CAP])
        if self.error:
            parts.append(f"Error: {self.error}\n(sandbox unchanged: the script's partial effects were discarded)")
        elif self.value is not None:
            parts.append(f"=> {repr(self.value)[:SHORT_REPR]}")
        line = render_delta_line(self.delta)
        if line:
            parts.append(f"[sandbox] {line}")
        return "\n".join(parts) if parts else "(no output)"


def diff_listings(
    old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Names added, removed and changed between two listings, by digest."""
    added = {n: new[n] for n in new if n not in old}
    removed = {n: old[n] for n in old if n not in new}
    changed = {
        n: (old[n], new[n]) for n in new if n in old and old[n]["digest"] != new[n]["digest"]
    }
    return {"added": added, "removed": removed, "changed": changed}


def render_delta_line(diff: dict[str, Any]) -> str:
    """One line for a tool result: ``+a:list ~b:dict -c``."""
    parts = [f"+{n}:{v['type']}" for n, v in diff["added"].items()]
    parts += [f"~{n}:{new['type']}" for n, (_, new) in diff["changed"].items()]
    parts += [f"-{n}" for n in diff["removed"]]
    return " ".join(parts)


def render_delta(diff: dict[str, Any], *, indent: str = "  ") -> list[str]:
    """Lines naming every change with a short repr, for a summary."""
    lines = []
    for name, v in diff["added"].items():
        lines.append(f"{indent}+ {name}: {v['type']} = {v['repr']}")
    for name, (old, new) in diff["changed"].items():
        lines.append(f"{indent}~ {name}: {old['type']} {old['repr']} -> {new['type']} {new['repr']}")
    for name, v in diff["removed"].items():
        lines.append(f"{indent}- {name} (was {v['type']} {v['repr']})")
    return lines


def _listing_of(entry: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return dict(entry["data"]["listing"]) if entry else {}


def sandbox_delta_text(
    entries: list[dict[str, Any]], from_leaf: str | None, to_leaf: str | None, *, kept: bool
) -> str:
    """Text for a branch summary or compaction: how the sandbox changed from one point to another.

    Args:
        entries: The tree's entries.
        from_leaf: The earlier point (a branch point, or the start of a compacted span).
        to_leaf: The later point on the same lineage (the abandoned leaf, or the
            first kept entry's parent).
        kept: Whether the reader's sandbox still holds the ``to_leaf`` state. A branch
            summary without carry is ``False``: the changes happened and are gone.

    Returns:
        ``""`` when no script ran between the two points.
    """
    start = state_entry_at(entries, from_leaf)
    stop = last_sandbox_entry_at(entries, from_leaf)
    chain = state_chain(entries, to_leaf, stop=None if stop is None else str(stop["id"]))
    if not chain:
        return ""
    end = state_entry_at(entries, to_leaf)
    diff = diff_listings(_listing_of(start), _listing_of(end))
    failed = sum(1 for e in chain if e["data"]["outcome"] != "ok")
    calls: dict[str, int] = {}
    for e in chain:
        for c in e["data"]["tool_calls"]:
            calls[c["name"]] = calls.get(c["name"], 0) + 1
    head = f"Sandbox: {len(chain)} script(s) ran"
    if failed:
        head += f" ({failed} failed, no effect)"
    if calls:
        head += "; nested calls " + ", ".join(f"{n}×{k}" for n, k in sorted(calls.items()))
    lines = [head + "."]
    body = render_delta(diff)
    if body:
        lines.append(
            "Variables now in your sandbox, relative to before:"
            if kept
            else "Those scripts changed the sandbox as follows. These changes are NOT in your "
            "sandbox now; it is as it was before them:"
        )
        lines.extend(body)
    else:
        lines.append("No variable changed.")
    return "\n".join(lines)


class MontySandbox:
    """Runs scripts in Monty for any cursor of one tree, recording state in that tree.

    The live interpreter is a cache of exactly one state entry; a run on a cursor
    whose path names a different state discards it and restores from the tree.
    Runs are serialized: two scripts racing on one cursor would both start from one
    state and chain as if one followed the other.
    """

    def __init__(
        self,
        pool: monty.AsyncMonty,
        *,
        blobs: BlobStore | None,
        tools: dict[str, HostTool] | None = None,
        limits: monty.ResourceLimits | None = None,
    ) -> None:
        """Bind to an entered ``pool``; ``tools`` are the host functions scripts may call."""
        self.pool = pool
        self.blobs = blobs
        self.tools = dict(tools or {})
        self.limits = limits
        self._live: Any = None
        self._live_state: str | None = None
        self._lock = asyncio.Lock()

    async def _checkout(self) -> Any:
        session = self.pool.checkout(limits=self.limits)
        await session.__aenter__()
        return session

    async def _drop_live(self) -> None:
        if self._live is not None:
            await self._live.__aexit__(None, None, None)
        self._live = None
        self._live_state = None

    async def close(self) -> None:
        """Return the live interpreter to the pool."""
        await self._drop_live()

    async def restore(self, entries: list[dict[str, Any]], leaf: str | None) -> tuple[Any, str | None]:
        """A fresh interpreter holding the state at ``leaf``, and that state's entry id.

        The caller owns the returned session. ``None`` as the id is the empty sandbox,
        which is the true state of a path no script has run on.
        """
        entry = state_entry_at(entries, leaf)
        session = await self._checkout()
        if entry is not None:
            await session.load_session(decode_state(entry["data"]["state"], self.blobs))
        return session, None if entry is None else str(entry["id"])

    async def _session_for(self, entries: list[dict[str, Any]], leaf: str | None) -> tuple[Any, float]:
        want = state_entry_at(entries, leaf)
        want_id = None if want is None else str(want["id"])
        if self._live is not None and self._live_state == want_id:
            return self._live, 0.0
        start = time.perf_counter()
        await self._drop_live()
        self._live, self._live_state = await self.restore(entries, leaf)
        return self._live, (time.perf_counter() - start) * 1000

    async def _listing(
        self, session: Any, names: list[str], digests: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        listing: dict[str, dict[str, Any]] = {}
        for name in names:
            try:
                type_name, full = await session.feed_run(f"(type({name}).__name__, repr({name}))")
            except monty.MontyRuntimeError as exc:
                if "NameError" in str(exc):
                    continue
                listing[name] = {"type": "?", "repr": f"<unreadable: {exc}>"[:SHORT_REPR], "digest": "?", "size": 0}
                continue
            digest = digests.get(name) or hashlib.sha256(full.encode()).hexdigest()[:16]
            listing[name] = {
                "type": type_name,
                "repr": full if len(full) <= SHORT_REPR else full[: SHORT_REPR - 1] + "…",
                "digest": digest,
                "size": len(full),
            }
        return listing

    async def run(self, cursor: Any, code: str) -> RunResult:
        """Run ``code`` against the state on ``cursor``'s path and record the result there.

        On success the entry carries the new state. On failure it carries the calls
        the script made and ``state: None``, and the live interpreter is discarded,
        because Monty keeps a failed script's partial assignments.

        Raises:
            SyntaxError: ``code`` does not parse; nothing is written.
            SandboxError: the path's state cannot be restored.
        """
        names, new_digests = script_bindings(code)
        async with self._lock:
            timings: dict[str, float] = {}
            entries = cursor.entries()
            prev_any = last_sandbox_entry_at(entries, cursor.leaf)
            prev_state = state_entry_at(entries, cursor.leaf)
            old_listing = _listing_of(prev_state)
            session, timings["restore_ms"] = await self._session_for(entries, cursor.leaf)

            calls: list[RecordedCall] = []
            lookup = {n: _recording(n, fn, calls) for n, fn in self.tools.items()}
            out = monty.CollectString(max_bytes=OUTPUT_CAP * 4)
            start = time.perf_counter()
            value: Any = None
            error: str | None = None
            try:
                value = await session.feed_run(code, external_lookup=lookup, print_callback=out)
            except (monty.MontyRuntimeError, monty.MontySyntaxError, monty.MontyCrashedError) as exc:
                error = str(exc)
            timings["run_ms"] = (time.perf_counter() - start) * 1000
            output = out.output

            data: dict[str, Any] = {
                "v": FORMAT_VERSION,
                "generation": (prev_any["data"]["generation"] + 1) if prev_any else 1,
                "parent_state": None if prev_state is None else str(prev_state["id"]),
                "script": code,
                "script_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "outcome": "error" if error else "ok",
                "error": error,
                "output": output[:OUTPUT_CAP],
                "tool_calls": [c.as_dict() for c in calls],
            }
            if error:
                await self._drop_live()
                data["state"] = None
                listing = old_listing
            else:
                start = time.perf_counter()
                carried = {n: v for n, v in old_listing.items() if v["type"] in ("function", "type")}
                digests = {n: v["digest"] for n, v in carried.items()}
                digests.update(new_digests)
                probe_names = list(dict.fromkeys([*old_listing, *names]))
                listing = await self._listing(session, probe_names, digests)
                timings["listing_ms"] = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                raw = await session.dump()
                timings["dump_ms"] = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                data["state"] = encode_state(raw, self.blobs)
                timings["encode_ms"] = (time.perf_counter() - start) * 1000
                data["listing"] = listing
            start = time.perf_counter()
            entry_id = await cursor.append_custom_entry(CUSTOM_TYPE, data)
            timings["append_ms"] = (time.perf_counter() - start) * 1000
            if not error:
                self._live_state = entry_id
            return RunResult(
                entry_id=entry_id,
                ok=error is None,
                value=_jsonable(value),
                output=output,
                error=error,
                listing=listing,
                delta=diff_listings(old_listing, listing),
                calls=calls,
                timings=timings,
            )

async def carry_state(cursor: Any, source_leaf: str) -> str:
    """Write, at ``cursor``'s leaf, a state entry that adopts the state at ``source_leaf``.

    What a branch summary does when the summarized branch's sandbox should survive:
    the summary explains the change and this entry makes it true. The state ref is
    copied, so an inline dump is written twice and a side file is shared.

    Raises:
        SandboxError: ``source_leaf`` has no sandbox state to carry.
    """
    entries = cursor.entries()
    source = state_entry_at(entries, source_leaf)
    if source is None:
        raise SandboxError(f"no sandbox state on the path to {source_leaf!r}")
    prev_any = last_sandbox_entry_at(entries, cursor.leaf)
    prev_state = state_entry_at(entries, cursor.leaf)
    data = {
        "v": FORMAT_VERSION,
        "generation": (prev_any["data"]["generation"] + 1) if prev_any else 1,
        "parent_state": None if prev_state is None else str(prev_state["id"]),
        "carried_from": str(source["id"]),
        "script": None,
        "script_sha256": None,
        "outcome": "ok",
        "error": None,
        "output": "",
        "tool_calls": [],
        "state": source["data"]["state"],
        "listing": source["data"]["listing"],
    }
    return str(await cursor.append_custom_entry(CUSTOM_TYPE, data))


async def summarize_branch_with_sandbox(
    cursor: Any, target_id: str, model: Any, *, carry: bool, api_key: str | None = None
) -> tuple[str, dict[str, int]]:
    """``tree_ops.summarize_and_navigate`` with the sandbox delta attached to the summary.

    The LLM summarizes the branch text as today; the delta is computed from the
    state entries, so it is exact and costs no tokens to produce. With ``carry`` the
    branch's final sandbox is adopted after the summary; without it the summary says
    the changes are gone.

    Returns:
        The ``branch_summary`` entry's id and the summarizer's usage.
    """
    from tau_agent_core.session_manager import summarize_branch

    abandoned = cursor.leaf
    entries = cursor.entries()
    summary, usage = await summarize_branch(
        cursor.tree().subtree_text(target_id), model, api_key=api_key
    )
    delta = sandbox_delta_text(entries, target_id, abandoned, kept=carry)
    if delta:
        summary = f"{summary}\n\n{delta}"
    entry_id = await cursor.append_branch_summary(summary, target_id)
    if carry and delta:
        await carry_state(cursor, abandoned)
    return str(entry_id), usage


def make_python_tool(sandbox: MontySandbox) -> Any:
    """A τ ``AgentTool`` named ``python`` that runs a script on the turn's own cursor.

    Sequential, because the sandbox serializes runs and a parallel batch would
    otherwise reorder its state entries against the tool results.
    """
    from tau_agent_core.cursor import TURN_CURSOR
    from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolDefinition

    async def execute(tool_call_id: str, args: dict[str, Any], signal: Any = None) -> AgentToolResult:
        cursor = TURN_CURSOR.get()
        if cursor is None:
            raise RuntimeError("python tool ran outside a turn; it has no cursor to record on")
        result = await sandbox.run(cursor, str(args["code"]))
        return AgentToolResult(
            tool_name="python",
            content=[{"type": "text", "text": result.model_text()}],
            details={"sandbox_state": result.entry_id},
            is_error=not result.ok,
        )

    definition = ToolDefinition(
        name="python",
        label="Python",
        description="Run a Python script in a persistent sandbox. Variables persist between calls.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        execute=execute,
        execution_mode="sequential",
    )
    return AgentTool(definition=definition)
