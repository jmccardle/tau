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

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from tau_llm.docs import agent_facing


@agent_facing(topic="sessions")
@runtime_checkable
class SessionLog(Protocol):
    """The persistence surface ``AgentSession`` reads from and appends to.

    Exactly the methods ``AgentSession`` calls, plus the two cursor-move /
    branch-summary appenders the tree-browser (Part 2) drives through the same
    facade. ``append_model_change`` / ``append_thinking_change`` /
    ``append_session_info`` are deliberately absent — ``AgentSession`` never calls
    them (the TUI/headless call those on the concrete ``Session`` directly), so
    keeping them off the Protocol avoids an unused-method contract (Fail-Early).

    **Precondition: a conversation has exactly one writing process**
    (NODE-ADDRESSABLE-AGENTS.md Decision 6). Concurrency *inside* a conversation is
    lanes — open a :class:`BranchView`, which is a second cursor over the same
    entry log, never a second writer of it. Concurrency *across* processes is
    ``Session.fork(mode="export")`` (``tau_coding_agent.session_store``): a
    verbatim entry copy into a new file with its own header, "self-contained — no
    cross-file chaining," handed to a second process as an independent
    conversation. There is no third option — a second process must never append
    to the same conversation's log a first process is also appending to.

    This is stated as a precondition, not enforced by a guard, a stat check, or an
    id change here. The hazard it heads off is **not** id collision (an earlier
    draft of the design doc said otherwise): ``_generate_entry_id`` retries against
    the log's own id set, so a same-process collision merely redraws, and a
    cross-process collision window is only the entries the other writer added
    since the last load — negligible. The real hazard is that the *cursor* — the
    file store's ``_leaf_id`` — is process-local memory that nothing re-reads: two
    writers both parent their next append off the same node, and the conversation
    silently becomes a fork instead of a line, with ``resolve_cursor`` picking one
    writer's turns on reload and orphaning the other's on disk, unlinked from any
    tree walk. Guarding against that here would mean giving this Protocol a
    liveness check no single implementation needs today; the fix that exists
    (``fork(mode="export")``) already prices out the correct trade at process
    scale (a full copy) against a lane's trade at turn scale (zero copy) — see
    Decision 6 for the full argument.
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

    def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        *,
        summarizer_model_id: str,
        summary_usage: dict[str, int],
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Persist a compaction splice anchor and the provenance of its summary.

        The five keyword arguments are TREE-BROWSER-AS-EDITOR.md §8's decision,
        widened onto this Protocol by §11.3. They are **keyword-only and have no
        defaults**, which is the whole point: every one of them already exists at
        the call site and was being discarded there (§8.1), so a caller that cannot
        name one fails where the value lives rather than recording ``None``. A
        default would make "no provenance recorded" indistinguishable from
        "provenance recorded as unknown" — the swallowed-gap pattern the repo's
        Fail-Early rule exists to prevent (§11.3, rejected option 1).

        - ``summarizer_model_id`` — the id of the model that WROTE ``summary``.
          Not the conversation's model: ``AgentSession._summarizer()``
          (agent_session.py:866) routes a ``local_summarizer`` policy's compaction
          through a different one, so the two genuinely differ and the transcript
          could not previously say which (§8.1).
        - ``summary_usage`` — what generating ``summary`` cost, i.e.
          ``CompactionResult.usage`` (compaction.py:112-119). Compaction summarises
          a full context window and fires automatically, so this is routinely the
          most expensive call in a session; ``tokens_before`` says how big the
          context was, this says what shrinking it charged.
        - ``covered_entries`` / ``covered_tokens`` — the span this anchor removes
          from the fold, as measured at write time: the count of entries and
          :func:`~tau_agent_core.compaction.estimate_span_tokens` over them. Passed
          in rather than recomputed here — see :meth:`append_elide` for why.
        - ``agent_spec_id`` — the id of the ``agent_spec`` ``customEntry`` in force
          over the covered span, from :func:`agent_spec_in_force`. ``None`` is a
          real answer (a pi-imported log, or a store driven without an
          ``AgentSession``, has no such node), not an absent one — the absence of a
          default is what keeps those two cases apart. §8.3: only the ID is
          recorded, never the prompt text, and the record it points at may lag what
          was actually bound, because ``_record_agent_spec`` re-runs at construction
          and ``set_model`` but not at ``load_extensions``.

        ``agent_spec_id`` is deliberately NOT validated against the entry set, unlike
        ``first_kept_id``. The hazards are not comparable: a dangling
        ``first_kept_id`` is never found by the fold's forward scan and silently
        drops the entire kept region from model input, while a dangling
        ``agent_spec_id`` only makes one browser row unhelpful. ``agent_spec`` is a
        RECORD and never a contract (NODE-ADDRESSABLE-AGENTS.md Decision 3), so
        nothing reads it back to reconstruct anything that could then be wrong.
        """
        ...

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Persist a summary-less splice anchor (W3, NODE-ADDRESSABLE-AGENTS.md).

        Same anchor kind ``ConversationTree._active_path_entries`` folds
        ``compaction`` on — "skip the path from here back to ``first_kept_id``" —
        with the ``summary``/``tokens_before`` fields dropped, since there is
        nothing to render. Structured exclusion in tree SHAPE (Decision 2): no new
        per-node flag, no new walker, and a branch whose path never reaches this
        node is completely unaffected (Decision 7 keeps it out of no fold but
        ``context_for`` — ``entries()`` stays total).

        **It takes three of :meth:`append_compaction`'s five provenance arguments,
        not five.** An elide generates no summary, so there is no summarizer model
        and no summary cost; ``summarizer_model_id``/``summary_usage`` would be
        parameters whose only admissible value is a placeholder, which is the same
        swallowed gap §11.3 rejects, wearing symmetry as a disguise. It does have a
        covered span and it does run under an ``agent_spec``, so it takes those.

        ``covered_entries``/``covered_tokens`` close §8.2 — an elide recorded no
        size at all, where a compaction at least recorded ``tokensBefore``. They are
        **passed in, not computed here**, for three reasons. (1) ``covered_tokens``
        is not recoverable from structure at read time (§8.2 says exactly this), so
        it must cross the boundary regardless; computing the count here while the
        token figure comes from the caller would let the two describe different
        spans, with nothing to catch it. (2) The count arithmetic already exists,
        once, in ``ConversationTree._splice_span_phrase`` (conversation_tree.py:552);
        reproducing it in the five stores that implement this Protocol would be five
        copies, and pushing it down here would make every store depend on
        ``ConversationTree``. (3) Both call sites already compute the span and throw
        it away — ``TauBackend.elide_span`` builds the exact ``hidden`` list for its
        no-op refusal check (backends.py) — which is §8.1's pattern verbatim.
        """
        ...

    def append_navigate(self, target_id: str | None) -> str: ...

    def append_branch_summary(self, summary: str, from_id: str | None) -> str: ...

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Append an entry at an EXPLICIT parent.

        The one primitive C2/W14's branch sub-agents need, and the ONLY member this
        Protocol grew for them. Every appender above is "``append_at`` at the current
        leaf, then move the leaf"; this exposes the parent so a second cursor can write
        to the same log without disturbing the first. It does **not** move the store's
        own leaf — a branch's writes must never move the tip of the cursor that spawned
        it.

        Deliberately ONE new member rather than the ``branch()``-per-store shape first
        sketched in the C2 plan: with ``append_at`` in place, the branch handle itself
        (:class:`BranchView`) is storage-agnostic and lives here **once**, instead of
        being reimplemented — and kept in sync — inside each of the three stores. It is
        also the member ``runtime_checkable`` can actually police, since that checks
        member *names*, never signatures: a store that ignored a ``parent_id=`` kwarg
        bolted onto the six existing appenders would pass an ``isinstance`` check while
        silently ignoring the explicit parent.

        It writes NO branch marker. A branch's identity is in-memory
        (:attr:`BranchView.lane`, which the TUI uses as a render-routing key); nothing
        durable distinguishes a sub-agent's entry from a user's fork of the same shape,
        because nothing should — see docs/LANE-REMOVAL.md §1.
        """
        ...


@agent_facing(topic="sessions")
def resolve_cursor(entries: list[dict[str, Any]]) -> str | None:
    """Resolve the persisted cursor (leaf pointer) from the entry log.

    Latest-wins: a trailing ``navigate`` entry points at its ``targetId`` (``None`` =
    pre-root); any other kind points at itself. pi-style logs carry no ``navigate``
    entries, so the cursor is simply the last entry — identical to pi's "fall back to
    last entry" on load (session-manager.ts:855-859). **This is pi parity restored**:
    τ briefly filtered lane-tagged (``branchOf``) entries out of this decision, and no
    longer does.

    Lives here, not on a concrete store, because **every** SessionLog implementation
    (in-memory, file, and any database-backed one) must agree on it exactly — the
    cursor is part of the entry algebra, not of any one durability layer.

    **A guarantee was dropped here, deliberately** (docs/LANE-REMOVAL.md §2). The lane
    filter existed so that a sub-agent's append landing last before a crash could not
    make the next load resume *inside* that branch. τ is an interactive agent, not a
    service that must survive ``pkill`` at an arbitrary instant with perfect
    consistency: if a crash lands you on a branch leaf, the transcript is visible, the
    tree browser moves you, and nothing is corrupted. The filter also encoded a "main
    agent with helpers" model that τ does not hold — one agent moves forwards,
    backwards and sideways through its own history, and there "last write wins" is
    usually the intended continuation rather than an accident (§2).
    """
    if not entries:
        return None
    last = entries[-1]
    if last.get("type") == "navigate":
        target = last.get("targetId")
        return str(target) if target is not None else None
    return str(last["id"])


@agent_facing(topic="sessions")
def agent_spec_in_force(entries: list[dict[str, Any]], leaf_id: str | None) -> str | None:
    """The id of the ``agent_spec`` record governing ``leaf_id``, or ``None``.

    What a splice anchor's ``agentSpecId`` must be set to
    (TREE-BROWSER-AS-EDITOR.md §8.3): the nearest ``agent_spec`` ``customEntry``
    among ``leaf_id``'s ANCESTORS, walking ``parentId`` leaf→root.

    Ancestry, not "the last one this session wrote", for the same reason
    ``ConversationTree._previous_agent_spec`` uses it and docs/LANE-REMOVAL.md §1
    removed the tag that pretended otherwise: a leaf's frame is its ancestor chain
    and nothing else. The two answers diverge exactly where it matters — an elide
    the tree browser aims at a historical anchor is governed by whatever spec was in
    force *there*, which may be two ``set_model`` swaps behind the session's current
    one, and a spec written on a sibling branch never governed this path at all.

    Lives here beside :func:`resolve_cursor`, and for the same reason: it is part of
    the entry algebra every ``SessionLog`` implementation must agree on exactly, not
    a property of any one durability layer. Implemented as a plain ``parentId`` walk
    rather than through ``ConversationTree`` so this module keeps its zero-dependency
    position under the tree, and so the two callers (one in ``tau-agent-core``, one in
    ``tau-coding-agent``) share one spelling.

    Returns ``None`` when no ancestor is an ``agent_spec`` — an honest answer, and a
    reachable one: a pi-imported log has no such node, and neither does a store
    driven directly rather than through ``AgentSession``. §11.3's "no defaults" rule
    is what keeps that answer distinct from a caller who never looked.
    """
    by_id = {str(e["id"]): e for e in entries if e.get("id") is not None}
    current = leaf_id
    visited: set[str] = set()
    while current is not None and current not in visited:
        visited.add(current)  # cycle guard, mirroring ConversationTree._walk
        node = by_id.get(current)
        if node is None:
            return None
        if node.get("type") == "customEntry" and node.get("customType") == "agent_spec":
            return current
        parent = node.get("parentId")
        current = str(parent) if parent is not None else None
    return None


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


@agent_facing(topic="sessions")
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
        # deepcopy, not dict(e): a shallow copy protects the top-level keys and leaves
        # the nested payload SHARED, so `log.entries()[0]["message"]["content"] = ...`
        # mutates the live log — and, in a file-backed store, silently diverges memory
        # from the on-disk JSONL. ctx.entries() promises callers a read-only copy; this
        # is what makes that promise true.
        return copy.deepcopy(self._entries)

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

    def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        *,
        summarizer_model_id: str,
        summary_usage: dict[str, int],
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Fail-Early on an unknown splice anchor, as ``append_navigate`` already does.

        An anchor matching no entry is never found by the tree fold, so the entire kept
        region silently drops out of the context — the worst of the three
        unknown-id cases, because it corrupts model input rather than raising.

        The five provenance keywords land as camelCase payload fields beside the
        three that were already here (see the Protocol for what each records and why
        none of them has a default — TREE-BROWSER-AS-EDITOR.md §8, §11.3).
        """
        if first_kept_id not in self._ids:
            raise ValueError(
                f"compaction first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
            summarizerModelId=summarizer_model_id,
            summaryUsage=dict(summary_usage),
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
        )

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Persist a summary-less splice anchor (W3, NODE-ADDRESSABLE-AGENTS.md):
        the same splice as ``append_compaction``, minus ``summary``/``tokensBefore``.
        Fail-Early for the identical reason ``append_compaction`` validates — an
        anchor matching no entry is never found by ``_active_path_entries``'s
        forward scan, so the ENTIRE kept region silently drops out of the fold.

        Three provenance keywords, not five: §8.2's missing size plus §8.3's frame
        id. There is no summary here, so there is no summarizer and no summary cost
        (see the Protocol)."""
        if first_kept_id not in self._ids:
            raise ValueError(
                f"elide first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
            "elide",
            firstKeptId=first_kept_id,
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
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

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Explicit-parent append (see the Protocol). Does NOT move this log's leaf."""
        if parent_id is not None and parent_id not in self._ids:
            raise ValueError(f"append parent {parent_id!r} not found")
        entry: dict[str, Any] = {
            "type": entry_type,
            "id": _generate_entry_id(self._ids),
            "parentId": parent_id,
            "timestamp": _now_iso(),
            **payload,
        }
        self._entries.append(entry)
        self._ids.add(entry["id"])
        return str(entry["id"])

    def _append(self, kind: str, **payload: Any) -> str:
        """The ordinary append: ``append_at`` the current leaf, then move the leaf."""
        entry_id = self.append_at(self._leaf_id, kind, payload)
        self._leaf_id = entry_id
        return entry_id


@agent_facing(topic="sessions")
class BranchView:
    """A second cursor over ONE underlying log — the branch sub-agent's handle (C2/W14).

    A :class:`SessionLog` in its own right (so an ``AgentSession`` accepts it with **no**
    changes), but not a second log: same ``id``, same ``entries()`` (the whole shared
    list, not a filtered one), same durable storage. What it owns is **its own leaf**.
    Every append it makes goes to the underlying log via ``append_at`` — parented at the
    branch's leaf — and moves only *this* view's cursor, never the spawning one.

    Its writes are **not marked** on disk. :attr:`lane` is an in-memory identity used to
    route this branch's live output (the TUI opens a render lane per branch); it is not
    stamped on the entries, because a durable "this came from a sub-agent" tag makes a
    three-way fork and three sub-agents — structurally identical trees — behave
    oppositely for every reader that asks "does this entry belong to the conversation I
    am looking at?" (docs/LANE-REMOVAL.md §1, §3.2). The answer to that question is
    ancestry from the reader's own cursor, and it is available without any tag.

    Two properties then fall out of the existing fold **for free**, which is the entire
    reason C2 is tractable (JMFTS-INTEGRATION-PLAN.md §9.2):

    - **The sub-agent's context is already correct.** ``AgentSession.messages`` is
      ``ConversationTree(log.entries(), log.cursor).context_for()``. Hand it this view
      and the leaf→root walk from the branch leaf yields exactly the shared conversation
      prefix (down to ``parent_id``) plus the branch's own work. Choosing ``parent_id``
      IS choosing the sub-agent's inherited context. No new context plumbing exists.
    - **Isolation is mutual, and it is structural rather than enforced.** ``context_for``
      walks leaf→root, so a branch's entries are never *ancestors* of the primary leaf
      and cannot leak into the primary context — whatever their kind, and even though
      ``entries()`` returns them. Nothing filters them out; the tree shape means they are
      never on the path.

    ``entries()`` deliberately returns the WHOLE list (branch + primary). It must: the
    ``ConversationTree`` fold resolves ``parentId`` by dict lookup, so hiding the primary
    prefix from a branch would break the very walk that gives it its context.
    """

    def __init__(self, log: SessionLog, parent_id: str | None, *, lane: str, label: str) -> None:
        self._log = log
        self._leaf_id = parent_id
        self._lane = lane
        self._label = label

    @property
    def id(self) -> str:
        """The UNDERLYING session's id — a branch is a lane in one conversation, not a
        second conversation. (Its own identity is :attr:`lane`.)"""
        return self._log.id

    @property
    def lane(self) -> str:
        """This branch's lane id — an IN-MEMORY identity, never written to an entry.

        It names the branch on the ``branch_event``/``branch_end`` channels, which is
        how the TUI's ``RenderRouter`` keeps two concurrent sub-agents' output in two
        visual lanes. Nothing reads it back off disk (docs/LANE-REMOVAL.md §5)."""
        return self._lane

    @property
    def label(self) -> str:
        """Human label for this branch (what the sub-agent was spawned to do)."""
        return self._label

    @property
    def cursor(self) -> str | None:
        """This branch's leaf — independent of the underlying log's primary cursor."""
        return self._leaf_id

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    def _ids(self) -> set[str]:
        return {str(e["id"]) for e in self._log.entries()}

    def append_at(
        self,
        parent_id: str | None,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Pass through to the underlying log — a branch adds nothing to the entry."""
        return self._log.append_at(parent_id, entry_type, payload)

    def _append(self, entry_type: str, **payload: Any) -> str:
        entry_id = self.append_at(self._leaf_id, entry_type, payload)
        self._leaf_id = entry_id
        return entry_id

    def append_message(self, message: dict[str, Any]) -> str:
        return self._append("message", message=message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        return self._append("customMessage", customType=custom_type, message=message)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        return self._append("customEntry", customType=custom_type, data=data)

    def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        *,
        summarizer_model_id: str,
        summary_usage: dict[str, int],
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Fail-Early on an unknown anchor, exactly as the concrete stores do — a
        compaction whose ``firstKeptId`` names nothing silently drops the whole kept
        region from the fold rather than raising.

        A branch adds nothing to the provenance and hides nothing from it: the five
        §8 fields are written verbatim, exactly as ``append_at`` writes a branch's
        entries with no marker of their own (docs/LANE-REMOVAL.md §1)."""
        if first_kept_id not in self._ids():
            raise ValueError(
                f"compaction first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
            "compaction",
            summary=summary,
            firstKeptId=first_kept_id,
            tokensBefore=tokens_before,
            summarizerModelId=summarizer_model_id,
            summaryUsage=dict(summary_usage),
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
        )

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        """Fail-Early on an unknown anchor, exactly as ``append_compaction`` does —
        see :class:`InMemorySessionLog` for why a dangling anchor is the worst of
        the unknown-id cases rather than merely a rejected call."""
        if first_kept_id not in self._ids():
            raise ValueError(
                f"elide first_kept_id {first_kept_id!r} not found; the splice anchor "
                "must name a real entry, or the whole kept region silently drops out of "
                "the context fold"
            )
        return self._append(
            "elide",
            firstKeptId=first_kept_id,
            coveredEntries=covered_entries,
            coveredTokens=covered_tokens,
            agentSpecId=agent_spec_id,
        )

    def append_navigate(self, target_id: str | None) -> str:
        """Move THIS branch's leaf. The primary cursor is untouched."""
        if target_id is not None and target_id not in self._ids():
            raise ValueError(f"navigate target {target_id!r} not found")
        entry_id = self._append("navigate", targetId=target_id)
        self._leaf_id = target_id
        return entry_id

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        """Re-parent to the branch point before appending (pi ``branchWithSummary``)."""
        if from_id is not None and from_id not in self._ids():
            raise ValueError(f"branch_summary from {from_id!r} not found")
        self._leaf_id = from_id
        return self._append("branch_summary", summary=summary, fromId=from_id)


@agent_facing(topic="sessions")
def open_branch(log: SessionLog, parent_id: str | None, *, label: str) -> BranchView:
    """Open a new branch lane over ``log``, rooted at ``parent_id``.

    ``parent_id`` chooses the sub-agent's inherited context (the fold walks up from it),
    and must name a real entry — Fail-Early, since a dangling branch root would give the
    sub-agent an empty or wrong context with no error.

    The lane id is freshly generated per call, NOT derived from ``parent_id``. Two
    sub-agents spawned from the SAME parent (the common fan-out shape — several
    evaluators over one retrieval result) would otherwise be indistinguishable on the
    live ``branch_event`` channel, and their output would interleave in one render lane.
    It is a runtime routing key only; nothing durable carries it.
    """
    if parent_id is not None and parent_id not in {str(e["id"]) for e in log.entries()}:
        raise ValueError(
            f"cannot open a branch at {parent_id!r}: no such entry. The branch root "
            "chooses the sub-agent's inherited context; a dangling one would hand it a "
            "wrong context rather than fail."
        )
    return BranchView(log, parent_id, lane=uuid.uuid4().hex[:8], label=label)
