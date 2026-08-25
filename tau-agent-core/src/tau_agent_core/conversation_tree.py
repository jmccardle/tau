"""τ-agent-core conversation tree: the pure, I/O-free session-tree algebra.

``ConversationTree`` is a side-effect-free function of ``(entries, cursor)`` over
the raw ``session_store.Session.entries()`` dicts (camelCase ``parentId`` /
``firstKeptId`` / ``fromId``). It owns the *interpretive fold* — the leaf→root
``parentId`` walk plus the read-time compaction / ``branch_summary`` splice — that
turns the persisted branching tree into the flat message list the agent loop
consumes, without touching the filesystem, ``Session``, or ``asyncio``.

Provenance (ported verbatim, only the field names reconciled camelCase):

- ``context_for`` / ``_active_path_entries`` ← pi ``buildSessionContext``
  (``session-manager.ts:325-423``) — the leaf→root ``parentId`` walk, the "anchor on
  the LAST **compaction** in the path" rule (``:367, :400-423``), and the splice that
  emits the summary node, the kept entries before it from ``firstKeptId``, then
  everything after. Only ``compaction`` drops a prefix; ``branch_summary`` is a plain
  INLINE node (pi ``createBranchSummaryMessage``, ``:390-397``) whose ``fromId`` is
  display metadata, *not* a splice boundary — the abandoned branch drops out purely
  via the ``parentId`` walk, because ``branchWithSummary`` parents the summary at the
  branch point (Decision 5, §5). This reads a compaction appended at the tip
  (append-only, step 1c) as well as one whose kept region trails it; the entry→message
  conversion mirrors ``SessionManager.get_active_messages`` (``:191-221``).
- ``elide`` (W3, NODE-ADDRESSABLE-AGENTS.md) generalizes the anchor: the SAME splice
  shape as ``compaction`` (last-anchor-wins, drop the pre-``firstKeptId`` span) but
  with nothing to render — no ``summary`` field, so it contributes no message at all.
  Structured exclusion with nothing erased, per Decision 2: the exclusion lives in
  *where the node sits on the path*, not in a per-node flag every walker would need
  to be taught. Since it is additive (no existing appender ever produces one), it
  changes nothing about ``compaction``'s own behaviour.
- ``tree`` ← pi ``getTree(): SessionTreeNode[]`` (``session-manager.ts:1191``):
  parent/child nodes, children sorted by timestamp, ``is_leaf`` == the cursor.
- ``subtree_text`` ← ``SessionManager._extract_branch_messages``
  (``session_manager.py:627-702``).
- ``navigate`` ← pi ``branch(id)`` (``session-manager.ts:1241``) — cursor move only.

Reference: SESSION-TREE-IMPLEMENTATION.md §2.1, §2.5, §2.7 (step 1a); §5 Decision 5
(branch_summary is an inline node, not a splice anchor — step 2);
EXTENSIONS-ORCHESTRATION-PLAN.md §4 (tree-as-truth).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Kinds that carry a ``summary`` field (for previews + subtree extraction). NOTE:
# ``compaction`` is a splice anchor AND has a summary; ``branch_summary`` has a
# summary but renders INLINE, never splicing (Decision 5, §5) — pi's
# ``buildSessionContext`` sets its ``compaction`` local solely from
# ``entry.type === "compaction"`` (``session-manager.ts:367``). This tuple is
# therefore about "has a summary to show", not "drops a prefix"; splice-anchor-hood
# is the separate, orthogonal ``_SPLICE_ANCHOR_KINDS`` below (``elide`` is a splice
# anchor with NO summary, so it belongs to that tuple and not to this one).
_SUMMARY_KINDS = ("compaction", "branch_summary")

# Kinds the fold in ``_active_path_entries`` treats as a splice ANCHOR — "skip from
# here back to firstKeptId" (W3, NODE-ADDRESSABLE-AGENTS.md §3). ``compaction`` is
# the original, summary-bearing anchor; ``elide`` is its summary-less generalization
# (Decision 2: exclusion is tree shape, not a per-node flag — so it is simply a
# second entry KIND the same one fold already knows how to anchor on, not a new
# walker). Last anchor in the path wins, exactly as it always has for compaction
# alone: with no ``elide`` entries present this tuple degrades to exactly the old
# ``"compaction"``-only check, so compaction's own behaviour is unchanged.
_SPLICE_ANCHOR_KINDS = ("compaction", "elide")

# The verb each splice anchor's browser row uses for the span it removes from the
# fold. Two verbs, deliberately: an ``elide`` HIDES its span (``context_for``'s
# ``elide`` case renders nothing in its place), while a ``compaction`` FOLDS its
# span into the summary it renders instead. TREE-BROWSER-AS-EDITOR.md uses exactly
# that pair for exactly that distinction (§1.2 "hides 42 entries", §4.1 "folds 3
# entries"), so the verb alone tells a reader scanning rows whether the removed
# span left anything behind. Keyed by entry ``type``, so a kind added to
# ``_SPLICE_ANCHOR_KINDS`` without a verb raises here rather than rendering a row
# that silently omits its span.
_SPLICE_VERBS = {"compaction": "folds", "elide": "hides"}


def _compaction_message(summary: str) -> dict[str, Any]:
    """Render a compaction anchor as the loop-consumable user message. Mirrors
    ``SessionManager.get_active_messages`` (``session_manager.py:208-220``) so the
    fold parity test holds."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"[[Compaction summary: {summary}]]"}],
    }


def _branch_summary_message(summary: str) -> dict[str, Any]:
    """Render a ``branch_summary`` as a plain INLINE user message (pi
    ``createBranchSummaryMessage`` → ``convertToLlm`` branchSummary case,
    ``messages.ts:100, 169``). Unlike a compaction it does NOT splice out a
    prefix — it sits in the path exactly where it was appended (Decision 5, §5)."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"[[Branch summary: {summary}]]"}],
    }


def _message_text(message: dict[str, Any]) -> str:
    """Flatten a message's text content to a plain string (for previews /
    subtree extraction). Ported from the block walk in
    ``_extract_branch_messages`` (``session_manager.py:669-685``)."""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "toolCall":
                name = block.get("name", "unknown")
                args = block.get("arguments", {})
                parts.append(f"[tool_call: {name}({args})]")
            elif kind == "thinking":
                parts.append(f"[thinking: {block.get('thinking', '')}]")
            elif kind == "image":
                parts.append("[image]")
        return "".join(parts)
    return ""


def _spec_model_id(data: dict[str, Any]) -> str:
    """The model id recorded in an ``agent_spec`` payload (W2), or a stated absence.

    ``AgentSession._record_agent_spec`` writes ``AgentSession.get_model()`` — the
    ``{id, provider, context_window}`` projection — so the id is one level down. A
    payload that has no model at all says so rather than rendering ``None``: this is
    a browser row, and "(no model recorded)" is a readable statement about a
    hand-written or future log, while ``None`` reads like a real model named None.
    """
    model = data.get("model")
    if isinstance(model, dict):
        value = model.get("id")
        return str(value) if value is not None else "(no model recorded)"
    return str(model) if model is not None else "(no model recorded)"


def _spec_tools(data: dict[str, Any]) -> list[str]:
    """The tool names recorded in an ``agent_spec`` payload (W2), order preserved."""
    tools = data.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(t) for t in tools]


def _tools_phrase(tools: list[str]) -> str:
    """``"4 tools: read, write, edit, bash"`` — the absolute (non-delta) tool summary.

    Truncated at four names because this is one row of a tree browser, and the
    count in front is what makes the truncation honest: "12 tools: read, write,
    edit, bash +8 more" still answers "read-only reviewer or full-tool builder?",
    which is the question W2 exists to answer.
    """
    if not tools:
        return "no tools"
    noun = "tool" if len(tools) == 1 else "tools"
    shown = ", ".join(tools[:4])
    extra = len(tools) - 4
    suffix = f" +{extra} more" if extra > 0 else ""
    return f"{len(tools)} {noun}: {shown}{suffix}"


@dataclass
class TreeNode:
    """A node in the browsable session tree (pi ``SessionTreeNode``)."""

    id: str
    parent_id: str | None
    kind: str  # message | compaction | branch_summary | navigate | …
    role: str | None  # for message nodes
    preview: str  # first line of text (browser row)
    is_leaf: bool  # == the current cursor
    children: list[TreeNode] = field(default_factory=list)


class ConversationTree:
    """Pure, I/O-free view over an append-only session entry log + a cursor.

    ``entries`` are ``session_store``-shaped dicts (camelCase ``parentId``); the
    log is never mutated — ``navigate`` only moves the in-memory cursor.
    """

    def __init__(self, entries: list[dict[str, Any]], cursor: str | None) -> None:
        self._entries = entries  # append-only, load order
        self._by_id: dict[str, dict[str, Any]] = {e["id"]: e for e in entries}
        self._children: dict[str | None, list[str]] = {}
        for e in entries:
            self._children.setdefault(e.get("parentId"), []).append(e["id"])
        self._cursor = cursor  # leaf pointer (None = pre-root fallback to root)

    # --- navigation (cursor only; nothing is deleted or rewritten) ---------

    @property
    def cursor(self) -> str | None:
        return self._cursor

    def navigate(self, entry_id: str | None) -> None:
        """Move the cursor to ``entry_id`` (pi ``branch``). Raises if unknown."""
        if entry_id is not None and entry_id not in self._by_id:
            raise KeyError(f"Entry {entry_id} not found")
        self._cursor = entry_id

    def path(self, leaf: str | None = None) -> list[dict[str, Any]]:
        """The raw leaf→root entry chain, reversed to root→leaf order.

        No splicing — every entry on the ``parentId`` chain (all kinds). A cycle
        guard mirrors ``_build_active_path`` (``session_manager.py:571-579``).
        ``leaf=None`` uses the stored cursor.
        """
        leaf_id = self._cursor if leaf is None else leaf
        return list(self._walk(leaf_id))

    def fork_admission_reason(self, target_id: str | None) -> str | None:
        """Whether ``target_id`` is safe to fork FROM, or the reason it is not.

        docs/SUBMISSION-LIFECYCLE.md's concrete admission check for
        ``multitask_strategy="fork"``: a fork point must be a TURN-COMPLETE entry.
        Forking at an assistant message whose ``toolCall`` blocks have no matching
        ``toolResult`` on this path yields a prefix most providers reject outright
        — a chat-completions turn cannot end on an assistant message that declares
        tool calls with no results attached, and ``BranchView``'s ancestors-only
        walk (I1, NODE-ADDRESSABLE-AGENTS.md §2) means a toolResult appended AFTER
        ``target_id`` (a descendant) can never rescue it — there is no "wait for
        the rest of the turn to land" here, only "this point was, or was not,
        already complete when it was appended".

        ``target_id=None`` (fork before the root) is trivially safe: there is no
        context yet, so nothing can be pending.

        Walks the RAW root→target ancestor chain (:meth:`path`, not
        :meth:`context_for`'s compaction/elide-spliced view — compaction never
        cuts mid-tool-call, see ``compaction.find_valid_cut_points``'s "toolResult:
        not a cut point", so the two views agree on this question), tracking the
        most recently seen assistant message's outstanding tool_call ids and
        clearing each as its ``toolResult`` is walked past. Whatever is still
        outstanding at ``target_id`` is the gap.

        Returns a human-readable rejection reason, or ``None`` if ``target_id`` is
        a safe fork point.

        Raises:
            ValueError: ``target_id`` names no entry — Fail-Early, mirroring
                :func:`~tau_agent_core.session_log.open_branch`'s own check on the
                same value (a dangling fork point would hand the second agent an
                empty or wrong context with no error).
        """
        if target_id is None:
            return None
        if target_id not in self._by_id:
            raise ValueError(f"fork point {target_id!r} not found")

        pending: dict[str, str] = {}  # tool_call_id -> tool name, for the message
        for entry in self.path(target_id):
            if entry.get("type") != "message":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                # A NEW assistant message resets what is outstanding — only the
                # MOST RECENT assistant turn's tool calls can still be pending by
                # the time a well-formed log reaches `target_id`.
                content = message.get("content", [])
                pending = {
                    str(block["id"]): str(block.get("name", "?"))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "toolCall"
                }
            elif role == "toolResult":
                tool_call_id = message.get("tool_call_id")
                if tool_call_id is not None:
                    pending.pop(str(tool_call_id), None)

        if not pending:
            return None
        names = ", ".join(f"{name}({tool_call_id})" for tool_call_id, name in pending.items())
        return (
            f"fork point {target_id!r} is not turn-complete: outstanding tool call(s) "
            f"{names} have no matching toolResult on this path. Forking here would "
            "hand the second agent a prefix ending in an assistant message that "
            "declares tool calls with no results — most providers reject that "
            "outright."
        )

    # --- the interpretive fold (port of _build_active_path:544-625) --------

    def context_for(self, leaf: str | None = None) -> list[dict[str, Any]]:
        """Root→leaf message list with compaction/branch_summary splices applied.

        The entry-level fold is ``_build_active_path`` (anchor on the LAST summary
        in the path; drop kept-region entries whose linear order precedes the
        boundary); the entry→message conversion is ``get_active_messages``.
        ``leaf=None`` uses the stored cursor.
        """
        leaf_id = self._cursor if leaf is None else leaf
        messages: list[dict[str, Any]] = []
        for entry in self._active_path_entries(leaf_id):
            kind = entry.get("type")
            if kind == "message":
                messages.append(entry.get("message", {}))
            elif kind == "customMessage":
                # Extension-injected durable node (E5 §3.1 / S29): the stored
                # message carries ``role: "custom"`` (rendered as extension-origin);
                # it folds onto the path like a plain message (NOT a splice anchor)
                # and is remapped custom→user at the wire (agent_loop convert_to_llm).
                messages.append(entry.get("message", {}))
            elif kind == "compaction":
                messages.append(_compaction_message(str(entry.get("summary", ""))))
            elif kind == "branch_summary":
                # Inline node, not a splice (Decision 5, §5): rendered in place.
                messages.append(_branch_summary_message(str(entry.get("summary", ""))))
            elif kind == "elide":
                # Summary-less splice anchor (W3): it occupies the anchor slot in
                # ``_active_path_entries`` exactly like ``compaction`` so the SAME
                # splice runs, but there is no summary text to inject — so, unlike
                # ``compaction``, it renders NOTHING. The excluded span is gone from
                # this fold; the anchor and everything it hid stay fully present in
                # ``entries()`` (Decision 7, T5).
                pass
            # ``customEntry`` (durable extension backplane state, E6 §2 / S39) is
            # deliberately NOT rendered here: it is a non-message node on the path,
            # so it emits no loop message and thus never reaches ``convert_to_llm``
            # / the model. It stays readable through ``ctx.entries()`` and the tree.
        return messages

    def context_entries(self, leaf: str | None = None) -> list[dict[str, Any]]:
        """Root→leaf *entry* list with the compaction/branch_summary splice applied.

        The entry-level counterpart of :meth:`context_for` (which converts these
        to loop messages). This is exactly what ``SessionManager._build_active_path``
        returned, so it feeds ``compaction.prepare_compaction`` unchanged — the
        AgentSession compaction path builds it over the live entries instead of
        the retired System-A manager (§2.6). ``leaf=None`` uses the stored cursor.
        """
        leaf_id = self._cursor if leaf is None else leaf
        return self._active_path_entries(leaf_id)

    def _active_path_entries(self, leaf_id: str | None) -> list[dict[str, Any]]:
        """Faithful side-effect-free port of pi ``buildSessionContext``
        (``session-manager.ts:325-423``) over camelCase entries: leaf→root walk,
        then the compaction/elide splice. ``branch_summary`` is NOT an anchor — it
        stays on the path as a plain inline entry (Decision 5, §5)."""
        entries = self._entries
        if not entries:
            return []

        # Walk backwards from the leaf (falling back to the root entry when the
        # cursor is unset), then reverse to root→leaf order (``:568-582``).
        path = self._walk(leaf_id or entries[0]["id"])

        # Anchor on the LAST (most recent) splice-anchor entry in the path —
        # pi sets its ``compaction`` local solely from ``entry.type === "compaction"``
        # (``:367``); ``elide`` (W3) is the summary-less generalization of the same
        # anchor kind, so it is folded in here rather than taught to a second walker
        # (Decision 2). With iterative compaction/elision each new anchor supersedes
        # the earlier ones, so anchoring on the last drops the stale summaries/spans
        # and their kept regions alike. ``branch_summary`` is deliberately excluded
        # (Decision 5).
        anchor_idx: int | None = None
        for idx, entry in enumerate(path):
            if entry.get("type") in _SPLICE_ANCHOR_KINDS:
                anchor_idx = idx

        if anchor_idx is None:
            return path

        anchor = path[anchor_idx]
        boundary_value = anchor.get("firstKeptId")
        boundary = str(boundary_value) if boundary_value is not None else None

        # pi ``buildSessionContext`` (``:400-423``): emit the anchor node, then the
        # kept entries BEFORE it starting at ``firstKeptId``, then every entry AFTER
        # it. Correct whether the anchor was appended at the tip (append-only: the
        # boundary is an ancestor, so the kept region precedes the anchor) or its
        # kept region trails it — the frozen System-A oracle's shape. Identical for
        # ``elide``: the boundary search does not care whether the anchor renders a
        # summary, only where it sits.
        result: list[dict[str, Any]] = [anchor]
        found = False
        for entry in path[:anchor_idx]:
            if entry["id"] == boundary:
                found = True
            if found:
                result.append(entry)
        result.extend(path[anchor_idx + 1 :])
        return result

    def _walk(self, start_id: str | None) -> list[dict[str, Any]]:
        """Leaf→root ``parentId`` walk with a cycle guard, reversed to root→leaf."""
        path: list[dict[str, Any]] = []
        current_id = start_id
        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            node = self._by_id.get(current_id)
            if node is None:
                break
            path.append(node)
            parent = node.get("parentId")
            current_id = str(parent) if parent is not None else None
        path.reverse()
        return path

    # --- UI + subtree ops --------------------------------------------------

    def entry(self, entry_id: str) -> dict[str, Any]:
        """The raw log entry ``entry_id`` names.

        Exists for the tree browser's detail pane, which renders a node's FULL
        body while :class:`TreeNode` carries only the one-line ``preview`` the
        browser row needs. Widening ``TreeNode`` instead would push the whole
        message onto every consumer of :meth:`tree` — the RPC surface included —
        to serve one pane.

        Raises:
            KeyError: no entry has that id. The browser builds its rows from these
                same entries, so a miss is a broken index, not a missing body.
        """
        return self._by_id[entry_id]

    def message_text(self, entry_id: str) -> str:
        """``entry_id``'s message flattened to plain text, or ``""`` if it has none.

        The whole body, where :attr:`TreeNode.preview` is its first line elided to
        a row. Added for the tree browser's ``revise`` gesture (PLAN-0.9.4 §4,
        item 2), which puts a user message back in the input to be edited — a
        preview would hand back a truncated version of what the reader typed.

        ``""`` for an entry with no message at all (a ``navigate``, an
        ``agent_spec``) — those are records, not text, and the caller asking for
        one is asking about the wrong node rather than hitting an error.

        Raises:
            KeyError: no entry has that id, same as :meth:`entry`.
        """
        return _message_text(self._by_id[entry_id].get("message", {}))

    def tree(self) -> list[TreeNode]:
        """Parent/child ``TreeNode`` roots for the browser (pi ``getTree``).

        A well-formed session has one root (first entry with ``parentId is None``);
        orphaned entries (broken parent chain) are also returned as roots. Each
        node's children are sorted by timestamp (oldest first); ``is_leaf`` marks
        the current cursor. Roots keep load order.
        """
        nodes: dict[str, TreeNode] = {}
        for entry in self._entries:
            nodes[entry["id"]] = TreeNode(
                id=entry["id"],
                parent_id=entry.get("parentId"),
                kind=str(entry.get("type", "")),
                role=self._role_of(entry),
                preview=self._preview_of(entry),
                is_leaf=entry["id"] == self._cursor,
            )

        roots: list[TreeNode] = []
        for entry in self._entries:
            node = nodes[entry["id"]]
            parent_id = entry.get("parentId")
            if parent_id is None or parent_id == entry["id"]:
                roots.append(node)
                continue
            parent = nodes.get(parent_id)
            if parent is None:
                roots.append(node)  # orphan → treat as a root
            else:
                parent.children.append(node)

        # Sort children by timestamp, iteratively (deep-tree safe, pi :1229-1235).
        stack = list(roots)
        while stack:
            node = stack.pop()
            node.children.sort(key=self._timestamp_key)
            stack.extend(node.children)
        return roots

    def _timestamp_key(self, node: TreeNode) -> Any:
        entry = self._by_id.get(node.id, {})
        return entry.get("timestamp", 0)

    def _role_of(self, entry: dict[str, Any]) -> str | None:
        # A ``customMessage`` (extension-injected node, §3.1) carries its role in
        # the stored message too, so the tree browser tags it ``custom`` (not a
        # literal ``user`` turn).
        if entry.get("type") not in ("message", "customMessage"):
            return None
        role = entry.get("message", {}).get("role")
        return str(role) if role is not None else None

    def _preview_of(self, entry: dict[str, Any]) -> str:
        kind = entry.get("type")
        if kind in ("message", "customMessage"):
            text = _message_text(entry.get("message", {}))
        elif kind in _SPLICE_ANCHOR_KINDS:
            # BEFORE the ``_SUMMARY_KINDS`` arm: ``compaction`` is in both tuples and
            # the anchor rendering is the more specific one (it states the span AND
            # the summary). ``branch_summary`` is in ``_SUMMARY_KINDS`` only — it is
            # not an anchor and has no span (Decision 5, §5).
            text = self._splice_anchor_preview(entry)
        elif kind in _SUMMARY_KINDS:
            text = str(entry.get("summary", ""))
        elif kind == "customEntry":
            if entry.get("customType") == "agent_spec":
                text = self._agent_spec_preview(entry)
            else:
                # Backplane state (E6 §2 / S39): label the browser row by its customType.
                text = f"customEntry: {entry.get('customType', '')}"
        else:
            text = ""
        stripped = text.strip()
        return stripped.split("\n", 1)[0] if stripped else ""

    def _splice_anchor_preview(self, entry: dict[str, Any]) -> str:
        """Row text for a splice anchor: WHAT it removes, then WHAT it left behind.

        TREE-BROWSER-AS-EDITOR.md §4.2. Both members of ``_SPLICE_ANCHOR_KINDS`` get
        the same arithmetic, because the span is a property of *where the anchor
        sits*, not of whether it renders a summary — until now the kind with no
        payload (``elide``) had the better row and the kind with a summary
        (``compaction``) said nothing at all about the entries it replaced (§1.2).

        **Composition: the computed span FIRST, the summary after an em dash.** Two
        reasons, both about truncation. :meth:`_preview_of` keeps only the first
        line, and the browser then elides the row to the width left over after the
        indent (``app.py:736``); the span phrase is bounded (a count and one id)
        while a summary is a paragraph, so summary-first would let the very fact
        this change adds be the part that gets cut — and a multi-line summary would
        delete it outright at the ``split("\\n")``. An ``elide`` has no summary, so
        it degrades to exactly the span phrase and its row is unchanged.

        The span itself is the difference between :meth:`context_entries` at the
        parent and the kept region ``firstKeptId``…parent — the arithmetic
        :meth:`_active_path_entries` performs, read back out. Three shapes, because
        the two stores disagree about where the kept region sits (§4.1):

        1. ``firstKeptId`` on the parent's raw path — the append-at-the-tip shape
           (``session_store.append_compaction`` / ``append_elide``). The kept region
           is the suffix of the parent's path from the boundary onward.
        2. ``firstKeptId`` is a DESCENDANT of the anchor — the re-parented shape
           (``SessionManager.apply_compaction``, and the frozen System-A oracle the
           fold-parity tests pin). The forward scan in :meth:`_active_path_entries`
           never finds the boundary among the anchor's ancestors, so the anchor keeps
           nothing from before itself; the count is therefore the whole folded parent
           context, and it is a real count, not a guess. Rendering this as the
           warning in case 3 would cry corruption over τ's own normal output.
        3. Anything else — no ``firstKeptId``, or one naming an entry that is neither
           an ancestor nor a descendant. The fold keeps nothing before the anchor
           *and* the resume point is unreachable, so the row says so instead of
           reporting a count next to a meaningless id. That is the only way a browser
           row can warn about a log written by something that skipped the boundary
           check.

        A root-level anchor (``parentId is None``) folds nothing, and is counted as
        such rather than passed to :meth:`context_entries`, whose ``leaf=None`` means
        "use the cursor" — a different question with a plausible-looking wrong answer.
        """
        kind = str(entry.get("type", ""))
        verb = _SPLICE_VERBS[kind]  # KeyError == a caller that is not an anchor
        span = self._splice_span_phrase(entry, kind, verb)
        summary = str(entry.get("summary", "")).strip()
        return f"{span} — {summary}" if summary else span

    def _splice_span_phrase(self, entry: dict[str, Any], kind: str, verb: str) -> str:
        """``"folds 3 entries, resumes at e05"`` — the structural half of the row.

        See :meth:`_splice_anchor_preview` for the three shapes and why they differ.
        """
        boundary_value = entry.get("firstKeptId")
        boundary = str(boundary_value) if boundary_value is not None else None
        parent_value = entry.get("parentId")
        parent = str(parent_value) if parent_value is not None else None

        path_ids = [e["id"] for e in self._walk(parent)] if parent is not None else []
        if boundary is not None and boundary in path_ids:
            kept = set(path_ids[path_ids.index(boundary) :])
        elif boundary is not None and self._boundary_trails_anchor(entry, boundary):
            kept = set()  # shape 2: the kept region hangs BELOW the anchor
        else:
            return f"{kind} → {boundary}: resume point is not on this path ({verb} everything)"

        hidden = (
            [e for e in self.context_entries(parent) if e["id"] not in kept]
            if parent is not None
            else []
        )
        noun = "entry" if len(hidden) == 1 else "entries"
        return f"{verb} {len(hidden)} {noun}, resumes at {boundary}"

    def _boundary_trails_anchor(self, entry: dict[str, Any], boundary: str) -> bool:
        """True when ``firstKeptId`` names a DESCENDANT of this anchor (shape 2).

        ``SessionManager.apply_compaction`` re-parents ``first_kept`` onto the
        compaction, so the kept region trails the anchor instead of preceding it.
        The anchor is then on the boundary's own ancestor chain, which is what this
        checks — an unknown boundary, or one on a sibling branch, is not.
        """
        if boundary not in self._by_id:
            return False
        anchor_id = entry.get("id")
        return any(e["id"] == anchor_id for e in self._walk(boundary))

    def _agent_spec_preview(self, entry: dict[str, Any]) -> str:
        """Row text for an ``agent_spec`` node: WHICH agent starts speaking here.

        W2 (NODE-ADDRESSABLE-AGENTS.md) writes this node for exactly one reason —
        *"Turns 1-5 from a read-only reviewer and 6-10 from a full-tool builder are
        indistinguishable … the loss you feel the first time you debug why the agent
        did not run the tests"* — and until now the browser rendered it as
        ``customEntry: agent_spec``, which is the same loss with a label on it. So
        this says what the frame IS (the first spec on a path) or what CHANGED (every
        later one), because a swap is what a reader is actually scanning for: the node
        is written at construction and at each :meth:`AgentSession.set_model`, so a
        second one on the same path is by definition a boundary between two agents.

        The delta is computed against the nearest ``agent_spec`` among this node's
        ANCESTORS (:meth:`_previous_agent_spec`), not the previous one in load order.
        Ancestry is the only relation that means "the agent that was speaking
        immediately before this one" — a sibling branch's spec never governed these
        turns (I1: a leaf's context is its ancestor chain, nothing else).

        Extensions are reported as a count rather than by name: the record stores
        absolute file paths, which would dominate a one-line row, and the count still
        flags "this span ran with extensions the previous one did not". The API key is
        not in the record at all and must never be (see ``_record_agent_spec``).

        **This is rendering, not reading back.** Decision 3 is that ``agent_spec`` is
        a RECORD, never a contract: nothing may reconstruct a session from it. A
        preview string for a human is the one use the work item explicitly asks for;
        it must stay the only one.
        """
        data = entry.get("data")
        if not isinstance(data, dict):
            # A hand-written or future log. Same policy as _splice_span_phrase's
            # unreachable boundary: report the shape honestly rather than guess.
            return "agent_spec: no frame recorded"

        model = _spec_model_id(data)
        tools = _spec_tools(data)
        previous = self._previous_agent_spec(entry)
        if previous is None:
            return f"agent_spec: {model} · {_tools_phrase(tools)}"

        prev_raw = previous.get("data")
        prev = prev_raw if isinstance(prev_raw, dict) else {}
        changes: list[str] = []

        prev_model = _spec_model_id(prev)
        if prev_model != model:
            changes.append(f"model {prev_model} → {model}")

        prev_tools = _spec_tools(prev)
        added = [t for t in tools if t not in prev_tools]
        removed = [t for t in prev_tools if t not in tools]
        if added or removed:
            marks = [f"+{t}" for t in added] + [f"-{t}" for t in removed]
            changes.append("tools " + " ".join(marks))

        if prev.get("system_prompt_digest") != data.get("system_prompt_digest"):
            changes.append("new system prompt")

        prev_exts = prev.get("extensions")
        exts = data.get("extensions")
        n_prev = len(prev_exts) if isinstance(prev_exts, list) else 0
        n_now = len(exts) if isinstance(exts, list) else 0
        if n_prev != n_now:
            changes.append(f"extensions {n_prev} → {n_now}")

        if prev.get("cwd") != data.get("cwd"):
            changes.append(f"cwd {prev.get('cwd')} → {data.get('cwd')}")

        if not changes:
            # Two records, same frame. Saying "unchanged" is the informative answer —
            # it tells a reader who is hunting a swap that this node is not the one.
            return f"agent_spec: {model} · {_tools_phrase(tools)} (unchanged)"
        return "agent_spec: " + "; ".join(changes)

    def _previous_agent_spec(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """The nearest ``agent_spec`` among ``entry``'s ancestors, or ``None``.

        ``_walk`` returns root→leaf, so the reversed scan finds the CLOSEST ancestor
        first — the spec that governed the turns immediately preceding this node.
        """
        parent = entry.get("parentId")
        if parent is None:
            return None
        for candidate in reversed(self._walk(str(parent))):
            if (
                candidate.get("type") == "customEntry"
                and candidate.get("customType") == "agent_spec"
            ):
                return candidate
        return None

    def subtree_text(self, from_id: str) -> str:
        """Concatenated text of the whole SUBTREE at ``from_id`` (BFS, every descendant).

        Verbatim port of ``_extract_branch_messages`` (``session_manager.py:627-702``)
        with ``parent_id`` → ``parentId`` and a ``branch_summary`` case added
        alongside ``compaction`` (§2.4). Feeds the "summarize branch" prompt.

        **The bound is structural: descendants of the node the caller named** — nothing
        else. It reaches down, never sideways: a sibling subtree, a concurrent branch
        rooted elsewhere, and the primary line above ``from_id`` are all outside it,
        because none of them is reachable by following ``parentId`` edges downward from
        ``from_id``.

        This is deliberately NOT the lane filter it replaces (docs/LANE-REMOVAL.md §6.2).
        That filter asked *who wrote this entry* and refused to descend from a primary
        entry into a sub-agent branch hanging under it; this asks *what did the caller
        name*, and a sub-agent's subtree under ``from_id`` IS part of what happened
        there, so it is summarized with it. The difference is visible exactly when the
        two disagree — and when they do, write provenance is the wrong answer: an
        extension that deliberately summarizes a region containing a sub-agent's work
        has said which region it means, while the old rule silently returned a different
        one. A caller that wants only the sub-agent's own work names the branch root; a
        caller that wants only the primary line asks for ``context_for``, not this.
        """
        if not self._entries:
            return ""

        branch_messages: list[str] = []
        queue: list[str] = [from_id]
        visited: set[str] = set()

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            entry = self._by_id.get(current_id)
            if entry is None:
                continue

            kind = entry.get("type")
            if kind in ("message", "customMessage"):
                message = entry.get("message", {})
                role = message.get("role", "unknown")
                content = message.get("content", [])
                if isinstance(content, str):
                    branch_messages.append(f"[{role}]: {content}")
                else:
                    branch_messages.append(f"[{role}]: {_message_text(message)}")
            elif kind == "toolResult":
                tool_name = entry.get("tool_name", "unknown")
                content = entry.get("content", [])
                content_str = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
                branch_messages.append(f"[toolResult: {tool_name}] {content_str}")
            elif kind in _SUMMARY_KINDS:
                summary = entry.get("summary", "")
                branch_messages.append(f"[{kind}]: {summary}")

            for child_id in self._children.get(current_id, []):
                queue.append(child_id)

        return "\n".join(branch_messages)
