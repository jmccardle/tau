"""``ext_kit.state`` — the *backplane* atom.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S56.

A *backplane* is where an extension keeps state between events, turns, and — for
the file-backed half — between whole sessions. τ has a native answer for the
conversation-scoped case that pi lacks: the session tree itself is the durable,
reload-safe canon store (roadmap §1.1, the tree-as-backplane thesis). Anything an
extension appends onto the path via ``api.append_entry`` (the S39 durable
``customEntry`` node) is *already* persisted, reload-invariant, and readable back
through ``ctx.entries()`` — no side database needed. This module gives that a
typed, reconstructing surface, plus a small file store for the state that must
outlive a single conversation:

* :class:`TreeStore` — typed records over the durable ``customEntry`` node, one
  store per ``custom_type``. :meth:`TreeStore.append` writes a record; :meth:`TreeStore.load`
  reconstructs every record for that type **along the active path** from
  ``ctx.entries()`` (reload-safe, conversation-scoped). Because ``customEntry`` is
  excluded from ``convert_to_llm``, these records are backplane state — durable and
  on the path, but never model input.
* :class:`FileStore` — an atomic JSON blob under ``~/.tau/ext-state/<name>.json``,
  for **cross-session** state (ledgers, a red-team corpus) that is not scoped to
  one conversation's tree. Writes go through a same-directory temp file +
  ``os.replace`` so a crash mid-write never truncates the existing state.

**Why the active path, not every entry.** ``ctx.entries()`` returns the raw
append-only log — *all* branches, including ones the user navigated away from or
forked off. Reconstructing from all of them would resurrect records from
abandoned branches, a silent divergence from what the session actually shows. So
:class:`TreeStore` walks the ``parentId`` chain from the current cursor to the
root (the same active-path notion the tree renders), keeping only the
``customEntry`` records on it. The cursor is derived from the log itself by
replaying its append/navigate algebra (:func:`_active_cursor`) — no private-attr
reach, no harness import; the store composes ``api.append_entry`` +
``api.context.entries()`` only.

**Fail-Early.** :class:`TreeStore.append` delegates validation to
``api.append_entry`` (which raises on an empty ``custom_type`` or non-dict data —
no fabricated record). :class:`FileStore` rejects a ``name`` that is not a bare
filename (path separators / ``..`` would escape the state dir), raises
``FileNotFoundError`` when :meth:`FileStore.load` is called on an absent store
with no caller-supplied ``default`` (the first-run empty value is the caller's to
name, never guessed), and lets a corrupt-JSON ``load`` raise rather than silently
resetting real state to empty.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

#: One durable record. ``customEntry.data`` is a JSON object, so a record is a
#: dict by default; a typed :class:`TreeStore` maps it to/from ``T`` via
#: ``encode`` / ``decode``.
T = TypeVar("T")

#: The tree entry KIND :class:`TreeStore` records live in (S39). Kept in sync
#: with ``SessionLog.append_custom_entry`` / ``ConversationTree``.
CUSTOM_ENTRY_KIND = "customEntry"

#: Cross-session file-store root (``FileStore``). Resolved lazily via
#: :func:`_default_state_dir` so tests can redirect ``$HOME`` and never touch a
#: real ``~/.tau``.
STATE_DIR_NAME = "ext-state"


# ── the tree-as-backplane handle ─────────────────────────────────────────────


class _TreeBackplane(Protocol):
    """The slice of ``ExtensionAPI`` :class:`TreeStore` composes.

    In τ, pi's single ``ctx.appendEntry`` splits across two public surfaces: the
    durable write is ``api.append_entry`` and the read-back is
    ``api.context.entries()``. :class:`TreeStore` needs exactly those two — this
    Protocol names them so the store never imports a harness internal and mypy
    still checks the call sites.
    """

    def append_entry(self, custom_type: str, data: dict[str, Any]) -> None: ...

    @property
    def context(self) -> Any:  # exposes ``.entries() -> list[dict[str, Any]]``
        ...


# ── active-path reconstruction (pure, over the raw entry log) ────────────────


def _active_cursor(entries: list[dict[str, Any]]) -> str | None:
    """The current leaf id, replaying the log's own append/navigate algebra.

    Mirrors ``InMemorySessionLog._append`` / ``append_navigate`` (and the on-disk
    ``Session`` it shadows): every appended entry advances the leaf to its own id,
    except a ``navigate`` entry, which moves the leaf to its ``targetId`` (and a
    ``branch_summary``, whose ``_append`` already set the leaf to its own id after
    re-parenting at the branch point). Replaying this over the ordered log yields
    the same cursor the session holds — so the store finds the active path without
    reaching a private ``session_log.cursor`` or importing ``ConversationTree``.
    """
    leaf: str | None = None
    for entry in entries:
        if entry.get("type") == "navigate":
            leaf = entry.get("targetId")
        else:
            leaf = entry.get("id")
    return leaf


def _active_path(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries on the ``parentId`` chain from the cursor to the root, in
    root→leaf order.

    A raw ``parentId`` walk (no compaction/branch-summary splicing — backplane
    state is not model context, so it must survive a compaction that summarizes
    *messages*). A cycle guard mirrors ``ConversationTree._walk`` /
    ``_build_active_path``. Entries off the active branch (abandoned by a navigate
    or fork) are excluded, so :class:`TreeStore` never resurfaces their records.
    """
    by_id = {e["id"]: e for e in entries if "id" in e}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    node_id = _active_cursor(entries)
    while node_id is not None and node_id in by_id and node_id not in seen:
        seen.add(node_id)
        node = by_id[node_id]
        chain.append(node)
        node_id = node.get("parentId")
    chain.reverse()
    return chain


class TreeStore(Generic[T]):
    """Typed, reload-safe records over the durable ``customEntry`` node (S39).

    One store owns one ``custom_type``: :meth:`append` writes a record as a
    durable ``customEntry`` on the active path, and :meth:`load` reconstructs
    every record of that type from ``api.context.entries()`` — filtered to the
    active path and decoded in tree order. Because ``customEntry`` is excluded
    from ``convert_to_llm``, the records are backplane state: durable, rendered in
    the tree, reload-invariant, but never fed to the model. This is the τ
    differentiator the roadmap's §1.1 thesis names — conversation-scoped extension
    state with no side database.

    Records default to plain dicts (a ``customEntry.data`` is a JSON object). For
    genuinely *typed* records, pass ``encode`` (``T -> dict``) and ``decode``
    (``dict -> T``) — e.g. a dataclass with ``asdict`` / its constructor — and the
    store round-trips ``T`` while persisting the dict form.

    The store caches the records it has loaded/appended in :attr:`records`; call
    :meth:`load` to (re)reconstruct from the log — e.g. on ``session_start`` after
    a reload, or when another actor may have appended.
    """

    def __init__(
        self,
        api: _TreeBackplane,
        custom_type: str,
        *,
        encode: Callable[[T], dict[str, Any]] | None = None,
        decode: Callable[[dict[str, Any]], T] | None = None,
    ) -> None:
        if not custom_type:
            raise ValueError(
                "TreeStore: custom_type is required (Fail-Early, no fabricated default)"
            )
        self._api = api
        self.custom_type = custom_type
        self._encode = encode
        self._decode = decode
        self._records: list[T] = []

    # ── read side ────────────────────────────────────────────────────────────

    def _raw_records(self) -> list[dict[str, Any]]:
        """The active-path ``customEntry.data`` dicts for this ``custom_type``."""
        entries: list[dict[str, Any]] = self._api.context.entries()
        out: list[dict[str, Any]] = []
        for entry in _active_path(entries):
            if entry.get("type") != CUSTOM_ENTRY_KIND:
                continue
            if entry.get("customType") != self.custom_type:
                continue
            data = entry.get("data")
            if not isinstance(data, dict):
                raise ValueError(
                    f"TreeStore[{self.custom_type!r}]: entry {entry.get('id')!r} has "
                    f"non-dict data {type(data).__name__} (a corrupt backplane record)"
                )
            out.append(data)
        return out

    def load(self) -> list[T]:
        """Reconstruct and cache every record of this type on the active path.

        Reads ``api.context.entries()``, keeps the active-path ``customEntry``
        nodes for :attr:`custom_type`, and decodes each in tree order. This is the
        reload path: a fresh :class:`TreeStore` over a session reloaded from disk
        returns the same records that were appended before the reload (the S56
        reload-invariance guarantee).
        """
        decode = self._decode
        raw = self._raw_records()
        if decode is None:
            # Untyped store: records are the raw ``customEntry.data`` dicts (T is dict).
            self._records = cast("list[T]", list(raw))
        else:
            self._records = [decode(d) for d in raw]
        return list(self._records)

    @property
    def records(self) -> list[T]:
        """The records loaded/appended so far (a copy). Call :meth:`load` to
        (re)reconstruct from the log."""
        return list(self._records)

    def latest(self) -> T | None:
        """The most recently appended/loaded record, or ``None`` when empty.

        The common "latest snapshot wins" read (e.g. a todo list where each
        append carries the full list): ``store.latest()`` is that snapshot."""
        return self._records[-1] if self._records else None

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Any:
        return iter(list(self._records))

    # ── write side ───────────────────────────────────────────────────────────

    def append(self, record: T) -> None:
        """Persist ``record`` as a durable ``customEntry`` on the active path.

        Encodes to a dict (via ``encode`` when typed) and calls
        ``api.append_entry`` — so the record is flushed to the session log, folds
        onto the tree, and survives a reload. The in-memory :attr:`records` cache
        is kept in sync. Validation (non-empty type, dict data) is enforced by
        ``api.append_entry`` (Fail-Early) — a bad record raises, it is not
        silently dropped.
        """
        data = self._encode(record) if self._encode is not None else record
        if not isinstance(data, dict):
            raise TypeError(
                f"TreeStore[{self.custom_type!r}].append: record must encode to a dict, "
                f"got {type(data).__name__} — pass an ``encode`` for typed records"
            )
        self._api.append_entry(self.custom_type, data)
        self._records.append(record)


# ── the cross-session file store ─────────────────────────────────────────────


def _default_state_dir() -> Path:
    """``~/.tau/ext-state`` — resolved at call time so ``$HOME`` is honored."""
    return Path.home() / ".tau" / STATE_DIR_NAME


#: Sentinel for :meth:`FileStore.load` — distinguishes "no default supplied"
#: (raise on a missing file) from an explicit ``default=None``.
_UNSET: Any = object()


class FileStore:
    """An atomic JSON blob for cross-session extension state.

    Backed by ``<base_dir>/<name>.json`` (default ``~/.tau/ext-state/<name>.json``),
    for state that must outlive one conversation's tree — a cost ledger, a
    cross-session red-team corpus, presets. Unlike :class:`TreeStore` this is NOT
    on the session path and NOT reconstructed from entries; it is a plain
    file the extension owns.

    :meth:`save` is atomic: it writes to a temp file in the *same directory* and
    ``os.replace``-s it into place (an atomic rename on one filesystem), so a
    crash or concurrent reader never observes a half-written blob and an existing
    store is never truncated by a failed serialization.

    :meth:`load` returns the parsed JSON. On a missing file it returns the
    caller-supplied ``default`` (the honest first-run value — e.g. ``load([])`` for
    an accreting corpus) or raises ``FileNotFoundError`` if none was given; a
    corrupt file raises ``json.JSONDecodeError`` rather than silently resetting
    real state (Fail-Early).
    """

    def __init__(self, name: str, *, base_dir: str | os.PathLike[str] | None = None) -> None:
        if not name:
            raise ValueError("FileStore: name is required")
        if name != Path(name).name or name in (".", "..") or os.sep in name or "/" in name:
            raise ValueError(
                f"FileStore: name {name!r} must be a bare filename with no path "
                "separators or '..' (it would escape the state directory)"
            )
        self.name = name
        self._dir = Path(base_dir) if base_dir is not None else _default_state_dir()
        self.path = self._dir / f"{name}.json"

    def exists(self) -> bool:
        """Whether the store file is present on disk."""
        return self.path.exists()

    def load(self, default: Any = _UNSET) -> Any:
        """Read and parse the JSON blob.

        A missing file returns ``default`` when one was supplied, else raises
        ``FileNotFoundError`` — the first-run empty value is the caller's to name,
        never fabricated. A present-but-corrupt file raises (no silent reset).
        """
        if not self.path.exists():
            if default is _UNSET:
                raise FileNotFoundError(
                    f"FileStore[{self.name!r}]: no store at {self.path} and no default "
                    "given (pass ``default=`` to name the first-run value)"
                )
            return default
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, value: Any) -> None:
        """Atomically persist ``value`` as JSON (temp file + ``os.replace``).

        Creates the state directory if needed, serializes to a temp file in that
        same directory, ``fsync``-s it, then renames it over :attr:`path`. If
        serialization fails, the temp file is cleaned up and the existing store is
        left untouched (Fail-Early: a broken write must not corrupt good state).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=f".{self.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
