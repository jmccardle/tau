"""Persistence for τ coding sessions — append-only JSONL, partitioned by cwd.

A *session* is one ``.jsonl`` file under
``~/.tau/sessions/<dashed-cwd>/<iso-ts>_<uuid4>.jsonl``. Line 1 is a header; lines
2..N are append-only entries (messages, model/thinking changes, the mutable
session name, compaction markers). Both the TauApp TUI (``app.py``) and ``tau -p``
(``headless.py``) read and write this format, so a headless run is resumable in
the TUI and vice-versa.

This is the **coding-agent** session shape (cwd-scoped transcripts), replacing the
chat-web ``Chat`` blob τ inherited from TauApp. The module is deliberately free of
any Textual import: ``tau -p`` must not pull in the TUI just to persist a session.

Reference: docs/SESSION-UX-REDESIGN.md (§5 on-disk format; §9 Phase A seams).
pi parity: packages/coding-agent/src/core/session-manager.ts (cited inline).
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import (
    event_iso,
    normalize_loaded_entries,
    default_leaf,
)

from tau_coding_agent.config import TAU_DIR, ConfigError

SESSIONS_DIRNAME = "sessions"
# Header schema version (§5.3). Bumped only on a breaking on-disk change.
SESSION_VERSION = 1

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


def _sessions_base(base_dir: Path | None) -> Path:
    """The directory that holds the per-cwd subdirs (seam 1: ``base_dir`` slot)."""
    return base_dir if base_dir is not None else TAU_DIR / SESSIONS_DIRNAME


def session_dir_for_cwd(cwd: str, base_dir: Path | None = None) -> Path:
    """Map a working directory to its dashed-cwd session dir.

    Ports pi's ``getDefaultSessionDirPath`` (session-manager.ts:438-442):
    ``--`` + abspath (leading slash stripped, ``/`` ``\\`` ``:`` → ``-``) + ``--``.
    ``/srv/work/agent-harness-py`` → ``--srv-work-agent-harness-py--``.
    """
    abspath = os.path.abspath(cwd)
    dashed = (
        "--" + abspath.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    )
    return _sessions_base(base_dir) / dashed


class UnsafeSessionDirError(ConfigError):
    """The chosen session base exists but is not a private directory we own.

    A :class:`~tau_coding_agent.config.ConfigError` so ``cli.main()``'s single
    handler renders it as ``tau: error: ...`` (exit 2) — this must ABORT the
    run. Fail-Early: τ never writes into such a path and never quietly picks a
    different one, because both of those are how a symlink planted in a shared
    temp dir turns into "τ wrote your transcripts somewhere you can read".
    """


def _ensure_private_dir(path: Path) -> None:
    """``mkdir(0o700)`` ``path``, or verify an existing one is ours and private.

    The whole guard for hazard 2 of unit S: a name under a shared, sticky
    ``/tmp`` can be pre-created by anyone — as a symlink into someone's home,
    or as a directory of their own — and would then harvest whatever τ writes
    there next. :func:`rpc_default_session_base` puts our uid IN the name so
    two legitimate users never contend for one entry (round-3 finding 1); this
    function is what handles the case that remains after that, which is a
    HOSTILE squat on the name belonging to the uid being attacked.

    - Missing → created ``0o700`` (umask can only remove bits from that).
    - Exists and is not a directory → refuse. ``lstat`` (never ``stat``), so a
      SYMLINK is the non-directory it is rather than the directory it points at.
    - Exists, is a directory, but ``st_uid`` is not ours → refuse.
    - Ours, but group/other-accessible → tightened to ``0o700``. We own it, so
      narrowing it is ours to do; leaving a session store other users can plant
      files in is exactly the hazard this function exists for.
    """
    try:
        path.mkdir(mode=0o700)
        return
    except FileExistsError:
        pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        kind = "symlink" if stat.S_ISLNK(st.st_mode) else "non-directory file"
        raise UnsafeSessionDirError(
            f"refusing to use {path} as a session directory: it exists and is a "
            f"{kind}. τ will not follow it and will not silently choose another "
            "path; remove it, or pass --session-dir DIR to name one explicitly."
        )
    if st.st_uid != os.getuid():
        raise UnsafeSessionDirError(
            f"refusing to use {path} as a session directory: it is owned by uid "
            f"{st.st_uid}, not by you (uid {os.getuid()}). Another user created "
            "it first; remove it, or pass --session-dir DIR to name one "
            "explicitly."
        )
    if st.st_mode & 0o077:
        path.chmod(0o700)


def rpc_tmp_dirname() -> str:
    """``.tau-<uid>`` — the per-user directory name under the temp dir.

    The uid is IN the name, and that is the whole point (round-3 review,
    finding 1). The first shape of this was a flat ``.tau``, which on a default
    distro means ``/tmp/.tau`` — and ``/tmp`` is ``drwxrwxrwt``, shared and
    sticky. The first user to run ``--mode rpc`` on the box created it ``0700``,
    and :func:`_ensure_private_dir`'s ownership check — correct, and kept —
    then refused it for every OTHER user, aborting the run with exit 2 before
    it served a single request. ``--mode rpc`` became a mode one uid per boot
    could use, with an error message whose stated remedy ("remove it") the
    sticky bit forbids.

    Qualifying the name retires the COLLISION without weakening one refusal:
    a hostile user can still pre-create ``.tau-<your uid>``, and
    :func:`_ensure_private_dir` still refuses it loudly. What changes is that a
    second honest user is no longer indistinguishable from that attacker.
    """
    return f".tau-{os.getuid()}"


def rpc_default_session_base() -> Path:
    """``<tempdir>/.tau-<uid>/sessions`` — the DEFAULT base for ``--mode rpc``.

    Creates and validates both levels (:func:`_ensure_private_dir`) and returns
    the path; raises :class:`UnsafeSessionDirError` rather than writing into or
    around anything suspicious.

    ``tempfile.gettempdir()`` rather than a hardcoded ``/tmp``: it IS ``/tmp``
    unless ``$TMPDIR`` says otherwise, and honoring ``$TMPDIR`` costs nothing
    while giving every subprocess test (and every distro that hands each user a
    private temp dir) a real sandbox instead of the shared one. The uid in the
    name (:func:`rpc_tmp_dirname`) is what makes the SHARED case work too —
    see there.

    **Durability is bounded by machine uptime.** Most systems clear the temp dir
    on reboot, so an RPC session is durable for the life of the machine, not
    forever. That is stated on the wire too — in ``set_model``'s and
    ``set_session_name``'s ``notes`` — because the point of D-6 was to stop a
    cursor promising a durability it does not deliver, and replacing a loud lie
    with a quiet one would be the same defect wearing a different hat.
    """
    tau_dir = Path(tempfile.gettempdir()) / rpc_tmp_dirname()
    _ensure_private_dir(tau_dir)
    base = tau_dir / SESSIONS_DIRNAME
    _ensure_private_dir(base)
    return base


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
    def header(self) -> dict[str, Any]:
        """The line-1 header (seam 2: export + pi-faithful json need it raw)."""
        return dict(self._header)

    # --- reconstructed views ----------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Every ``message`` entry on the path to the default leaf, root first.

        Unspliced: compacted history still shows here, so this is neither what a
        user sees nor what the model receives — that is :attr:`context`. Read by
        the picker's title and counts. A head reads its own cursor, not this.
        """
        return [
            e["message"]
            for e in ConversationTree(self._entries, default_leaf(self._entries)).path()
            if e.get("type") == "message"
        ]

    @property
    def context(self) -> list[dict[str, Any]]:
        """The folded context at the default leaf: where a reopened tree continues."""
        return ConversationTree(self.entries(), default_leaf(self._entries)).context_for()

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
        """Ordered raw entries, all kinds (seam 2 — export / pi-faithful json).

        deepcopy, not dict(e): a shallow copy shares the nested payload, so a caller
        doing ``entries()[0]["message"]["content"] = …`` would mutate the live log AND
        silently diverge it from the on-disk JSONL, which is never rewritten.
        """
        return copy.deepcopy(self._entries)

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
        session = cls(path, header, normalize_loaded_entries(entries))
        _emit_session_event(SESSION_START, session)
        return session

    @classmethod
    def fork(
        cls,
        source: "Session",
        cwd: str,
        *,
        base_dir: Path | None = None,  # seam 1
        at: str | None = None,
    ) -> "Session":
        """Fork ``source`` into a new file whose header ``parent`` is the source id.

        Copies every entry, or with ``at`` only the path to that entry, so the
        fork's default leaf is ``at`` (docs/CURSORS.md §4). The copy is
        self-contained and the source file is never touched (§5.5).

        Raises:
            ValueError: ``at`` names no entry of ``source``.
        """
        _emit_session_event(SESSION_BEFORE_FORK, source, cwd=cwd)
        timestamp = _now_iso()
        session_id = uuid.uuid4().hex
        directory = session_dir_for_cwd(cwd, base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _session_filename(timestamp, session_id)
        header = cls._build_header(session_id, timestamp, os.path.abspath(cwd), parent=source.id)
        if at is None:
            copied = [dict(e) for e in source._entries]
        else:
            if at not in source._ids:
                raise ValueError(f"fork point {at!r} not found")
            on_path = {e["id"] for e in ConversationTree(source._entries, at).path()}
            copied = [dict(e) for e in source._entries if e.get("id") in on_path]
        session = cls(path, header, copied)
        session._persist_header()
        for entry in copied:
            session._persist_entry(entry)
        return session

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
        """Write a new session's opening chain: model, optional name, system prompt.

        Synchronous, because ``create``/``fork`` are classmethods a head may call
        before it has an event loop. The chain is parented entry to entry; there is
        no stored leaf to move.
        """
        parent = self._append_at_now(None, "model_change", {"model": model, "backend": backend})
        if name is not None:
            parent = self._append_at_now(parent, "session_info", {"name": name})
        if system_prompt:
            self._append_at_now(
                parent, "message", {"message": {"role": "system", "content": system_prompt}}
            )

    async def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Write one entry at ``parent_id`` (the ``SessionLog`` contract).

        Entries from several cursors interleave in the file; ``parentId``, not line
        order, defines the tree. A ``compaction`` emits ``session_before_compact``
        first, for ``subscribe_session_events`` listeners.
        """
        if entry_type == "compaction":
            _emit_session_event(
                SESSION_BEFORE_COMPACT, self, first_kept_id=payload.get("firstKeptId")
            )
        return self._append_at_now(parent_id, entry_type, payload)

    def _append_at_now(
        self, parent_id: str | None, entry_type: str, payload: dict[str, Any]
    ) -> str:
        """The synchronous write :meth:`append_at` exposes as a coroutine.

        It does **not** hop to a thread, unlike the JMFTS store's. One entry is a
        buffered append to a local file; docs/BLOCKING-PERSISTENCE.md §4 measured
        the freeze as invisible for this store, and a thread hop per entry costs
        about what the write does. The ``async`` signature is the Protocol's
        contract, not a claim that this store threads.

        The split also lets :meth:`_init_state` write a new session's opening
        entries from a synchronous classmethod, where there may be no running loop.
        """
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        entry: dict[str, Any] = {
            "type": entry_type,
            "id": _generate_entry_id(self._ids),
            "parentId": parent_id,
            "timestamp": event_iso(payload, _now_iso),
            **payload,
        }
        self._entries.append(entry)
        self._ids.add(entry["id"])
        self._persist_entry(entry)
        return str(entry["id"])

    def _persist_header(self) -> None:
        if self.path is None:
            return
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(self._header) + "\n")

    def _persist_entry(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def read_session_info(path: Path) -> SessionInfo | None:
    """Read a file → SessionInfo, or None on any parse error (skip at the
    list edge — Fail-Early: a corrupt file shouldn't break the whole listing).

    **The message counts are ancestry-scoped** (docs/LANE-REMOVAL.md §3.2, §6.3), which
    is why this buffers the entries instead of accumulating in one streaming pass: the
    picker's ``message_count`` / ``first_message`` describe *the conversation you would
    resume*, so they are taken from the cursor's ancestor chain. A flat count is wrong
    on any session that has been forked — it sums mutually exclusive alternatives — and
    was previously patched with a ``branchOf`` filter that only helped when the extra
    entries came from a sub-agent. Buffering costs a constant factor here (both passes
    are dominated by ``json.loads``, and this runs in the picker's worker thread), not
    a change of complexity.

    ``name`` and the modified timestamp stay whole-file: a session's name is
    session-level metadata, and any write to the file — on the active path or not —
    genuinely did modify it.
    """
    try:
        header: dict[str, Any] | None = None
        name: str | None = None
        message_count = 0
        first_message = ""
        last_message = ""
        last_timestamp: str | None = None
        entries: list[dict[str, Any]] = []

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

                if entry.get("type") == "session_info":
                    value = entry.get("name")
                    name = value.strip() if isinstance(value, str) and value.strip() else None
                entries.append(entry)

        if header is None:
            return None

        for entry in ConversationTree(entries, default_leaf(entries)).path():
            if entry.get("type") != "message":
                continue
            message = entry.get("message", {})
            role = message.get("role")
            if role in ("user", "assistant"):
                message_count += 1
                text = _extract_text(message)
                if text:
                    last_message = text
                    if not first_message and role == "user":
                        first_message = text

        created = _parse_iso(str(header["timestamp"]))
        modified = _parse_iso(last_timestamp) if last_timestamp else created
        return SessionInfo(
            ref=str(path),
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
            info = read_session_info(file)
            if info is not None:
                infos.append(info)
    infos.sort(key=lambda i: i.modified, reverse=True)
    return infos


def most_recent(cwd: str | None = None, base_dir: Path | None = None) -> Path | None:
    """The most recently modified session's path (pi ``findMostRecentSession``)."""
    infos = list_sessions(cwd, base_dir)
    return Path(infos[0].ref) if infos else None


class FileSessionCatalog(SessionCatalog):
    """Thin :class:`~tau_agent_core.session_catalog.SessionCatalog` adapter over
    the existing module-level ``Session``/``list_sessions`` API.

    Adds no new behaviour: every method is a direct pass-through to the concrete
    ``Session`` classmethods (or ``list_sessions``/``read_session_info`` for
    listing) that already implement this on-disk format. ``base_dir`` is the
    optional seam-1 override (``--session-dir``, and every test that sandboxes a
    ``tmp_path``); ``None`` means the real ``~/.tau/sessions``.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir

    def create(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> Session:
        return Session.create(
            cwd,
            model,
            backend,
            system_prompt=system_prompt,
            name=name,
            base_dir=self._base_dir,
        )

    def create_ephemeral(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> Session:
        return Session.create_in_memory(cwd, model, backend, system_prompt=system_prompt, name=name)

    def load(self, ref: str) -> Session:
        return Session.load(Path(ref))

    def fork(self, source: ConversationSession, cwd: str, *, at: str | None = None) -> Session:
        if not isinstance(source, Session):
            raise TypeError(
                f"FileSessionCatalog.fork requires a file-backed Session, got {type(source)!r}"
            )
        return Session.fork(source, cwd, base_dir=self._base_dir, at=at)

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        return list_sessions(cwd, base_dir=self._base_dir)

    def resolve_ref(self, ref: str, *, cwd: str | None = None) -> ConversationSession:
        """A file-store REF may be a ``.jsonl`` PATH as well as a session id.

        The path form is this store's own directly-addressable ref, so it is
        resolved here rather than in the storage-agnostic base — core must not know
        what a ``.jsonl`` is. A path that does not exist falls through to the base's
        id / id-prefix search (matching the original ``p.exists()`` guard); a path
        that exists but is *malformed* still raises out of ``load()``, so a real
        corruption surfaces instead of being silently reinterpreted as an id.
        """
        if Path(ref).suffix == ".jsonl" and Path(ref).exists():
            return self.load(ref)
        return super().resolve_ref(ref, cwd=cwd)
