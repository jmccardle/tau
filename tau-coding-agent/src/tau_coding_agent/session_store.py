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
    finalized_entry,
    keep_last_per_id,
    normalize_loaded_entries,
    CONFIG_ENTRY_TYPE,
    config_at,
    default_leaf,
    entries_for_fork,
    session_name,
)

from tau_agent_core.session_lock import SessionWriteLock, sidecar_for
from tau_agent_core.tau_home import resolve_tau_home
from tau_coding_agent.config import ConfigError

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
    return base_dir if base_dir is not None else resolve_tau_home() / SESSIONS_DIRNAME


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


def _required(config: dict[str, Any], key: str, session_id: str) -> str:
    """``config[key]`` as a string, or raise naming the session that lacks it."""
    value = config.get(key)
    if value is None:
        raise ValueError(f"session {session_id} has no {key!r} in its config")
    return str(value)


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
    A deferred session (``create(deferred=True)``) reads ``path`` as ``None``
    until its first write creates the file with everything held so far, because
    until then no listing can return it (``session_log_is_addressable``).
    """

    def __init__(
        self,
        path: Path | None,
        header: dict[str, Any],
        entries: list[dict[str, Any]],
        *,
        allow_unlocked_fs: bool = False,
    ):
        self._path = path
        self._header = header
        self._entries = entries
        self._ids: set[str] = {e["id"] for e in entries if "id" in e}
        self._unwritten = False
        self._allow_unlocked_fs = allow_unlocked_fs
        self._write_lock: SessionWriteLock | None = None
        self._disk_mark: tuple[int, int, int] | None = None

    @property
    def path(self) -> Path | None:
        """The session's file: ``None`` when ephemeral, and when deferred until its first write."""
        return None if self._unwritten else self._path

    @property
    def write_lock(self) -> SessionWriteLock | None:
        """The lock on ``<file>.lock`` a host retains while it writes; ``None`` when ephemeral.

        A deferred session's sidecar appears with its file (docs/STEP-DEBUGGER.md §3.7.3).
        """
        if self._path is None:
            return None
        if self._write_lock is None:
            self._write_lock = SessionWriteLock(
                sidecar_for(self._path),
                ready=lambda: not self._unwritten,
                allow_unlocked_fs=self._allow_unlocked_fs,
                on_acquire=self._refresh_from_disk,
            )
        return self._write_lock

    def _mark_disk(self) -> None:
        """Remember the file as this object last read or wrote it: inode, size, mtime."""
        if self.path is None:
            return
        st = os.stat(self.path)
        self._disk_mark = (st.st_ino, st.st_size, st.st_mtime_ns)

    def _refresh_from_disk(self) -> bool:
        """Re-read the file if another writer changed it since this object last saw it.

        Called when the write lock is taken, so a session loaded while another
        process held it never becomes the writer on a copy missing that
        process's last entries (docs/STEP-DEBUGGER.md §3.7.3).

        Returns:
            Whether the entries were reloaded.
        """
        if self.path is None or self._disk_mark is None:
            return False
        st = os.stat(self.path)
        if (st.st_ino, st.st_size, st.st_mtime_ns) == self._disk_mark:
            return False
        _header, entries = _read_session_file(self.path)
        self._entries = normalize_loaded_entries(keep_last_per_id(entries))
        self._ids = {e["id"] for e in self._entries if "id" in e}
        self._mark_disk()
        return True

    def _guard_write(self) -> None:
        """Raise ``SessionReadOnlyError`` while another process holds this session."""
        if self._write_lock is not None:
            self._write_lock.guard()

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
    def config(self) -> dict[str, Any]:
        """The folded config at the default leaf (``session_log.config_at``)."""
        return config_at(self._entries, default_leaf(self._entries))

    @property
    def model(self) -> str:
        """The config's model name. Raises if none — ``create`` always writes one
        (Fail-Early: don't fabricate a default)."""
        return _required(self.config, "model", self.id)

    @property
    def backend(self) -> str:
        """The config's backend name. Raises if none."""
        return _required(self.config, "backend", self.id)

    @property
    def name(self) -> str | None:
        """The newest ``session_info`` name (``session_log.session_name``), or ``None``."""
        return session_name(self._entries)

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
        deferred: bool = False,
        allow_unlocked_fs: bool = False,
    ) -> "Session":
        """Create a new persisted session.

        Args:
            cwd: Working directory the session belongs to.
            model: Config model name the session starts with.
            backend: Backend name the session starts with.
            system_prompt: System prompt text; ``None`` writes none.
            name: Session name; ``None`` leaves it unnamed.
            id: Session id; ``None`` mints one.
            base_dir: Sessions root; ``None`` is ``~/.tau/sessions``.
            deferred: Hold the header and opening chain in memory and write
                them with the first append, so an unused session leaves no file
                and no directory (``SessionCatalog.create_deferred``).
            allow_unlocked_fs: ``sessions.allow_unlocked_fs`` for its write lock.

        Returns:
            The new session.
        """
        timestamp = _now_iso()
        session_id = id if id is not None else uuid.uuid4().hex
        directory = session_dir_for_cwd(cwd, base_dir)
        path = directory / _session_filename(timestamp, session_id)
        header = cls._build_header(session_id, timestamp, os.path.abspath(cwd), parent=None)
        if deferred:
            session = cls(None, header, [], allow_unlocked_fs=allow_unlocked_fs)
            session._init_state(model, backend, system_prompt, name)
            session._path = path
            session._unwritten = True
        else:
            directory.mkdir(parents=True, exist_ok=True)
            session = cls(path, header, [], allow_unlocked_fs=allow_unlocked_fs)
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
    def load(cls, path: Path, *, allow_unlocked_fs: bool = False) -> "Session":
        """Stream a ``.jsonl`` file and reconstruct the session.

        Loading takes no lock; a host retains :attr:`write_lock` when it writes,
        and taking it re-reads the file if another writer changed it meanwhile.
        """
        header, entries = _read_session_file(path)
        session = cls(
            path,
            header,
            normalize_loaded_entries(keep_last_per_id(entries)),
            allow_unlocked_fs=allow_unlocked_fs,
        )
        session._mark_disk()
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
        allow_unlocked_fs: bool = False,
    ) -> "Session":
        """Fork ``source`` into a new file whose header ``parent`` is the source id.

        Copies every entry, or with ``at`` only the path to that entry, so the
        fork's default leaf is ``at`` (docs/CURSORS.md §4); either way the copy
        set is ``session_log.entries_for_fork``. The copy is self-contained and a
        written source file is never touched (§5.5); the fork gets its own fresh
        sidecar, never a copy of the source's. An unwritten deferred source
        is written first, so the fork's ``parent`` names a session on disk:
        forking is real activity on it.

        Raises:
            ValueError: ``at`` names no entry of ``source``.
            session_log.OpenSideEntryError: the fork point has an open side entry.
        """
        copied = [dict(e) for e in entries_for_fork(source._entries, at)]
        _emit_session_event(SESSION_BEFORE_FORK, source, cwd=cwd)
        if source._unwritten:
            source._write_held()
        timestamp = _now_iso()
        session_id = uuid.uuid4().hex
        directory = session_dir_for_cwd(cwd, base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _session_filename(timestamp, session_id)
        header = cls._build_header(session_id, timestamp, os.path.abspath(cwd), parent=source.id)
        session = cls(path, header, copied, allow_unlocked_fs=allow_unlocked_fs)
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
        """Write a new session's opening chain: config, optional name, system prompt.

        The root config entry precedes the system message (docs/CURSORS.md §5).
        Synchronous, because ``create``/``fork`` are classmethods a head may call
        before it has an event loop.
        """
        config = {"model": model, "backend": backend, "cwd": self.cwd}
        parent = self._append_at_now(
            None, "customEntry", {"customType": CONFIG_ENTRY_TYPE, "data": config}
        )
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
        self._guard_write()
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

    async def finalize(self, entry_id: str, payload: dict[str, Any]) -> None:
        """Complete an incomplete entry: a second line with its id, which the loader keeps.

        Raises:
            ValueError: ``entry_id`` names no entry, or one that is not incomplete.
            SessionReadOnlyError: another process holds this session.
        """
        self._guard_write()
        for index, entry in enumerate(self._entries):
            if entry.get("id") == entry_id:
                final = finalized_entry(entry, payload, _now_iso)
                self._entries[index] = final
                self._persist_entry(final)
                return
        raise ValueError(f"finalize: entry {entry_id!r} not found")

    def _persist_header(self) -> None:
        if self.path is None:
            return
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(self._header) + "\n")
        self._mark_disk()

    def _persist_entry(self, entry: dict[str, Any]) -> None:
        if self._unwritten:
            self._write_held()
            return
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self._mark_disk()

    def _write_held(self) -> None:
        """Create a deferred session's file: the header, then every entry held, the new one last.

        Written to a temp file in the same directory and renamed into place, so
        the file appears whole or not at all; a failed write leaves the session
        unwritten and the next append tries again.

        Raises:
            FileExistsError: a file already holds this session's path.
        """
        assert self._path is not None
        target = self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"refusing to overwrite {target} with a new session")
        fd, staging = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=".partial")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self._header) + "\n")
                for held in self._entries:
                    handle.write(json.dumps(held) + "\n")
            os.replace(staging, target)
        except BaseException:
            Path(staging).unlink(missing_ok=True)
            raise
        self._unwritten = False
        self._mark_disk()
        if self._write_lock is not None:
            self._write_lock.attach()


def _read_session_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ``.jsonl`` file's header and raw entry lines.

    Raises:
        ValueError: the file is empty or its first line is not a session header.
    """
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
    return header, entries


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

        entries = keep_last_per_id(entries)
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
    ``allow_unlocked_fs`` is ``sessions.allow_unlocked_fs``, handed to every
    session's write lock.
    """

    def __init__(self, base_dir: Path | None = None, *, allow_unlocked_fs: bool = False) -> None:
        self._base_dir = base_dir
        self._allow_unlocked_fs = allow_unlocked_fs

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
            allow_unlocked_fs=self._allow_unlocked_fs,
        )

    def create_deferred(
        self,
        cwd: str,
        model: str,
        backend: str,
        *,
        system_prompt: str | None = None,
        name: str | None = None,
    ) -> Session:
        """A session whose file and directory appear on its first append."""
        return Session.create(
            cwd,
            model,
            backend,
            system_prompt=system_prompt,
            name=name,
            base_dir=self._base_dir,
            deferred=True,
            allow_unlocked_fs=self._allow_unlocked_fs,
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
        return Session.load(Path(ref), allow_unlocked_fs=self._allow_unlocked_fs)

    def fork(self, source: ConversationSession, cwd: str, *, at: str | None = None) -> Session:
        if not isinstance(source, Session):
            raise TypeError(
                f"FileSessionCatalog.fork requires a file-backed Session, got {type(source)!r}"
            )
        return Session.fork(
            source, cwd, base_dir=self._base_dir, at=at, allow_unlocked_fs=self._allow_unlocked_fs
        )

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
