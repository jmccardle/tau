"""Execute τ agent-model scenarios in-process and dump what storage and cursors hold.

Each scenario drives a real :class:`~tau_agent_core.agent_session.AgentSession`
over the file store the CLI builds by default
(``tau_coding_agent.store_factory.build_session_catalog`` with backend ``file``,
which returns ``tau_coding_agent.session_store.FileSessionCatalog``), rooted in a
temporary directory. The model is a scripted fake patched in at
``tau_agent_core.agent_loop.stream_simple``; nothing contacts a server.

After each step a snapshot records: every session file in the catalog (header
id, ``parent``, file name, lock sidecar), every raw line of every file (entry
id, parent id, type, status, source, and whether the line duplicates an entry
another file or an earlier line already holds), every field whose value names
another entry or session (a by-id reference), every live cursor, open
suspensions, write-lock state, and the events the head's subscription received.
Random ids are relabelled in first-seen order (``S1``, ``e01``, ``c1``) so the
output is deterministic.

Usage:
    python scripts/arch/scenarios.py [--out DIR] [--only NAME ...] [--no-render]

Outputs (in ``--out``, default ``build/arch/scenarios``):
    <scenario>.json          snapshots and events, one file per scenario
    <scenario>-<step>.dot    graph of sessions / entries / cursors at a step
    <scenario>-events.mmd    sequence diagram of the events the head received
    *.svg                    rendered with dot + svg_theme.py / seq_svg.py
    SUMMARY.md               per-scenario before/after tables
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
for _pkg in ("tau-llm", "tau-agent-core", "tau-coding-agent"):
    sys.path.insert(0, str(REPO / _pkg / "src"))

from tau_llm.streaming import DoneEvent, TextDeltaEvent  # noqa: E402
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage  # noqa: E402

from tau_agent_core.agent_session import AgentSession  # noqa: E402
from tau_agent_core.cursor import Cursor  # noqa: E402
from tau_agent_core.session_log import InMemorySessionLog  # noqa: E402
from tau_coding_agent.session_store import FileSessionCatalog  # noqa: E402
from tau_coding_agent.store_factory import build_session_catalog  # noqa: E402

ID_FIELDS_SKIP = {"id", "timestamp"}


def scen_model() -> Model:
    """The fake model every scenario's session is built with."""
    return Model(
        id="fake",
        provider="openai",
        api="openai-completions",
        base_url="http://fake.invalid/v1",
        name="fake",
        context_window=4000,
        max_tokens=256,
    )


def scen_reply(
    text: str, calls: list[ToolCall] | None = None, tokens: int = 10
) -> AssistantMessage:
    """An assistant message with ``text`` and optional tool calls."""
    return AssistantMessage(
        content=[TextContent(text=text), *(calls or [])],
        api="openai-completions",
        provider="openai",
        model="fake",
        stop_reason="toolUse" if calls else "stop",
        timestamp=1_700_000_000_000,
        usage=Usage(input_tokens=tokens, output_tokens=1, total_tokens=tokens + 1),
    )


class ScenStream:
    """A provider stream yielding one text delta then ``done``; optionally gated on an event."""

    def __init__(self, message: AssistantMessage, gate: asyncio.Event | None = None) -> None:
        """Hold the message and the optional gate the stream waits on before finishing."""
        self._message = message
        self._gate = gate

    def __aiter__(self) -> Any:
        """Yield the delta, wait for the gate, then yield ``done``."""

        async def _gen() -> Any:
            text = "".join(b.text for b in self._message.content if isinstance(b, TextContent))
            yield TextDeltaEvent(delta=text, partial=self._message)
            if self._gate is not None:
                await self._gate.wait()
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        """The final message."""
        return self._message

    def abort(self) -> None:
        """No-op; the fake has nothing to cancel."""


def scen_text(message: Any) -> str:
    """Concatenated text of a message dict, whatever its content shape."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


class ScenProvider:
    """A scripted ``stream_simple``: ``route(messages, n)`` returns a reply, a string, or a coroutine."""

    def __init__(self, route: Callable[[list[dict[str, Any]], int], Any]) -> None:
        """Keep the routing function and a log of every request's messages."""
        self.route = route
        self.requests: list[list[dict[str, Any]]] = []
        self.gates: dict[int, asyncio.Event] = {}

    async def stream(self, model: Any, context: Any, options: Any = None) -> ScenStream:
        """Answer one request by the route; a coroutine route result is awaited first."""
        messages = [m.model_dump() if hasattr(m, "model_dump") else m for m in context["messages"]]
        self.requests.append(messages)
        answer = self.route(messages, len(self.requests))
        if asyncio.iscoroutine(answer):
            answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        message = answer if isinstance(answer, AssistantMessage) else scen_reply(str(answer))
        return ScenStream(message, self.gates.get(len(self.requests)))


class ScenLabels:
    """First-seen relabelling of random ids, so two runs print the same output."""

    def __init__(self) -> None:
        """Start with empty maps for sessions, entries and cursors."""
        self.maps: dict[str, dict[str, str]] = {"S": {}, "e": {}, "c": {}, "x": {}}

    def get(self, kind: str, raw: str | None) -> str | None:
        """The stable label for ``raw`` of ``kind`` (``S``, ``e`` or ``c``), minting one if new."""
        if raw is None:
            return None
        table = self.maps[kind]
        if raw not in table:
            width = 2 if kind == "e" else 1
            table[raw] = f"{kind}{len(table) + 1:0{width}d}"
        return table[raw]

    def known(self, kind: str, raw: Any) -> str | None:
        """The label for ``raw`` if it has one, without minting."""
        return self.maps[kind].get(raw) if isinstance(raw, str) else None


def scen_canon(entry: dict[str, Any]) -> str:
    """Canonical JSON of an entry minus its timestamp, for byte comparison across files."""
    return json.dumps({k: v for k, v in entry.items() if k != "timestamp"}, sort_keys=True)


def scen_summary(entry: dict[str, Any]) -> str:
    """A one-line description of an entry's payload."""
    return " ".join(scen_summary_raw(entry).split())


def scen_summary_raw(entry: dict[str, Any]) -> str:
    """The description before whitespace is collapsed."""
    kind = entry.get("type")
    if kind == "message":
        message = entry.get("message", {})
        role = message.get("role", "?")
        calls = [
            b.get("name")
            for b in message.get("content", []) or []
            if isinstance(b, dict) and b.get("type") == "toolCall"
        ]
        text = scen_text(message)[:28]
        return f"{role}: {text}" + (f" [call {','.join(calls)}]" if calls else "")
    if kind in ("customEntry", "customMessage"):
        return f"{kind}:{entry.get('customType')}"
    if kind == "compaction":
        return f"compaction: {str(entry.get('summary'))[:24]}"
    return str(kind)


class ScenRecorder:
    """One scenario's world: a temp file store, the sessions in it, events, and snapshots."""

    def __init__(self, name: str, root: Path) -> None:
        """Create the store under ``root`` and empty event and snapshot logs."""
        self.name = name
        self.root = root
        self.base = root / "sessions"
        self.tau_home = root / "tauhome"
        self.tau_home.mkdir(parents=True, exist_ok=True)
        os.environ["TAU_HOME"] = str(self.tau_home)
        self.cwd = str(root / "work")
        os.makedirs(self.cwd, exist_ok=True)
        catalog = build_session_catalog({}, None, self.base)
        assert isinstance(catalog, FileSessionCatalog)
        self.catalog = catalog
        self.labels = ScenLabels()
        self.events: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.agents: list[AgentSession] = []
        self.notes: list[str] = []

    def watch(self, agent: AgentSession) -> AgentSession:
        """Subscribe to ``agent``'s events and cursor channel; remember it for snapshots."""
        self.agents.append(agent)

        def on_event(event: Any) -> None:
            if event.type == "message_update":
                return
            row: dict[str, Any] = {
                "type": event.type,
                "cursor": self.labels.get("c", event.cursor_id),
            }
            for field in ("tool_name", "end_reason", "close_reason", "suspension_id"):
                value = getattr(event, field, None)
                if value is not None:
                    row[field] = value
            if event.type in ("message_start", "message_end") and event.message is not None:
                message = event.message
                row["role"] = (
                    message.get("role")
                    if isinstance(message, dict)
                    else getattr(message, "role", None)
                )
            if event.type in ("suspension_open", "suspension_closed") and event.suspension:
                row["suspension_entry"] = "raw:" + str(event.suspension.get("id"))
                row["queued"] = event.suspension.get("queued")
            if event.type == "suspension_closed":
                row["action"] = (event.resolution or {}).get("action")
            self.events.append(row)

        agent.subscribe(on_event)
        for channel in ("cursor_open", "cursor_close"):
            agent.subscribe_channel(
                channel,
                lambda cursor, _c=channel: self.events.append(
                    {"type": _c, "cursor": self.labels.get("c", cursor.id), "label": cursor.label}
                ),
            )
        return agent

    def new_agent(
        self, log: Any, extensions: list[Callable] | None = None, **kw: Any
    ) -> AgentSession:
        """Build an AgentSession the way the heads do: scratch in-memory log, then bind a cursor at ``log``'s default leaf."""
        kw.setdefault("tools", [])
        agent = AgentSession(
            session_log=InMemorySessionLog(),
            model=scen_model(),
            system_prompt="SYS",
            api_key="k",
            extensions=extensions or [],
            cwd=self.cwd,
            **kw,
        )
        agent.cursor = Cursor.newest(log)
        return self.watch(agent)

    def files(self) -> list[Path]:
        """Every session ``.jsonl`` under the store, ordered by header timestamp then name."""
        found = [p for d in (self.base, self.tau_home) if d.exists() for p in d.rglob("*.jsonl")]
        return sorted(
            found, key=lambda p: (json.loads(p.read_text().split("\n", 1)[0])["timestamp"], p.name)
        )

    def where(self, path: Path, session_label: str) -> str:
        """``path`` as ``<store>/<cwd-dir>/<ts>_<label>.jsonl``: which root holds it, ids and time masked."""
        store = "--session-dir" if self.base in path.parents else "$TAU_HOME/sessions"
        return f"{store}/<cwd-dir>/<ts>_{session_label}.jsonl"

    def snapshot(self, step: str, note: str = "") -> dict[str, Any]:
        """Record storage, cursors, suspensions, locks and the event count at ``step``."""
        sessions = []
        first_holder: dict[str, tuple[str, int, str]] = {}
        all_entry_ids: set[str] = set()
        raw_files = []
        for path in self.files():
            lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
            raw_files.append((path, lines))
            all_entry_ids.update(str(e["id"]) for e in lines[1:] if "id" in e)
        session_ids = {str(lines[0]["id"]) for _p, lines in raw_files}
        latest: dict[str, dict[str, tuple[int, str]]] = {}
        for _path, lines in raw_files:
            held = latest.setdefault(str(lines[0]["id"]), {})
            for number, entry in enumerate(lines[1:], start=2):
                held[str(entry.get("id"))] = (number, scen_canon(entry))
        for path, lines in raw_files:
            header = lines[0]
            slabel = self.labels.get("S", str(header["id"]))
            parent_raw = header.get("parent")
            rows = []
            seen_here: dict[str, tuple[int, str]] = {}
            for number, entry in enumerate(lines[1:], start=2):
                raw_id = str(entry.get("id"))
                elabel = self.labels.get("e", raw_id)
                canon = scen_canon(entry)
                storage = "original"
                if raw_id in seen_here:
                    line_no, held_canon = seen_here[raw_id]
                    same = "byte-identical" if held_canon == canon else "same id, payload differs"
                    storage = f"rewrite of line {line_no} ({same})"
                elif parent_raw in latest and raw_id in latest[parent_raw]:
                    line_no, held_canon = latest[parent_raw][raw_id]
                    same = "byte-identical" if held_canon == canon else "same id, payload differs"
                    storage = f"copy of {self.labels.get('S', parent_raw)}:{line_no} ({same})"
                elif raw_id in first_holder:
                    holder, line_no, held_canon = first_holder[raw_id]
                    same = "byte-identical" if held_canon == canon else "same id, payload differs"
                    storage = f"same id as {holder}:{line_no} ({same})"
                else:
                    first_holder[raw_id] = (slabel, number, canon)
                seen_here[raw_id] = (number, canon)
                refs = []
                for key, value in sorted(entry.items()):
                    if key in ID_FIELDS_SKIP or key == "parentId" or not isinstance(value, str):
                        continue
                    if value in all_entry_ids:
                        refs.append({"field": key, "entry": self.labels.get("e", value)})
                    elif value in session_ids:
                        refs.append({"field": key, "session": self.labels.get("S", value)})
                rows.append(
                    {
                        "line": number,
                        "id": elabel,
                        "parentId": self.labels.known("e", entry.get("parentId"))
                        or entry.get("parentId"),
                        "type": entry.get("type"),
                        "status": entry.get("status"),
                        "source": entry.get("source"),
                        "summary": scen_summary(entry),
                        "storage": storage,
                        "refs": refs,
                    }
                )
            lock = path.with_name(path.name + ".lock")
            sessions.append(
                {
                    "session": slabel,
                    "file": self.where(path, slabel),
                    "parent": self.labels.known("S", header.get("parent")) or header.get("parent"),
                    "lock_sidecar": lock.exists(),
                    "lock_holder_fields": scen_lock_fields(lock),
                    "lines": rows,
                }
            )
        cursors = []
        suspensions = []
        locks = []
        for index, agent in enumerate(self.agents):
            for cursor in agent.cursors:
                cursors.append(
                    {
                        "agent": index,
                        "cursor": self.labels.get("c", cursor.id),
                        "session": self.labels.get("S", cursor.session_id),
                        "leaf": self.labels.known("e", cursor.leaf) or cursor.leaf,
                        "opened_at": self.labels.known("e", cursor.opened_at) or cursor.opened_at,
                        "owner": self.labels.known("c", cursor.owner.id) if cursor.owner else None,
                        "label": cursor.label,
                        "head": cursor is agent.cursor,
                        "busy": cursor.busy,
                        "streaming": cursor.is_streaming,
                        "frame_tools": list(cursor.frame.tools) if cursor.frame else None,
                    }
                )
            for pending in agent.pending_suspensions(scope="session"):
                suspensions.append({"agent": index, **scen_suspension_row(self, pending)})
            access = agent.write_access
            locks.append(
                {
                    "agent": index,
                    "read_only": access.read_only,
                    "held_trees": len(agent._write_locks),
                }
            )
        snap = {
            "step": step,
            "note": note,
            "sessions": sessions,
            "cursors": cursors,
            "suspensions": suspensions,
            "locks": locks,
            "events_so_far": len(self.events),
        }
        self.snapshots.append(snap)
        return snap


def scen_mask(rec: ScenRecorder, text: str | None) -> str | None:
    """``text`` with host name, pids, times and the temp root masked."""
    import re
    import socket

    if text is None:
        return None
    dashed = str(rec.root).lstrip("/").replace("/", "-")
    text = text.replace(str(rec.root), "<root>").replace(dashed, "<root>")
    text = text.replace(socket.gethostname(), "<host>")
    text = re.sub(r"pid \d+", "pid <pid>", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[0-9:.-]+Z?", "<time>", text)
    return re.sub(r"[0-9a-f]{32}", "<id>", text)


def scen_lock_fields(lock: Path) -> dict[str, Any] | None:
    """The lock sidecar's holder record with host, pid and time masked; ``None`` when empty or absent."""
    if not lock.exists() or not lock.stat().st_size:
        return None
    try:
        held = json.loads(lock.read_text())
    except json.JSONDecodeError:
        return {"unparsed": True}
    return {
        k: ("<masked>" if k in ("pid", "host", "started") else v) for k, v in sorted(held.items())
    }


def scen_suspension_row(rec: ScenRecorder, state: dict[str, Any]) -> dict[str, Any]:
    """A pending suspension's ``state()`` fields, ids relabelled; its id is its side entry's id."""
    call = state.get("call") or {}
    return {
        "suspension_entry": rec.labels.get("e", str(state.get("id"))),
        "cursor": rec.labels.get("c", state.get("cursor_id")),
        "parent_cursor": rec.labels.known("c", state.get("parent_cursor_id")),
        "site": state.get("site"),
        "queued": state.get("queued"),
        "call_args": call.get("effective_args"),
        "actions": [a.get("label") for a in (state.get("spec") or {}).get("actions", [])],
    }


def scen_dot(rec: ScenRecorder, snap: dict[str, Any]) -> str:
    """Graphviz DOT of one snapshot: a cluster per session file, entries, cursors, copy and reference edges."""
    out = [
        "digraph G {",
        "  rankdir=TB; node [shape=box, fontsize=10]; edge [fontsize=9];",
    ]
    position: dict[str, str] = {}
    for sess in snap["sessions"]:
        s = sess["session"]
        out.append(f"  subgraph cluster_{s} {{")
        label = f"{s} {sess['file'].split('/')[-1][-14:]}" + (
            f" parent={sess['parent']}" if sess["parent"] else ""
        )
        out.append(f'    label="{label}"; class="durable";')
        for row in sess["lines"]:
            node = f"{s}_{row['id']}_{row['line']}"
            muted = (
                row["storage"].startswith("rewrite")
                or row["status"] == "incomplete"
                or row["type"] == "suspension"
            )
            text = f"{row['id']} {row['summary']}".replace('"', "'")
            if row["status"]:
                text += f" ({row['status']})"
            if row["source"]:
                text += f" src={row['source']}"
            attrs = ' class="muted"' if muted else ""
            out.append(f'    {node} [label="{text}"{attrs}];')
            position.setdefault(f"{s}:{row['id']}", node)
            if row["storage"].startswith("rewrite"):
                position[f"{s}:{row['id']}"] = node
        out.append("  }")
    for sess in snap["sessions"]:
        s = sess["session"]
        for row in sess["lines"]:
            node = f"{s}_{row['id']}_{row['line']}"
            if row["parentId"] and f"{s}:{row['parentId']}" in position:
                out.append(f"  {position[f'{s}:{row["parentId"]}']} -> {node};")
            if row["storage"].startswith("copy of"):
                src = row["storage"].split()[2].split(":")[0]
                if f"{src}:{row['id']}" in position:
                    out.append(
                        f'  {position[f"{src}:{row['id']}"]} -> {node} [label="copy", constraint=false];'
                    )
            if row["storage"].startswith("rewrite"):
                prior = f"{s}_{row['id']}_{row['storage'].split()[3]}"
                out.append(
                    f'  {prior} -> {node} [label="finalized by", class="soft", constraint=false];'
                )
            for ref in row["refs"]:
                if ref["field"] == "parentId":
                    continue
                if "entry" in ref and f"{s}:{ref['entry']}" in position:
                    out.append(
                        f'  {node} -> {position[f"{s}:{ref['entry']}"]} [label="{ref["field"]}", class="lazy", constraint=false];'
                    )
        if sess["parent"]:
            first = f"{s}_{sess['lines'][0]['id']}_2" if sess["lines"] else None
            parents = [x for x in snap["sessions"] if x["session"] == sess["parent"]]
            if first and parents and parents[0]["lines"]:
                p = parents[0]
                out.append(
                    f'  {p["session"]}_{p["lines"][0]["id"]}_2 -> {first} [label="header parent", class="lazy", ltail=cluster_{p["session"]}, lhead=cluster_{s}, constraint=false];'
                )
    for cur in snap["cursors"]:
        name = f"cur_{cur['cursor']}"
        tag = "HEAD " if cur["head"] else ""
        extra = " busy" if cur["busy"] else ""
        owner = f" owner={cur['owner']}" if cur["owner"] else ""
        cls = ' class="mark"' if cur["head"] else ""
        out.append(f'  {name} [shape=hexagon, label="{tag}{cur["cursor"]}{owner}{extra}"{cls}];')
        target = position.get(f"{cur['session']}:{cur['leaf']}")
        if target:
            out.append(f'  {name} -> {target} [label="leaf", class="soft"];')
    out.append("}")
    return "\n".join(out) + "\n"


def scen_mmd(rec: ScenRecorder) -> str:
    """Mermaid sequence diagram (renderer subset) of the events the head's subscription received."""
    lines = [
        "sequenceDiagram",
        "participant Bus as AgentSession event bus",
        "participant Head as head subscriber",
    ]
    cursors = sorted({e["cursor"] for e in rec.events if e.get("cursor")})
    for cur in cursors:
        lines.append(f"participant {cur} as cursor {cur}")
    for event in rec.events:
        source = event.get("cursor") or "Bus"
        detail = " ".join(f"{k}={v}" for k, v in event.items() if k not in ("type", "cursor"))
        lines.append(f"{source}->>Head: {event['type']} {detail}".rstrip())
    return "\n".join(lines) + "\n"


def scen_table(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Markdown before/after counts of sessions, lines, cursors and suspensions."""

    def count(snap: dict[str, Any]) -> dict[str, str]:
        return {
            "session files": str(len(snap["sessions"])),
            "lines per file": ", ".join(
                f"{s['session']}={len(s['lines'])}" for s in snap["sessions"]
            ),
            "copied lines": str(
                sum(
                    1
                    for s in snap["sessions"]
                    for r in s["lines"]
                    if r["storage"].startswith("copy")
                )
            ),
            "finalize rewrites": str(
                sum(
                    1
                    for s in snap["sessions"]
                    for r in s["lines"]
                    if r["storage"].startswith("rewrite")
                )
            ),
            "live cursors": ", ".join(
                f"{c['cursor']}@{c['session']}:{c['leaf']}{'(head)' if c['head'] else ''}"
                for c in snap["cursors"]
            ),
            "open suspensions": str(len(snap["suspensions"])),
        }

    b, a = count(before), count(after)
    rows = [f"| | {before['step']} | {after['step']} |", "|---|---|---|"]
    rows += [f"| {k} | {b[k]} | {a[k]} |" for k in b]
    return rows


async def scen_spawn(rec: ScenRecorder) -> None:
    """A tool calls ``ctx.spawn_branch`` with a prompt; snapshots before, during the child's request, and after."""
    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")

    async def delegate(
        tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
    ) -> dict[str, Any]:
        rec.snapshot("in-tool-before-spawn", "the parent's delegate tool is executing")
        parent = next(e["parentId"] for e in ctx.entries() if e["id"] == ctx.cursor.leaf)
        result = await ctx.spawn_branch(parent, "child task: count files", tools=[])
        rec.snapshot(
            "in-tool-after-spawn", f"BranchResult ok={result.ok} end_reason={result.end_reason}"
        )
        return {
            "content": [{"type": "text", "text": f"child said: {scen_text(result.messages[-1])}"}]
        }

    def factory(api: Any) -> None:
        api.register_tool(
            {
                "name": "delegate",
                "description": "Hand a task to a sub-agent.",
                "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
                "execute": delegate,
            }
        )

    agent = rec.new_agent(log, [factory])

    async def route(messages: list[dict[str, Any]], n: int) -> Any:
        if any("child task" in scen_text(m) for m in messages):
            rec.snapshot("during-child-request", "the fake provider received the child's request")
            return "child: 3 files"
        if messages[-1].get("role") == "toolResult":
            return "parent: done"
        return scen_reply("", [ToolCall(id="callP", name="delegate", arguments={"task": "count"})])

    provider = ScenProvider(route)
    rec.snapshot("before")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await agent.prompt("please delegate")
    rec.snapshot("after")
    rec.notes.append(
        "requests: " + json.dumps([[m.get("role") for m in r] for r in provider.requests])
    )
    rec.notes.append(
        "head context roles after: " + json.dumps([m.get("role") for m in agent.cursor.context()])
    )
    reloaded = rec.catalog.load(str(rec.files()[0]))
    fresh = Cursor.newest(reloaded)
    rec.notes.append(f"reload default leaf: {rec.labels.known('e', fresh.leaf)}")


async def scen_concurrent(rec: ScenRecorder) -> None:
    """Two cursors on one session run turns at once; the head's stream is held open while a second cursor's turn completes."""
    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
    agent = rec.new_agent(log)
    await agent.cursor.append_message(
        {"role": "user", "content": [{"type": "text", "text": "shared"}]}
    )
    await agent.cursor.append_message(scen_reply("ok").model_dump())
    gate = asyncio.Event()
    entered = asyncio.Event()

    def route(messages: list[dict[str, Any]], n: int) -> Any:
        last = scen_text(messages[-1])
        if "head question" in last:
            entered.set()
            return "head answer"
        return "side answer"

    provider = ScenProvider(route)
    provider.gates[1] = gate
    rec.snapshot("before")
    second = await agent.open_cursor(agent.cursor.leaf, label="background")
    from tau_agent_core.submission import Submission

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        head_task = asyncio.create_task(agent.prompt("head question"))
        await entered.wait()
        await asyncio.sleep(0)
        rec.snapshot("head-streaming", "head's provider stream is held open by a gate")
        side = Submission(
            text="side question", source="user", submitter="scen", submission_id="side"
        )
        side_result = await agent.submit(side, cursor=second)
        rec.snapshot("side-finished", f"second cursor's submit accepted={side_result.accepted}")
        gate.set()
        await head_task
    rec.snapshot("after")
    await agent.close_cursor(second)
    rec.snapshot("after-close")


async def scen_resume(rec: ScenRecorder) -> None:
    """A turn is cut off mid-stream (task cancelled, process exit stand-in); the file is reloaded by a second session object."""
    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
    agent = rec.new_agent(log)
    gate = asyncio.Event()
    entered = asyncio.Event()

    def route(messages: list[dict[str, Any]], n: int) -> Any:
        if n == 1:
            entered.set()
            return "partial answer that never finishes"
        return "retry answer"

    provider = ScenProvider(route)
    provider.gates[1] = gate
    rec.snapshot("before")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        task = asyncio.create_task(agent.prompt("first question"))
        await entered.wait()
        await asyncio.sleep(0)
        rec.snapshot("mid-stream", "provider stream held open")
        task.cancel()
        try:
            await task
        except BaseException as exc:  # noqa: BLE001 - recording how the turn unwound
            rec.notes.append(f"cancelled turn raised {type(exc).__name__}")
        rec.snapshot("after-cancel", "turn task cancelled; same process")
        agent.release_write_locks()
        rec.agents.clear()
        reloaded = rec.catalog.load(str(rec.files()[0]))
        agent2 = rec.new_agent(reloaded)
        rec.snapshot("reloaded", "new Session.load + AgentSession on the same file")
        await agent2.prompt("second question")
    rec.snapshot("after-retry")
    rec.notes.append(
        "retry request roles: " + json.dumps([m.get("role") for m in provider.requests[-1]])
    )
    rec.notes.append(
        "retry request texts: " + json.dumps([scen_text(m)[:30] for m in provider.requests[-1]])
    )


def scen_tool(name: str, ran: list[str]) -> Any:
    """A built-in-style ``AgentTool`` named ``name`` that records each run in ``ran``."""
    from tau_llm.tools import ToolDefinition

    from tau_agent_core.tools.base import AgentTool

    async def _execute(**kw: Any) -> str:
        ran.append(name)
        return f"{name} ok"

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def scen_step_mode() -> Callable[[Any], None]:
    """The ``register`` function of ``examples/80_step_mode.py``, loaded by path as ``tau -e`` does."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "scen_step_mode", REPO / "examples" / "80_step_mode.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.register


def scen_child(kind: str, root: Path) -> None:
    """Child-process body: build a session under ``root``, reach a pause point, then ``os._exit`` or hold.

    ``resume``: exit while the provider stream is open. ``debugger``: exit while a
    step-mode suspension is open. ``holder``: keep the session's write lock until
    stdin closes.
    """

    async def body() -> None:
        rec = ScenRecorder(kind, root)
        log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
        hold = asyncio.Event()
        ran: list[str] = []
        if kind == "debugger":
            agent = rec.new_agent(log, [scen_step_mode()], tools=[scen_tool("probe", ran)])
            agent.add_suspension_responder("scen")
        else:
            agent = rec.new_agent(log)

        def ready() -> None:
            print(f"READY {log.path}", flush=True)
            if kind != "holder":
                os._exit(0)

        def route(messages: list[dict[str, Any]], n: int) -> Any:
            if kind == "debugger":
                return scen_reply("", [ToolCall(id="call1", name="probe", arguments={})])
            asyncio.get_running_loop().call_later(0.3, ready)
            return "never"

        agent.subscribe(lambda e: ready() if e.type == "suspension_open" else None)
        provider = ScenProvider(route)
        if kind == "resume":
            provider.gates[1] = hold
        if kind == "holder":
            await agent.cursor.append_message(
                {"role": "user", "content": [{"type": "text", "text": "held"}]}
            )
            ready()
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
            agent.release_write_locks()
            return
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
            await agent.prompt("first question")

    asyncio.run(body())


def scen_spawn_child(rec: ScenRecorder, kind: str) -> tuple[subprocess.Popen[str], Path]:
    """Start ``scen_child(kind)`` on ``rec``'s store; return the process and the session file it reported."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--scen-child", kind, str(rec.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "TAU_HOME": str(rec.tau_home)},
    )
    assert proc.stdout is not None
    import selectors

    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        if not sel.select(timeout=20):
            proc.kill()
            raise RuntimeError(f"child {kind} printed nothing within 20s")
    line = proc.stdout.readline().strip()
    if not line.startswith("READY "):
        proc.kill()
        raise RuntimeError(f"child {kind} did not reach its pause point: {line!r}")
    return proc, Path(line.split(" ", 1)[1])


async def scen_resume_hard_exit(rec: ScenRecorder) -> None:
    """A separate process exits (``os._exit``) while its provider stream is open; this process reloads the file."""
    proc, path = scen_spawn_child(rec, "resume")
    proc.wait()
    rec.snapshot(
        "after-child-exit", f"child exit code {proc.returncode}; no agent in this process yet"
    )
    loaded = rec.catalog.load(str(path))
    provider = ScenProvider(lambda messages, n: "retry answer")
    agent = rec.new_agent(loaded)
    rec.notes.append(f"write_access after reload: read_only={agent.write_access.read_only}")
    rec.snapshot("reloaded", "Session.load + AgentSession bound at Cursor.newest")
    rec.notes.append(
        "context at reload: " + json.dumps([m.get("role") for m in agent.cursor.context()])
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await agent.prompt("second question")
    rec.snapshot("after-retry")
    rec.notes.append(
        "retry request texts: " + json.dumps([scen_text(m)[:30] for m in provider.requests[-1]])
    )


async def scen_fork(rec: ScenRecorder) -> None:
    """Build a session with an annotation, an image, a name, a compaction and a side branch; fork it every way."""
    from tau_agent_core import tree_ops
    from tau_agent_core.agent_session_runtime import AgentSessionRuntime
    from tau_agent_core.compaction import CompactionSettings
    from tau_agent_core.submission import Submission

    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
    agent = rec.new_agent(log, compaction_settings=CompactionSettings(keep_recent_tokens=1))
    cur = agent.cursor
    image = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
    await cur.append_message(
        {"role": "user", "content": [{"type": "text", "text": "q1 with image"}, image]},
        ext={"scen": {"tag": "annotated"}},
    )
    branch_point = await cur.append_message(scen_reply("a1").model_dump())
    provider = ScenProvider(lambda messages, n: f"answer {n}")
    with ExitStack() as stack:
        stack.enter_context(
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream)
        )
        stack.enter_context(
            patch("tau_agent_core.compaction.complete_simple", side_effect=scen_fake_summary)
        )
        await agent.prompt("q2")
        await agent.set_session_name("main-name")
        await agent.compact()
        await agent.prompt("q3 after compaction")
        main_leaf = cur.leaf
        cur.move(branch_point)
        await cur.append_message({"role": "user", "content": [{"type": "text", "text": "side q"}]})
        await agent.set_session_name("renamed-on-side")
        cur.move(main_leaf)
        rec.snapshot("before", "S1 holds main path + a side branch; head cursor on main leaf")

        runtime = AgentSessionRuntime(agent, rec.catalog, rec.cwd, "fake", "openai", "file")
        result = await runtime.fork()
        rec.snapshot("runtime-fork", "AgentSessionRuntime.fork (TUI /fork, REPL /fork, RPC fork)")
        forked = result["session"]
        rec.notes.append(f"fork session name: {forked.name!r}; source name: {log.name!r}")
        rec.notes.append(
            "fork context roles: " + json.dumps([m.get("role") for m in agent.cursor.context()])
        )
        ctx = agent._extension_api.context
        exported = await ctx.fork(entry_id=branch_point, mode="export")
        rec.snapshot(
            "ctx-fork-export", "ctx.fork(mode='export') at the branch point, from the fork"
        )
        rec.notes.append(
            f"ctx.fork export landed under: {rec.where(Path(exported), '?').split('/')[0]}"
        )
        await ctx.fork(entry_id=branch_point, mode="in_place")
        rec.snapshot(
            "ctx-fork-in-place", "ctx.fork(mode='in_place'): head cursor moved, nothing written"
        )
        await agent.prompt("q4 in place")
        rec.snapshot("after-in-place-turn")
        head = agent.cursor
        path_ids = [e["id"] for e in head.tree().path() if e.get("type") == "message"]
        await tree_ops.commit_branch(head, path_ids[-2:], drop_context=True)
        rec.snapshot(
            "commit-branch", "tree_ops.commit_branch(last two path messages, drop_context=True)"
        )
        users = [
            e["id"]
            for e in head.entries()
            if e.get("type") == "message" and e["message"].get("role") == "user"
        ]
        await tree_ops.paste_subtree(head, users[-1], users[0])
        rec.snapshot("paste-subtree", "tree_ops.paste_subtree(newest user entry under oldest)")
        fork_sub = Submission(
            text="forked q",
            source="user",
            submitter="scen",
            submission_id="fork1",
            multitask_strategy="fork",
        )
        accepted = await agent.submit(fork_sub)
        rec.notes.append(
            f"multitask fork accepted={accepted.accepted} reason={accepted.rejection_reason}"
        )
        await asyncio.sleep(0.2)
        rec.snapshot("multitask-fork", "submit(multitask_strategy='fork') on the idle head")


async def scen_fake_summary(
    model: Any, context: Any, options: Any = None, **_: Any
) -> AssistantMessage:
    """The fake summarizer patched in at ``tau_agent_core.compaction.complete_simple``."""
    return scen_reply("## Goal\nSUMMARY-TEXT")


async def scen_compaction(rec: ScenRecorder) -> None:
    """Three exchanges, ``AgentSession.compact()``, then one more turn; records what the model is sent."""
    from tau_agent_core.compaction import CompactionSettings

    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
    agent = rec.new_agent(log, compaction_settings=CompactionSettings(keep_recent_tokens=1))
    provider = ScenProvider(lambda messages, n: f"answer {n}")
    summaries: list[Any] = []

    async def summarizer(
        model: Any, context: Any, options: Any = None, **_: Any
    ) -> AssistantMessage:
        summaries.append(context)
        return scen_reply("## Goal\nSUMMARY-TEXT")

    with ExitStack() as stack:
        stack.enter_context(
            patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream)
        )
        stack.enter_context(
            patch("tau_agent_core.compaction.complete_simple", side_effect=summarizer)
        )
        for q in ("q1", "q2", "q3"):
            await agent.prompt(q)
        rec.snapshot("before")
        rec.notes.append(
            "request before compaction: "
            + json.dumps([scen_text(m)[:24] for m in provider.requests[-1]])
        )
        result = await agent.compact()
        rec.snapshot(
            "after-compact",
            f"CompactionResult first_kept={rec.labels.known('e', getattr(result, 'first_kept_entry_id', None))}",
        )
        await agent.prompt("q4")
        rec.snapshot("after-next-turn")
    rec.notes.append(
        "request after compaction: "
        + json.dumps([scen_text(m)[:40] for m in provider.requests[-1]])
    )
    rec.notes.append(
        "request roles after compaction: "
        + json.dumps([m.get("role") for m in provider.requests[-1]])
    )
    rec.notes.append(f"summarizer calls: {len(summaries)}")


async def scen_debugger(rec: ScenRecorder) -> None:
    """examples/80_step_mode.py over a two-call turn: step, skip, then abort at the next turn; then shutdown while paused."""
    ran: list[str] = []
    log = rec.catalog.create(rec.cwd, "fake", "openai", system_prompt="SYS")
    agent = rec.new_agent(
        log, [scen_step_mode()], tools=[scen_tool("probe", ran), scen_tool("write_x", ran)]
    )
    agent.add_suspension_responder("scen")
    opened: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    agent.subscribe(
        lambda e: (
            opened.put_nowait(e.suspension)
            if e.type == "suspension_open" and not e.suspension["queued"]
            else None
        )
    )

    def route(messages: list[dict[str, Any]], n: int) -> Any:
        if messages[-1].get("role") == "toolResult" and n in (2, 4):
            return f"done {n}"
        calls = [
            ToolCall(id=f"c{n}a", name="probe", arguments={}),
            ToolCall(id=f"c{n}b", name="write_x", arguments={}),
        ]
        return scen_reply("", calls if n == 1 else calls[:1])

    provider = ScenProvider(route)
    rec.snapshot("before")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        turn = asyncio.create_task(agent.prompt("go"))
        for action in ("step", "skip"):
            state = await asyncio.wait_for(opened.get(), 5)
            rec.snapshot(f"paused-before-{action}", f"open suspension on call {state.get('call')}")
            answer = agent.answer_suspension(state["id"], action)
            rec.notes.append(f"answer {action}: accepted={answer.get('accepted')}")
            late = agent.answer_suspension(state["id"], action)
            rec.notes.append(
                f"second answer to the same id: accepted={late.get('accepted')} reason={late.get('reason')}"
            )
            await asyncio.sleep(0)
        await asyncio.wait_for(turn, 5)
        rec.snapshot("after-turn-1", f"tools run: {ran}")
        turn = asyncio.create_task(agent.prompt("again"))
        state = await asyncio.wait_for(opened.get(), 5)
        rec.snapshot("paused-before-abort")
        agent.answer_suspension(state["id"], "abort")
        await asyncio.wait_for(turn, 5)
        rec.snapshot("after-abort", f"tools run: {ran}")
        turn = asyncio.create_task(agent.prompt("third"))
        state = await asyncio.wait_for(opened.get(), 5)
        rec.snapshot("paused-before-shutdown")
        await agent.emit_session_shutdown()
        await asyncio.wait_for(turn, 5)
        rec.snapshot("after-shutdown", "emit_session_shutdown while paused")
    rec.agents.clear()
    proc, path = scen_spawn_child(rec, "debugger")
    proc.wait()
    rec.snapshot(
        "after-child-exit-while-paused", "a second process paused at a step, then os._exit"
    )
    loaded = rec.catalog.load(str(path))
    agent2 = rec.new_agent(loaded, [scen_step_mode()])
    rec.notes.append(
        f"pending suspensions after reload: {agent2.pending_suspensions(scope='session')}"
    )
    side = [e for e in loaded.entries() if e.get("type") == "suspension"]
    try:
        agent2.answer_suspension(str(side[-1]["id"]), "step")
    except Exception as exc:  # noqa: BLE001 - recording the refusal
        rec.notes.append(
            f"answer after reload: {type(exc).__name__}: {getattr(exc, 'reason', exc)}"
        )
    try:
        rec.catalog.fork(loaded, rec.cwd)
        rec.notes.append("whole-session fork after crash: succeeded")
    except Exception as exc:  # noqa: BLE001 - recording the refusal
        rec.notes.append(f"whole-session fork after crash: {type(exc).__name__}")
    rec.snapshot("reloaded-after-crash")
    provider2 = ScenProvider(lambda messages, n: "after crash")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider2.stream):
        agent2.add_suspension_responder("scen")
        sub = scen_tui_submission("continue after crash")
        result = await agent2.submit(sub)
        rec.notes.append(
            f"submit after crash: accepted={result.accepted} reason={scen_mask(rec, result.rejection_reason)!r}"
        )
    if provider2.requests:
        rec.notes.append(
            "request after crash: " + json.dumps([m.get("role") for m in provider2.requests[-1]])
        )
    rec.snapshot("prompt-after-crash")


async def scen_locks(rec: ScenRecorder) -> None:
    """A second process holds a session's write lock; this process loads the same file and tries to write."""
    proc, path = scen_spawn_child(rec, "holder")
    rec.snapshot("held-by-other-process", "no agent in this process")
    loaded = rec.catalog.load(str(path))
    agent = rec.new_agent(loaded)
    access = agent.write_access
    rec.notes.append(
        f"write_access: read_only={access.read_only} reason={scen_mask(rec, access.reason)!r}"
    )
    provider = ScenProvider(lambda messages, n: "x")
    from tau_agent_core.submission import Submission

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        try:
            returned = await agent.prompt("try to write")
            rec.notes.append(
                f"prompt() while held returned {returned!r}, events so far {len(rec.events)}"
            )
        except Exception as exc:  # noqa: BLE001 - recording the refusal
            rec.notes.append(f"prompt while held: {type(exc).__name__}")
        sub = Submission(text="try submit", source="user", submitter="scen", submission_id="held1")
        refused = await agent.submit(sub)
        rec.notes.append(
            f"submit() while held: accepted={refused.accepted} reason={scen_mask(rec, refused.rejection_reason)!r}"
        )
    rec.snapshot("second-process-refused")
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait()
    rec.snapshot("holder-exited")
    rec.notes.append(f"write_access after holder exit: read_only={agent.write_access.read_only}")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        returned = await agent.prompt("after holder exit")
    rec.notes.append(f"prompt() after holder exit returned {returned!r}")
    rec.snapshot("prompt-after-holder-exit")
    agent.release_write_locks()
    agent.cursor = Cursor.newest(loaded)
    rec.notes.append(
        f"after release_write_locks + rebind: read_only={agent.write_access.read_only}"
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        await agent.prompt("after rebind")
    rec.snapshot(
        "written-after-rebind", "release_write_locks() then cursor rebind re-retains the lock"
    )


def scen_tui_submission(text: str) -> Any:
    """The Submission ``TauApp`` builds for typed input (app.py:1094-1103)."""
    from tau_agent_core.submission import Submission

    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=f"tui-{text}",
        multitask_strategy="enqueue",
        expand_commands=True,
        allow_user_input=True,
    )


def scen_title(title: Any) -> str:
    """An ExchangeBox title with its elapsed-time readout masked."""
    import re

    return re.sub(r"\d+:\d\d", "<elapsed>", str(title))


def scen_box(box: Any, exchange_cls: type) -> str:
    """``role:text`` of a transcript MessageBox, prefixed by the title of the ExchangeBox holding it."""
    owner = next((a for a in box.ancestors if isinstance(a, exchange_cls)), None)
    where = (
        f"[{scen_title(owner.title).split(' · Working')[0]}] " if owner is not None else "[top] "
    )
    return f"{where}{box.role}:{str(box._content)[:24]}"


async def scen_spawn_tui(rec: ScenRecorder) -> None:
    """The Textual TUI under ``App.run_test``: a delegate tool spawns a child; record what the app shows."""
    from tau_coding_agent import editor_widgets, transcript
    from tau_coding_agent.backends import TauBackend
    from tau_coding_agent.chat_widgets import ExchangeBox, MessageBox
    from tau_coding_agent.testing.sandbox import build_tau_app, sandbox_tau_home

    shown: list[dict[str, Any]] = []
    holder: dict[str, Any] = {}

    def create(_cfg: dict[str, Any]) -> TauBackend:
        backend = TauBackend(
            {
                "backend": "openai",
                "model": "fake",
                "base_url": "http://fake.invalid/v1",
                "api_key": "k",
                "tools": [],
            }
        )
        holder["backend"] = backend
        return backend

    def look(app: Any, step: str) -> None:
        session = holder["backend"].agent_session
        display = app.query_one(transcript.ChatDisplay)
        strip = app.query_one(editor_widgets.StreamStrip)
        shown.append(
            {
                "step": step,
                "app_cursor_is_head": app._cursor is session.cursor,
                "app_cursor": rec.labels.get("c", app._cursor.id),
                "live_cursors": [rec.labels.get("c", c.id) for c in session.cursors],
                "app_messages_roles": [m.get("role") for m in app.messages],
                "exchange_titles": [scen_title(e.title) for e in display.query(ExchangeBox)],
                "message_boxes": [scen_box(b, ExchangeBox) for b in display.query(MessageBox)],
                "top_level": [type(w).__name__ for w in display.children],
                "tool_boxes": len(display.query("ToolBox")),
                "stream_strip": sorted(str(v) for v in strip.streams.values()),
                "display_streams": len(display._streams),
            }
        )

    tau_home = rec.root / "tui-home"
    tau_home.mkdir()
    with sandbox_tau_home(tau_home), patch("tau_coding_agent.app.create_backend", create):
        app = build_tau_app(tau_home)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.action_new_chat()
            session = holder["backend"].agent_session
            rec.watch(session)

            async def delegate(
                tool_call_id: str, params: dict[str, Any], signal: Any, on_update: Any, ctx: Any
            ) -> dict[str, Any]:
                parent = next(e["parentId"] for e in ctx.entries() if e["id"] == ctx.cursor.leaf)
                result = await ctx.spawn_branch(parent, "child task", tools=[])
                await pilot.pause()
                look(app, "in-tool-after-spawn")
                return {
                    "content": [
                        {"type": "text", "text": f"child said: {scen_text(result.messages[-1])}"}
                    ]
                }

            session._extension_api.register_tool(
                {
                    "name": "delegate",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                    "execute": delegate,
                }
            )

            async def route(messages: list[dict[str, Any]], n: int) -> Any:
                if any("child task" in scen_text(m) for m in messages):
                    await pilot.pause()
                    look(app, "during-child-request")
                    return "child result"
                if messages[-1].get("role") == "toolResult":
                    return "parent done"
                return scen_reply("", [ToolCall(id="callT", name="delegate", arguments={})])

            control = ScenProvider(lambda messages, n: "plain answer")
            with patch("tau_agent_core.agent_loop.stream_simple", side_effect=control.stream):
                sub = scen_tui_submission("hello")
                await app._get_assistant_response(sub)
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0.05)
            look(app, "control-turn-without-spawn")
            provider = ScenProvider(route)
            look(app, "before")
            with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
                sub = scen_tui_submission("delegate please")
                await app._get_assistant_response(sub)
            for _ in range(10):
                await pilot.pause()
                await asyncio.sleep(0.05)
            look(app, "after")
            await app._reload_transcript()
            for _ in range(5):
                await pilot.pause()
            look(app, "after-reload-transcript")
            rec.snapshot("after", "file store under the TUI's sandboxed TAU_HOME")
            session.release_write_locks()
    for row in shown:
        rec.notes.append(json.dumps(row, sort_keys=True))


SCENARIOS: dict[str, Callable[[ScenRecorder], Awaitable[None]]] = {
    "1-spawn": scen_spawn,
    "1b-spawn-tui": scen_spawn_tui,
    "2-fork": scen_fork,
    "3-concurrent": scen_concurrent,
    "3b-locks": scen_locks,
    "4-debugger": scen_debugger,
    "5-resume-cancel": scen_resume,
    "5b-resume-hard-exit": scen_resume_hard_exit,
    "6-compaction": scen_compaction,
}


def scen_render(out: Path, stem: str, kind: str, title: str) -> None:
    """Render ``stem``.dot or ``stem``.mmd to SVG with the repo's themed renderers."""
    py = sys.executable
    if kind == "dot":
        dot = shutil.which("dot")
        if dot is None:
            raise SystemExit("graphviz 'dot' not found: apt install graphviz")
        svg = subprocess.run(
            [dot, "-Tsvg", str(out / f"{stem}.dot")], check=True, capture_output=True
        ).stdout
        subprocess.run(
            [
                py,
                str(REPO / "scripts/arch/svg_theme.py"),
                "-",
                str(out / f"{stem}.svg"),
                "--title",
                title,
                "--desc",
                stem,
            ],
            input=svg,
            check=True,
        )
    else:
        subprocess.run(
            [
                py,
                str(REPO / "scripts/arch/seq_svg.py"),
                str(out / f"{stem}.mmd"),
                str(out / f"{stem}.svg"),
                "--title",
                title,
                "--desc",
                stem,
            ],
            check=True,
        )


async def scen_run_one(name: str, out: Path, render: bool) -> list[str]:
    """Run one scenario in a fresh temp store; write its JSON, diagrams and summary lines."""
    with tempfile.TemporaryDirectory(prefix="tau-scen-") as tmp:
        rec = ScenRecorder(name, Path(tmp))
        error = None
        try:
            await asyncio.wait_for(SCENARIOS[name](rec), 30)
        except Exception as exc:  # noqa: BLE001 - a failed scenario is reported, then re-raised
            error = f"{type(exc).__name__}: {exc}"
            rec.snapshot("error", error)
        for agent in rec.agents:
            agent.release_write_locks()
    for row in rec.events:
        raw = row.get("suspension_entry")
        if isinstance(raw, str) and raw.startswith("raw:"):
            row["suspension_entry"] = rec.labels.get("e", raw[4:])
    data = {
        "scenario": name,
        "snapshots": rec.snapshots,
        "events": rec.events,
        "notes": rec.notes,
        "error": error,
    }
    (out / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    for snap in rec.snapshots:
        stem = f"{name}-{snap['step']}"
        (out / f"{stem}.dot").write_text(scen_dot(rec, snap))
        if render:
            scen_render(out, stem, "dot", f"{name} {snap['step']}")
    (out / f"{name}-events.mmd").write_text(scen_mmd(rec))
    if render:
        scen_render(out, f"{name}-events", "mmd", f"{name} events")
    lines = [f"## {name}", ""]
    if error:
        lines += [f"ERROR: {error}", ""]
    if len(rec.snapshots) >= 2:
        lines += scen_table(rec.snapshots[0], rec.snapshots[-1]) + [""]
    lines += [f"- {n}" for n in rec.notes] + [""]
    if error:
        raise RuntimeError(f"scenario {name} failed: {error}")
    return lines


def main() -> int:
    """Parse arguments and run the selected scenarios."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=REPO / "build/arch/scenarios")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS))
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--scen-child", nargs=2, metavar=("KIND", "ROOT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scen_child:
        scen_child(args.scen_child[0], Path(args.scen_child[1]))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    summary = ["# Scenario snapshots", ""]
    failures = []
    for name in args.only or sorted(SCENARIOS):
        try:
            summary += asyncio.run(scen_run_one(name, args.out, not args.no_render))
        except RuntimeError as exc:
            failures.append(str(exc))
            summary += [f"## {name}", "", f"FAILED: {exc}", ""]
    (args.out / "SUMMARY.md").write_text("\n".join(summary) + "\n")
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
