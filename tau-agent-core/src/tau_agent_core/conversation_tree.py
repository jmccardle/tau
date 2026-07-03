"""τ-agent-core conversation tree: the pure, I/O-free session-tree algebra.

``ConversationTree`` is a side-effect-free function of ``(entries, cursor)`` over
the raw ``session_store.Session.entries()`` dicts (camelCase ``parentId`` /
``firstKeptId`` / ``fromId``). It owns the *interpretive fold* — the leaf→root
``parentId`` walk plus the read-time compaction / ``branch_summary`` splice — that
turns the persisted branching tree into the flat message list the agent loop
consumes, without touching the filesystem, ``Session``, or ``asyncio``.

Provenance (ported verbatim, only the field names reconciled camelCase and the
splice generalised over both summary kinds):

- ``context_for`` / ``_active_path_entries`` ← pi ``buildSessionContext``
  (``session-manager.ts:325-423``) — the leaf→root ``parentId`` walk, the "anchor on
  the LAST compaction/branch_summary in the path" rule (``:591-600``), and the
  splice that emits the summary node, the kept entries before it from the boundary
  (``firstKeptId`` / ``fromId``), then everything after (``:400-423``). This reads a
  summary appended at the tip (append-only compaction, step 1c) as well as one whose
  kept region trails it; the entry→message conversion mirrors
  ``SessionManager.get_active_messages`` (``:191-221``).
- ``tree`` ← pi ``getTree(): SessionTreeNode[]`` (``session-manager.ts:1191``):
  parent/child nodes, children sorted by timestamp, ``is_leaf`` == the cursor.
- ``subtree_text`` ← ``SessionManager._extract_branch_messages``
  (``session_manager.py:627-702``).
- ``navigate`` ← pi ``branch(id)`` (``session-manager.ts:1241``) — cursor move only.

Reference: SESSION-TREE-IMPLEMENTATION.md §2.1, §2.5, §2.7 (step 1a);
EXTENSIONS-ORCHESTRATION-PLAN.md §4 (tree-as-truth).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The two "summary anchor" kinds. §2.4 unifies them: both replace a subpath with
# a single summary node at read time, differing only in ``type`` and in the field
# naming their linear-order boundary (``firstKeptId`` vs ``fromId``).
_SUMMARY_KINDS = ("compaction", "branch_summary")


def _boundary_id(entry: dict[str, Any]) -> str | None:
    """The first-kept boundary id of a summary anchor (``firstKeptId`` for a
    compaction, ``fromId`` for a branch_summary)."""
    if entry.get("type") == "compaction":
        value = entry.get("firstKeptId")
    else:
        value = entry.get("fromId")
    return str(value) if value is not None else None


def _summary_message(summary: str) -> dict[str, Any]:
    """Render a summary anchor as the loop-consumable user message. Mirrors
    ``SessionManager.get_active_messages`` (``session_manager.py:208-220``) so the
    fold parity test holds; branch_summary uses the same shape (§2.4)."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"[[Compaction summary: {summary}]]"}],
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
            elif kind in _SUMMARY_KINDS:
                messages.append(_summary_message(str(entry.get("summary", ""))))
        return messages

    def _active_path_entries(self, leaf_id: str | None) -> list[dict[str, Any]]:
        """Faithful side-effect-free port of pi ``buildSessionContext``
        (``session-manager.ts:325-423``) over camelCase entries: leaf→root walk,
        then the summary-anchor splice."""
        entries = self._entries
        if not entries:
            return []

        # Walk backwards from the leaf (falling back to the root entry when the
        # cursor is unset), then reverse to root→leaf order (``:568-582``).
        path = self._walk(leaf_id or entries[0]["id"])

        # Anchor on the LAST (most recent) summary entry in the path. With
        # iterative compaction each new summary supersedes the earlier ones, so
        # anchoring on the last drops the stale summaries and their kept regions
        # (``:591-600``). Identical to "first from root" with a single summary.
        anchor_idx: int | None = None
        for idx, entry in enumerate(path):
            if entry.get("type") in _SUMMARY_KINDS:
                anchor_idx = idx

        if anchor_idx is None:
            return path

        anchor = path[anchor_idx]
        boundary = _boundary_id(anchor)

        # pi ``buildSessionContext`` (``:400-423``): emit the summary node, then the
        # kept entries BEFORE the anchor starting at the boundary (``firstKeptId`` /
        # ``fromId``), then every entry AFTER the anchor. Correct whether the summary
        # was appended at the tip (append-only compaction: the boundary is an
        # ancestor, so the kept region precedes the anchor) or its kept region
        # trails it — the shape the frozen System-A oracle produced.
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
        if entry.get("type") != "message":
            return None
        role = entry.get("message", {}).get("role")
        return str(role) if role is not None else None

    def _preview_of(self, entry: dict[str, Any]) -> str:
        kind = entry.get("type")
        if kind == "message":
            text = _message_text(entry.get("message", {}))
        elif kind in _SUMMARY_KINDS:
            text = str(entry.get("summary", ""))
        else:
            text = ""
        stripped = text.strip()
        return stripped.split("\n", 1)[0] if stripped else ""

    def subtree_text(self, from_id: str) -> str:
        """Concatenated text of every descendant of ``from_id`` (BFS).

        Verbatim port of ``_extract_branch_messages`` (``session_manager.py:627-702``)
        with ``parent_id`` → ``parentId`` and a ``branch_summary`` case added
        alongside ``compaction`` (§2.4). Feeds the "summarize branch" prompt.
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
            if kind == "message":
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
