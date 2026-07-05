"""Persistence for τ coding sessions — append-only JSONL, partitioned by cwd.

A *session* is one ``.jsonl`` file under
``~/.tau/sessions/<dashed-cwd>/<iso-ts>_<uuid4>.jsonl``. Line 1 is a header; lines
2..N are append-only entries (messages, model/thinking changes, the mutable
session name, compaction markers). Both the Parley TUI (``app.py``) and ``tau -p``
(``headless.py``) read and write this format, so a headless run is resumable in
the TUI and vice-versa.

This is the **coding-agent** session shape (cwd-scoped transcripts), replacing the
chat-web ``Chat`` blob τ inherited from Parley. The module is deliberately free of
any Textual import: ``tau -p`` must not pull in the TUI just to persist a session.

Reference: docs/SESSION-UX-REDESIGN.md (§5 on-disk format; §9 Phase A seams).
pi parity: packages/coding-agent/src/core/session-manager.ts (cited inline).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tau_agent_core.conversation_tree import ConversationTree

# τ data dir for config and session storage (matches app.py / cli.py).
TAU_DIR = Path.home() / ".tau"
# pi derives this from APP_NAME (config.ts:481-482, PI_CODING_AGENT_SESSION_DIR);
# a TAU_CODING_AGENT_SESSION_DIR override is reserved but not implemented (§5.1).
SESSIONS_DIRNAME = "sessions"
# Header schema version (§5.3). Bumped only on a breaking on-disk change.
SESSION_VERSION = 1

# ---------------------------------------------------------------------------
# Seam 3 — session lifecycle events (docs/SESSION-UX-REDESIGN.md §9 Phase A).
#
# Session.create/load/fork/append_compaction emit events here. The extension bus
# is the first consumer (S21 / §E3c.4): the TUI wires each new backend's
# ``AgentSession.route_session_event`` here (app.py ``_bind_backend_session``), which
# re-emits the dict onto the session's ``EventBus`` on a separate string channel so an
# ``api.on("session_before_compact", …)`` extension handler fires. Kept minimal and
# in-process; no fabricated behaviour (Fail-Early).
# ---------------------------------------------------------------------------

SESSION_START = "session_start"
SESSION_BEFORE_FORK = "session_before_fork"
SESSION_BEFORE_COMPACT = "session_before_compact"
SESSION_SHUTDOWN = "session_shutdown"

_session_listeners: list[Callable[[dict[str, Any]], None]] = []


def subscribe_session_events(listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """Register a session-lifecycle listener; returns an unsubscribe callable."""
    _session_listeners.append(listener)

    def _unsubscribe() -> None:
        if listener in _session_listeners:
            _session_listeners.remove(listener)

    return _unsubscribe


def _emit_session_event(event_type: str, session: "Session", **extra: Any) -> None:
    event: dict[str, Any] = {"type": event_type, "session": session, **extra}
    for listener in list(_session_listeners):
        listener(event)


# ---------------------------------------------------------------------------
# Path / id / time helpers (pi parity cited inline).
# ---------------------------------------------------------------------------


def _sessions_base(base_dir: Path | None) -> Path:
    """The directory that holds the per-cwd subdirs (seam 1: ``base_dir`` slot)."""
    return base_dir if base_dir is not None else TAU_DIR / SESSIONS_DIRNAME


def session_dir_for_cwd(cwd: str, base_dir: Path | None = None) -> Path:
    """Map a working directory to its dashed-cwd session dir.

    Ports pi's ``getDefaultSessionDirPath`` (session-manager.ts:438-442):
    ``--`` + abspath (leading slash stripped, ``/`` ``\\`` ``:`` → ``-``) + ``--``.
    ``/home/john/Development/agent-harness-py`` →
    ``--home-john-Development-agent-harness-py--``.
    """
    abspath = os.path.abspath(cwd)
    dashed = (
        "--" + abspath.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    )
    return _sessions_base(base_dir) / dashed


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with millisecond precision + ``Z``.

    Mirrors JS ``new Date().toISOString()`` (e.g. ``2026-06-22T14:03:51.204Z``).
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp (our own ``_now_iso`` output, incl. the ``Z``)."""
    # Python 3.11+ datetime.fromisoformat accepts the trailing 'Z'.
    return datetime.fromisoformat(value)


def _session_filename(timestamp_iso: str, session_id: str) -> str:
    """``<iso-ts-dashes>_<id>.jsonl`` (§5.2; pi session-manager.ts:845).

    Colons and periods → ``-`` so the filename is filesystem-safe *and* sorts
    chronologically under ``ls``.
    """
    file_ts = timestamp_iso.replace(":", "-").replace(".", "-")
    return f"{file_ts}_{session_id}.jsonl"


def _generate_entry_id(existing: set[str]) -> str:
    """8-hex collision-checked entry id (pi ``generateId``, session-manager.ts:215)."""
    for _ in range(100):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
    return uuid.uuid4().hex  # pragma: no cover — 100 collisions is astronomically unlikely


def _extract_text(message: dict[str, Any]) -> str:
    """Flatten a τ message's content to plain text (for picker display/search)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        ]
        return " ".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Session — wraps one .jsonl file; append-on-message.
# ---------------------------------------------------------------------------


class Session:
    """One coding session: a header + an append-only list of entries.

    ``path`` is ``None`` for an in-memory (ephemeral) session — every ``append_*``
    becomes a pure in-memory mutation with no disk flush (seam 1, ``--no-session``).
    """

    def __init__(self, path: Path | None, header: dict[str, Any], entries: list[dict[str, Any]]):
        self.path = path
        self._header = header
        self._entries = entries
        self._ids: set[str] = {e["id"] for e in entries if "id" in e}
        self._leaf_id: str | None = self._resolve_cursor(entries)

    @staticmethod
    def _resolve_cursor(entries: list[dict[str, Any]]) -> str | None:
        """Resolve the persisted cursor (leaf pointer) from the LAST entry.

        Latest-wins, mirroring how ``model``/``name`` resolve (§2.2): a ``navigate``
        entry points at its ``targetId`` (``null`` = pre-root, before the first
        entry); any other kind points at itself. Pi-style files carry no
        ``navigate`` entries, so the cursor is the last entry — identical to pi's
        "fall back to last entry" on load (session-manager.ts:855-859)."""
        if not entries:
            return None
        last = entries[-1]
        if last.get("type") == "navigate":
            target = last.get("targetId")
            return str(target) if target is not None else None
        return str(last["id"])

    # --- identity / header -------------------------------------------------

    @property
    def id(self) -> str:
        return str(self._header["id"])

    @property
    def cwd(self) -> str:
        return str(self._header.get("cwd", ""))

    @property
    def parent(self) -> str | None:
        return self._header.get("parent")

    @property
    def cursor(self) -> str | None:
        """The current leaf (tip) entry id; ``None`` before the first entry.

        Exposes ``_leaf_id`` under the name the ``tau_agent_core.session_log``
        ``SessionLog`` Protocol reads, so ``Session`` satisfies that facade
        structurally and ``AgentSession`` can build a ``ConversationTree`` view
        over the live session (§2.6, §4.2)."""
        return self._leaf_id

    @property
    def header(self) -> dict[str, Any]:
        """The line-1 header (seam 2: export + pi-faithful json need it raw)."""
        return dict(self._header)

    # --- reconstructed views ----------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Raw linear fold: every ``message`` entry in load order.

        This IGNORES the cursor and never splices compaction/``branch_summary``,
        so it is *not* what the user sees or the model receives — use ``context``
        for that. Kept because a few callers still want the flat entry list.
        """
        return [e["message"] for e in self._entries if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        """The active-path context at the current cursor — the pi-faithful render
        and model-input source (pi ``buildSessionContext``, session-manager.ts:325).

        The ``ConversationTree`` fold over this session's entries: compaction /
        ``branch_summary`` splices applied, abandoned branches dropped via the
        ``parentId`` walk. Unlike ``messages`` (the raw linear fold, which shows a
        compacted session's dropped history and hides the summary), this is what
        must seed the TUI/headless transcript and the LLM context on load, new,
        fork, and resume. Reference: docs/SESSION-TREE-IMPLEMENTATION.md §2.6.
        """
        return ConversationTree(self.entries(), self.cursor).context_for()

    @property
    def model(self) -> str:
        """Latest ``model_change`` model (config key). Raises if none — a session
        always has one from ``create`` (Fail-Early: don't fabricate a default)."""
        for entry in reversed(self._entries):
            if entry.get("type") == "model_change":
                return str(entry["model"])
        raise ValueError(f"session {self.id} has no model_change entry")

    @property
    def backend(self) -> str:
        for entry in reversed(self._entries):
            if entry.get("type") == "model_change":
                return str(entry["backend"])
        raise ValueError(f"session {self.id} has no model_change entry")

    @property
    def name(self) -> str | None:
        """Latest ``session_info`` name (mutable; None if never set)."""
        for entry in reversed(self._entries):
            if entry.get("type") == "session_info":
                value = entry.get("name")
                return str(value) if value else None
        return None

    def entries(self) -> list[dict[str, Any]]:
        """Ordered raw entries, all kinds (seam 2 — export / pi-faithful json)."""
        return [dict(e) for e in self._entries]

    def display_title(self) -> str:
        """A short human label: the name, else the first user message, else model."""
        if self.name:
            return self.name
        for message in self.messages:
            if message.get("role") == "user":
                text = _extract_text(message).replace("\n", " ")
                if text:
                    return text[:50] + ("..." if len(text) > 50 else "")
        return f"Session ({self.model})"

    # --- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
        id: str | None = None,  # seam 1 → --session-id
        base_dir: Path | None = None,  # seam 1 → --session-dir
    ) -> "Session":
        """Create a new persisted session; write header + initial entries."""
        timestamp = _now_iso()
        session_id = id if id is not None else uuid.uuid4().hex
        directory = session_dir_for_cwd(cwd, base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _session_filename(timestamp, session_id)
        header = cls._build_header(session_id, timestamp, os.path.abspath(cwd), parent=None)
        session = cls(path, header, [])
        session._persist_header()
        session._init_state(model, backend, system_prompt, name)
        _emit_session_event(SESSION_START, session)
        return session

    @classmethod
    def create_in_memory(
        cls,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> "Session":
        """Ephemeral session (seam 1, pi ``inMemory`` session-manager.ts:1430):
        ``path=None``, entries held in a list, every ``append_*`` skips the disk
        flush. One API serves persisted and unpersisted runs. → ``--no-session``."""
        timestamp = _now_iso()
        header = cls._build_header(uuid.uuid4().hex, timestamp, os.path.abspath(cwd), parent=None)
        session = cls(None, header, [])
        session._init_state(model, backend, system_prompt, name)
        _emit_session_event(SESSION_START, session)
        return session

    @classmethod
    def load(cls, path: Path) -> "Session":
        """Stream a ``.jsonl`` file and reconstruct the session."""
        header: dict[str, Any] | None = None
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if header is None:
                    if obj.get("type") != "session":
                        raise ValueError(f"{path}: first line is not a session header")
                    header = obj
                else:
                    entries.append(obj)
        if header is None:
            raise ValueError(f"{path}: empty session file (no header)")
        session = cls(path, header, entries)
        _emit_session_event(SESSION_START, session)
        return session

    @classmethod
    def fork(
        cls,
        source: "Session",
        cwd: str,
        *,
        base_dir: Path | None = None,  # seam 1
    ) -> "Session":
        """Fork ``source`` into a new file whose header ``parent`` is the source id.

        Copies the source's entries (self-contained — no cross-file chaining), then
        new turns append. The source file is never touched (§5.5)."""
        _emit_session_event(SESSION_BEFORE_FORK, source, cwd=cwd)
        timestamp = _now_iso()
        session_id = uuid.uuid4().hex
        directory = session_dir_for_cwd(cwd, base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _session_filename(timestamp, session_id)
        header = cls._build_header(session_id, timestamp, os.path.abspath(cwd), parent=source.id)
        copied = [dict(e) for e in source._entries]
        session = cls(path, header, copied)
        session._persist_header()
        for entry in copied:
            session._persist_entry(entry)
        return session

    # --- append API (append-on-message; §5.4) ------------------------------

    def append_message(self, message: dict[str, Any]) -> str:
        return self._append("message", message=message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        """Persist an extension-injected custom message as a ``customMessage`` node.

        The on-disk counterpart of ``InMemorySessionLog.append_custom_message``
        (E5 §3.1 / S29): the durable, reloadable form of a ``before_agent_start``
        injection. Its own entry KIND carrying the stored ``message`` (``role:
        "custom"``) and the top-level ``customType`` — folded onto the active path
        by ``ConversationTree`` and remapped custom→user on the wire."""
        return self._append("customMessage", customType=custom_type, message=message)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        """Persist a durable, NON-message ``customEntry`` node (E6 §2 / S39).

        The on-disk counterpart of ``InMemorySessionLog.append_custom_entry``: the
        reloadable backing for ``api.append_entry`` (was the RAM-only registry
        ``_entry_store``, lost on restart — G4). Its own entry KIND carrying the
        extension's ``{customType, data}`` — flushed to the ``.jsonl`` on append and
        reconstructed by ``load`` like every other entry, so it round-trips a
        reload. It is NOT a ``message``/``customMessage`` node, so ``ConversationTree``
        never folds it into context and ``convert_to_llm`` never sees it (tree-as-
        backplane state: on the durable path, excluded from model input)."""
        return self._append("customEntry", customType=custom_type, data=data)

    def append_model_change(self, model: str, backend: str) -> str:
        return self._append("model_change", model=model, backend=backend)

    def append_thinking_change(self, level: str) -> str:
        return self._append("thinking_change", level=level)

    def append_session_info(self, name: str) -> str:
        return self._append("session_info", name=name)

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str:
        _emit_session_event(SESSION_BEFORE_COMPACT, self, first_kept_id=first_kept_id)
        return self._append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
        )

    def append_navigate(self, target_id: str | None) -> str:
        """Persist a cursor move as a first-class ``navigate`` entry (§2.2).

        pi's ``leafId`` is in-memory only and evaporates on quit (branch() moves
        the cursor without appending, session-manager.ts:1241-1246); τ diverges so
        an agent (or the tree-browser) can move the tip *without* new content and
        have it survive a reload. The entry's ``parentId`` is the previous leaf;
        ``targetId`` (``None`` = before-first-entry) is where the cursor now sits,
        and the in-memory leaf advances to it (not to the navigate entry itself).

        Fail-Early: a non-``None`` target must name a real entry, mirroring pi's
        ``branch()`` "Entry ... not found" throw (session-manager.ts:1242-1244)
        and ``ConversationTree.navigate`` (conversation_tree.py:121-125); persisting
        a dangling cursor would silently drop the whole conversation at read time."""
        if target_id is not None and target_id not in self._ids:
            raise ValueError(f"navigate target {target_id!r} not found")
        entry_id = self._append("navigate", targetId=target_id)
        self._leaf_id = target_id
        return entry_id

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Persist a ``branch_summary`` inline node at the branch point (§2.4, §5).

        pi ``branchWithSummary`` (session-manager.ts:1262-1279) sets
        ``this.leafId = branchFromId`` *first*, then appends — so the summary parents
        at the branch point and the abandoned children become a **sibling branch off
        the active path** (Decision 5, fix 1). We mirror that: move the in-memory leaf
        to ``from_id`` before appending, so ``parentId == from_id``. The abandoned
        branch then drops out of ``context_for`` purely via the ``parentId`` walk —
        ``branch_summary`` is a plain inline node at read time, NOT a splice anchor
        (Decision 5, fix 2; ``ConversationTree`` §5). The leaf then advances to this
        entry (pi ``_appendEntry``, session-manager.ts:937-942).

        Fail-Early: a non-``None`` ``from_id`` must name a real entry, mirroring pi's
        ``branchWithSummary`` "Entry ... not found" throw (session-manager.ts:1266-1268)."""
        if from_id is not None and from_id not in self._ids:
            raise ValueError(f"branch_summary from {from_id!r} not found")
        self._leaf_id = from_id  # branch point, not the current leaf (pi :1272)
        return self._append("branch_summary", summary=summary, fromId=from_id)

    def shutdown(self) -> None:
        """Signal end-of-session (seam 3). Emits ``session_shutdown``; no disk
        effect (every entry is already flushed on append)."""
        _emit_session_event(SESSION_SHUTDOWN, self)

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _build_header(
        session_id: str, timestamp: str, cwd: str, *, parent: str | None
    ) -> dict[str, Any]:
        return {
            "type": "session",
            "version": SESSION_VERSION,
            "id": session_id,
            "timestamp": timestamp,
            "cwd": cwd,
            "parent": parent,
        }

    def _init_state(
        self, model: str, backend: str, system_prompt: str | None, name: str | None
    ) -> None:
        """Write the entries every new session carries: model, optional name, and
        the system prompt as the first ``message`` entry (uniform reconstruction)."""
        self.append_model_change(model, backend)
        if name is not None:
            self.append_session_info(name)
        if system_prompt:
            self.append_message({"role": "system", "content": system_prompt})

    def _append(self, kind: str, **payload: Any) -> str:
        entry: dict[str, Any] = {
            "type": kind,
            "id": _generate_entry_id(self._ids),
            "parentId": self._leaf_id,
            "timestamp": _now_iso(),
            **payload,
        }
        self._entries.append(entry)
        self._ids.add(entry["id"])
        self._leaf_id = entry["id"]
        self._persist_entry(entry)
        return str(entry["id"])

    def _persist_header(self) -> None:
        if self.path is None:
            return
        # Exclusive create: the uuid4 filename makes a collision impossible, and
        # 'x' guarantees we never silently clobber a sibling (Fail-Early).
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(self._header) + "\n")

    def _persist_entry(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# SessionInfo — the picker's lightweight streaming reader (§5.7).
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """List metadata extracted without building agent messages (fast listing)."""

    path: Path
    id: str
    cwd: str
    name: str | None
    created: datetime
    modified: datetime
    message_count: int
    first_message: str
    last_message: str
    parent: str | None

    def display_title(self) -> str:
        if self.name:
            return self.name
        text = self.first_message.replace("\n", " ")
        if not text:
            return f"Session ({self.id[:8]})"
        return text[:50] + ("..." if len(text) > 50 else "")

    @classmethod
    def read(cls, path: Path) -> "SessionInfo | None":
        """Stream a file → SessionInfo, or None on any parse error (skip at the
        list edge — Fail-Early: a corrupt file shouldn't break the whole listing)."""
        try:
            header: dict[str, Any] | None = None
            name: str | None = None
            message_count = 0
            first_message = ""
            last_message = ""
            last_timestamp: str | None = None

            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    entry = json.loads(raw)
                    if header is None:
                        if entry.get("type") != "session":
                            return None
                        header = entry
                        continue

                    timestamp = entry.get("timestamp")
                    if isinstance(timestamp, str):
                        last_timestamp = timestamp

                    kind = entry.get("type")
                    if kind == "session_info":
                        value = entry.get("name")
                        name = value.strip() if isinstance(value, str) and value.strip() else None
                    elif kind == "message":
                        message = entry.get("message", {})
                        role = message.get("role")
                        if role in ("user", "assistant"):
                            message_count += 1
                            text = _extract_text(message)
                            if text:
                                last_message = text
                                if not first_message and role == "user":
                                    first_message = text

            if header is None:
                return None

            created = _parse_iso(str(header["timestamp"]))
            modified = _parse_iso(last_timestamp) if last_timestamp else created
            return cls(
                path=path,
                id=str(header["id"]),
                cwd=str(header.get("cwd", "")),
                name=name,
                created=created,
                modified=modified,
                message_count=message_count,
                first_message=first_message,
                last_message=last_message,
                parent=header.get("parent"),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None


# ---------------------------------------------------------------------------
# Listing & scoping (§5.8).
# ---------------------------------------------------------------------------


def list_sessions(cwd: str | None = None, base_dir: Path | None = None) -> list[SessionInfo]:
    """List sessions, newest (by ``modified``) first.

    ``cwd`` given → list that one dashed-cwd dir (cheap; already partitioned).
    ``cwd`` None → walk every dashed-cwd dir under the base.
    """
    if cwd is not None:
        dirs = [session_dir_for_cwd(cwd, base_dir)]
    else:
        base = _sessions_base(base_dir)
        dirs = sorted(d for d in base.iterdir() if d.is_dir()) if base.exists() else []

    infos: list[SessionInfo] = []
    for directory in dirs:
        if not directory.exists():
            continue
        for file in directory.glob("*.jsonl"):
            info = SessionInfo.read(file)
            if info is not None:
                infos.append(info)
    infos.sort(key=lambda i: i.modified, reverse=True)
    return infos


def most_recent(cwd: str | None = None, base_dir: Path | None = None) -> Path | None:
    """The most recently modified session's path (pi ``findMostRecentSession``)."""
    infos = list_sessions(cwd, base_dir)
    return infos[0].path if infos else None
