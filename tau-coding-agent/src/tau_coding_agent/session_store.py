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
from tau_agent_core.session_log import LANE_KEY, resolve_cursor

# τ data dir for config and session storage. Re-exported (not redefined) from the
# single config module — tests still monkeypatch ``session_store.TAU_DIR``, which
# rebinds this module global and works exactly as before.
from tau_coding_agent.config import TAU_DIR, ConfigError

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


# ---------------------------------------------------------------------------
# RPC mode's DEFAULT session base (unit S / docs/RPC-TIER-B.md D-6).
#
# `--mode rpc` does not write into the user's `~/.tau/sessions`: every spawn of
# an editor plugin's τ child would otherwise leave a durable, listable,
# 0-message session there, and `--continue` (`headless._select_session` ->
# `catalog.most_recent(cwd)`) would resume THAT instead of the human's work.
# Separating the LOCATION per mode is the fix (rather than filtering the
# listing, or reverting the startup session to ephemeral and re-breaking every
# durability promise D-6 exists to keep). `--session-dir` overrides it in both
# directions, and `--session-dir ~/.tau/sessions` is how a host says "yes, I
# really do want these in the user's list".
# ---------------------------------------------------------------------------


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
    # Racing creator, or a pre-existing entry. Either way, inspect what is
    # actually there rather than assuming the mkdir lost a benign race.
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
        """Resolve the persisted cursor — delegates to the shared entry algebra.

        The rule lives in ``tau_agent_core.session_log.resolve_cursor`` so every store
        (in-memory, file, database-backed) resolves the cursor identically; it is part
        of the entry algebra, not of any one durability layer.
        """
        return resolve_cursor(entries)

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
        """Raw linear fold: every PRIMARY-lane ``message`` entry in load order.

        This IGNORES the cursor and never splices compaction/``branch_summary``,
        so it is *not* what the user sees or the model receives — use ``context``
        for that. Kept because a few callers still want the flat entry list.

        **Lane-filtered (C2/W14).** Ignoring the cursor was harmless while one cursor
        wrote the log; with branch sub-agents it stops being harmless, because a
        cursor-ignoring fold picks up every sub-agent's messages too. The consumers are
        exactly the places a user would notice and mistrust: ``display_title()`` (a
        session named after a sub-agent's internal prompt), ``read_session_info``'s
        ``message_count``/``first_message`` in the picker, and ``rpc``'s
        ``message_count``. A sub-agent's private turns are not messages *of this
        conversation*, so they are excluded here. Anything wanting the true raw log
        still has ``entries()``.
        """
        return [
            e["message"] for e in self._entries if e.get("type") == "message" and LANE_KEY not in e
        ]

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
        """Persist a compaction splice anchored at ``first_kept_id``.

        Fail-Early on an unknown anchor, exactly as ``append_navigate`` and
        ``append_branch_summary`` already do. This one was missing, and it is the most
        damaging of the three to get wrong: ``ConversationTree._active_path_entries``
        walks the path looking for the anchor and only starts keeping entries once it
        finds it, so an id that matches nothing means the anchor is *never* found and
        the ENTIRE kept region is silently dropped from the fold. The context comes
        back as ``[summary] + whatever_was_appended_after`` and the conversation
        quietly loses its recent history — no error, no warning.
        """
        if first_kept_id not in self._ids:
            raise ValueError(
                f"compaction first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        _emit_session_event(SESSION_BEFORE_COMPACT, self, first_kept_id=first_kept_id)
        return self._append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
        )

    def append_elide(self, first_kept_id: str) -> str:
        """Persist a summary-less splice anchor (W3, NODE-ADDRESSABLE-AGENTS.md).

        The same splice ``ConversationTree._active_path_entries`` runs for
        ``compaction`` — anchor on the last of ``{"compaction", "elide"}`` in the
        path, drop everything before ``firstKeptId`` — with the ``summary`` and
        ``tokensBefore`` fields dropped: there is nothing to render, only a span to
        exclude. No ``SESSION_BEFORE_COMPACT`` event here — it names a compaction
        run specifically, and an ``elide`` is not one.

        Fail-Early on an unknown anchor, exactly as ``append_compaction`` does: an
        id matching nothing is never found by the fold's forward scan, so the
        entire kept region would silently drop out of context rather than raise.
        """
        if first_kept_id not in self._ids:
            raise ValueError(
                f"elide first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append("elide", firstKeptId=first_kept_id)

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

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
        *,
        lane: str | None = None,
    ) -> str:
        """Explicit-parent append — the C2/W14 branch primitive (see the ``SessionLog``
        Protocol). Parents where it is TOLD and does **not** move this log's leaf: a
        sub-agent's writes must never drag the primary cursor into its lane.

        The interleaving this allows (a branch's lines landing between two primary lines
        in one JSONL file) is already valid on disk — entries carry an explicit
        ``parentId``, so load order was never what defined the tree. Only the *writer*
        convenience of chaining off a single ``_leaf_id`` ever assumed one cursor.
        """
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        entry: dict[str, Any] = {
            "type": entry_type,
            "id": _generate_entry_id(self._ids),
            "parentId": parent_id,
            "timestamp": _now_iso(),
            **payload,
        }
        if lane is not None:
            entry[LANE_KEY] = lane
        self._entries.append(entry)
        self._ids.add(entry["id"])
        self._persist_entry(entry)
        return str(entry["id"])

    def _append(self, kind: str, **payload: Any) -> str:
        """The primary-lane append: ``append_at`` the current leaf, then move the leaf."""
        entry_id = self.append_at(self._leaf_id, kind, payload)
        self._leaf_id = entry_id
        return entry_id

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
#
# The dataclass itself now lives in tau_agent_core.session_catalog (W10): a path
# for the file store, a doc id for a future JMFTS-backed one — ``ref`` is the
# storage-agnostic handle, so the type had to move where storage-agnostic code
# (SessionCatalog.resolve_ref/most_recent) can consume it without importing
# tau_coding_agent (that import would be circular). The FILE-READING is not
# storage-agnostic, so ``read_session_info`` (formerly the ``SessionInfo.read``
# classmethod) stays here — tau-agent-core owns zero file I/O and that must stay
# true.
# ---------------------------------------------------------------------------


def read_session_info(path: Path) -> SessionInfo | None:
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
                elif kind == "message" and LANE_KEY not in entry:
                    # Primary lane only (C2/W14). A branch sub-agent's turns are not
                    # messages of THIS conversation: counting them would inflate the
                    # picker's message_count and — worse — let a sub-agent's internal
                    # prompt become the session's first_message, i.e. its display title.
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
            info = read_session_info(file)
            if info is not None:
                infos.append(info)
    infos.sort(key=lambda i: i.modified, reverse=True)
    return infos


def most_recent(cwd: str | None = None, base_dir: Path | None = None) -> Path | None:
    """The most recently modified session's path (pi ``findMostRecentSession``)."""
    infos = list_sessions(cwd, base_dir)
    return Path(infos[0].ref) if infos else None


# ---------------------------------------------------------------------------
# FileSessionCatalog — the SessionCatalog seam's file-store adapter (W10).
# ---------------------------------------------------------------------------


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

    def fork(self, source: ConversationSession, cwd: str) -> Session:
        # A real type gate, not an `assert`: asserts are stripped under `python -O`,
        # which would turn this into a silently-wrong call on a foreign session.
        if not isinstance(source, Session):
            raise TypeError(
                f"FileSessionCatalog.fork requires a file-backed Session, got {type(source)!r}"
            )
        return Session.fork(source, cwd, base_dir=self._base_dir)

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
