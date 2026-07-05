"""τ-agent-core session_log: the persistence FACADE ``AgentSession`` depends on.

``SessionLog`` is the small, structural interface ``AgentSession`` uses to (a)
read the raw append-only entry log + its cursor (to rebuild context via
:class:`~tau_agent_core.conversation_tree.ConversationTree`) and (b) append this
turn's messages / a compaction boundary / a cursor move. It is the layering seam
that lets ``AgentSession`` live in ``tau-agent-core`` while persisting through the
coding-agent's file ``Session`` on the live path — without ``tau-agent-core``
importing ``tau-coding-agent`` (that import would be circular).

Two implementations satisfy it:

- ``tau_coding_agent.session_store.Session`` — the authoritative on-disk log the
  TUI (``app.py``) and headless (``headless.py``) already own; injected on the
  live path (``TauBackend``/headless). It satisfies this Protocol *structurally*
  (same method names / signatures), so nothing is relocated.
- :class:`InMemorySessionLog` (below) — the SDK-default log for
  ``create_agent_session()`` with no session. It is NOT a second on-disk file
  format (there is still one write path, ``§4.5``): it is the "``path is None``"
  in-memory mode expressed as a first-class core object, producing entries whose
  shape is byte-identical to ``Session``'s so ``ConversationTree`` folds both the
  same way.

This is also the Part-3 ``§4.4`` DB-seam boundary, forward-delivered: a fork's
database-backed store satisfies the same surface.

Reference: SESSION-TREE-IMPLEMENTATION.md §2.6 (wiring), §4.1-§4.4 (the seam),
"Decision 4" RESOLVED option (B) (§5).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionLog(Protocol):
    """The persistence surface ``AgentSession`` reads from and appends to.

    Exactly the methods ``AgentSession`` calls, plus the two cursor-move /
    branch-summary appenders the tree-browser (Part 2) drives through the same
    facade. ``append_model_change`` / ``append_thinking_change`` /
    ``append_session_info`` are deliberately absent — ``AgentSession`` never calls
    them (the TUI/headless call those on the concrete ``Session`` directly), so
    keeping them off the Protocol avoids an unused-method contract (Fail-Early).
    """

    @property
    def id(self) -> str:
        """Stable session identity (a UUID — never a filesystem path, §4.2)."""
        ...

    @property
    def cursor(self) -> str | None:
        """The current leaf (tip) entry id; ``None`` before the first entry."""
        ...

    def entries(self) -> list[dict[str, Any]]:
        """The ordered, append-only raw entries (all kinds), in load order."""
        ...

    def append_message(self, message: dict[str, Any]) -> str: ...

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str: ...

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str: ...

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str: ...

    def append_navigate(self, target_id: str | None) -> str: ...

    def append_branch_summary(self, summary: str, from_id: str | None) -> str: ...


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with ms precision + ``Z``.

    Mirrors ``session_store._now_iso`` so in-memory and on-disk entries carry an
    identically-shaped ``timestamp`` (JS ``new Date().toISOString()``)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _generate_entry_id(existing: set[str]) -> str:
    """8-hex collision-checked entry id (mirrors ``session_store._generate_entry_id``)."""
    for _ in range(100):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
    return uuid.uuid4().hex  # pragma: no cover — 100 collisions is astronomically unlikely


class InMemorySessionLog:
    """A minimal, RAM-only :class:`SessionLog` for the SDK default path.

    The append algebra (parentId chaining off the current leaf, 8-hex ids,
    latest-wins cursor, navigate moving the tip to its target) is exactly
    ``session_store.Session._append``/``append_navigate`` — but with no disk
    flush. Entries are camelCase (``parentId``/``firstKeptId``/``fromId``) so
    :class:`~tau_agent_core.conversation_tree.ConversationTree` reads them the
    same as an on-disk ``Session``. No header, no system message, no file: a
    fresh log has zero entries (``messages == []``) until the first append.
    """

    def __init__(self, id: str | None = None) -> None:
        self._id = id if id is not None else uuid.uuid4().hex
        self._entries: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._leaf_id: str | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def cursor(self) -> str | None:
        return self._leaf_id

    def entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._entries]

    def append_message(self, message: dict[str, Any]) -> str:
        return self._append("message", message=message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        """Persist an extension-injected custom message as a ``customMessage`` node.

        The durable form of a ``before_agent_start`` injection (E5 §3.1 / S29):
        its own tree entry KIND, carrying the stored ``message`` (``role:
        "custom"``) plus the top-level ``customType`` (the extension-origin
        identity). ``ConversationTree`` folds it onto the active path like a
        ``message`` entry (it is not a splice anchor) and the wire remaps
        custom→user, so the injected content reaches the model and survives a
        reload byte-identically."""
        return self._append("customMessage", customType=custom_type, message=message)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        """Persist a durable, NON-message ``customEntry`` node (E6 §2 / S39).

        The reloadable backing for ``api.append_entry`` (formerly the RAM-only
        registry ``_entry_store``, lost on restart — G4). It carries the extension's
        ``{customType, data}`` as its own tree entry KIND — deliberately NOT a
        ``message``/``customMessage``, so :class:`~tau_agent_core.conversation_tree.ConversationTree`
        never folds it into the loop context and ``convert_to_llm`` never sees it:
        it is tree-as-backplane state, on the durable path and readable through
        ``ctx.entries()``, but excluded from model input. Folds onto the active path
        like any node (it advances the leaf); the exclusion is that ``context_for``
        emits no message for it (conversation_tree.py). The foundation S56's
        ``TreeStore`` reconstructs from ``ctx.entries()`` on reload."""
        return self._append("customEntry", customType=custom_type, data=data)

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int) -> str:
        return self._append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
        )

    def append_navigate(self, target_id: str | None) -> str:
        """Persist a cursor move; the leaf advances to ``target_id`` (not to the
        navigate entry itself), mirroring ``Session.append_navigate``. Fail-Early:
        a non-``None`` target must name a real entry."""
        if target_id is not None and target_id not in self._ids:
            raise ValueError(f"navigate target {target_id!r} not found")
        entry_id = self._append("navigate", targetId=target_id)
        self._leaf_id = target_id
        return entry_id

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Move the leaf to ``from_id`` (the branch point) then append, mirroring
        ``Session.append_branch_summary`` (session_store.py:433) and pi
        ``branchWithSummary`` (session-manager.ts:1272): the summary parents at the
        branch point so the abandoned children become a sibling branch that drops
        out of ``context_for`` via the ``parentId`` walk. Without this re-parent the
        summary would append off the *current* leaf and the abandoned branch would
        stay on the active path — the exact divergence ``ctx.summarize_branch``
        (E3-ctx / S19) exposed on the SDK/in-memory path.

        Fail-Early: a non-``None`` ``from_id`` must name a real entry (parity with
        ``Session.append_branch_summary``)."""
        if from_id is not None and from_id not in self._ids:
            raise ValueError(f"branch_summary from {from_id!r} not found")
        self._leaf_id = from_id  # branch point, not the current leaf (pi :1272)
        return self._append("branch_summary", summary=summary, fromId=from_id)

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
        return str(entry["id"])
