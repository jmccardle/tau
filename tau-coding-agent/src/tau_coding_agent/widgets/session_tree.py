"""Session Tree Widget — tree-based session navigation.

Implements SessionTreeWidget (a Tree-based widget) for displaying sessions
with branches, fork, clone, and compaction display.

Reference: PHASE-4-SUBPHASE-3.md — Session Tree and Input Bar
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import tau_agent_core


# ---------------------------------------------------------------------------
# SessionInfo — dataclass for session tree nodes
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """Dataclass for a session's metadata as displayed in the tree.

    Attributes:
        session_path: Path to the JSONL session file
        cwd: Working directory for the session
        model: Model identifier used for this session
        model_name: Human-readable model name
        timestamp: Creation timestamp (ms since epoch)
        entries: Raw session entries (for tree building)
    """

    session_path: str = ""
    cwd: str = ""
    model: str | None = None
    model_name: str | None = None
    timestamp: int | None = None
    entries: list[dict] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> SessionInfo:
        """Create SessionInfo from a dict (session entry).

        Args:
            d: Dict with session metadata keys.

        Returns:
            SessionInfo instance.
        """
        return cls(
            session_path=d.get("session_path", ""),
            cwd=d.get("cwd", ""),
            model=d.get("model"),
            model_name=d.get("model_name"),
            timestamp=d.get("timestamp"),
        )

    @classmethod
    def from_session_manager_info(
        cls, sm_info: tau_agent_core.SessionInfo
    ) -> SessionInfo:
        """Convert a SessionManager SessionInfo to our display SessionInfo."""
        return cls(
            session_path=sm_info.session_path,
            cwd=sm_info.cwd or "",
            model=sm_info.model,
            model_name=sm_info.model_name,
            timestamp=sm_info.created_at,
        )


# ---------------------------------------------------------------------------
# SessionTreeWidget — tree-based session navigation
# ---------------------------------------------------------------------------


class SessionTreeWidget:
    """Tree-based session navigation widget.

    Displays sessions as a tree with session names, message previews,
    and compaction summaries.

    Attributes:
        _session_manager: SessionManager instance
        _on_select: Callback when a session/entry is selected
        _roots: Dict mapping session_path -> root node label
        _tree: Internal tree data structure
    """

    def __init__(
        self,
        session_manager: tau_agent_core.SessionManager,
        on_select: Callable[[SessionInfo | tau_agent_core.SessionInfo], None],
    ) -> None:
        self._session_manager = session_manager
        self._on_select = on_select
        self._roots: dict[str, str] = {}  # session_path -> root label
        self._children: dict[str, list[dict]] = {}  # parent_id -> children
        self._selected: SessionInfo | tau_agent_core.SessionInfo | None = None
        self._refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Refresh the tree — rebuild from session manager."""
        self._roots.clear()
        self._children.clear()
        self._refresh()

    def get_selected(self) -> SessionInfo | tau_agent_core.SessionInfo | None:
        """Return the currently selected item."""
        return self._selected

    @property
    def root_children(self) -> list[str]:
        """Return root node labels (for testing)."""
        return list(self._roots.keys())

    @property
    def root_labels(self) -> list[str]:
        """Return root labels as display strings."""
        return list(self._roots.values())

    def get_children(self, parent_key: str) -> list[dict]:
        """Return children of a root node."""
        return self._children.get(parent_key, [])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Load sessions and build tree from scratch."""
        self._roots.clear()
        self._children.clear()
        sessions = self._session_manager.list()
        for session in sessions:
            root_key = session.session_path
            label = f"📁 {Path(session.cwd or session.session_path).name}"
            self._roots[root_key] = label
            entries = self._get_entries_for_session(session)
            self._children[root_key] = self._load_entries(session, entries)

    def _get_entries_for_session(
        self, session_info: tau_agent_core.SessionInfo
    ) -> list[dict]:
        """Get all entries for a session (from file or memory)."""
        # If the SessionManager provided entries (in-memory mode), use them
        if session_info.entries is not None:
            return session_info.entries

        # Otherwise, read from the file
        try:
            return self._session_manager._read_file(session_info.session_path)
        except (OSError, ValueError):
            return []

    def _load_entries(
        self, session_info: tau_agent_core.SessionInfo, entries: list[dict]
    ) -> list[dict]:
        """Load session entries into tree node format.

        Only skips the first entry if it's the root session entry.
        Subsequent session-type entries (e.g., from fork/clone with session_name)
        are displayed as child nodes.
        """
        nodes = []
        for i, entry in enumerate(entries):
            # Skip only the first entry if it's the root session entry
            if i == 0 and entry.get("type") == "session":
                continue
            node = self._make_entry_node(entry)
            nodes.append(node)
        return nodes

    def _make_entry_node(self, entry: dict) -> dict:
        """Create a tree node dict from a session entry."""
        entry_type = entry.get("type", "unknown")

        if entry_type == "message":
            msg = entry.get("message", {})
            role = msg.get("role", "unknown")
            if role == "user":
                # First user message preview
                content = "".join(
                    c.get("text", "")[:50]
                    for c in msg.get("content", [])
                    if c.get("type") == "text"
                )
                label = f"📝 {content}"
            elif role == "assistant":
                label = "💬 assistant"
            else:
                label = f"💬 {role}"
            return {
                "type": "message",
                "label": label,
                "entry": entry,
            }
        elif entry_type == "session":
            name = entry.get("session_name") or "unnamed"
            return {
                "type": "session",
                "label": f"🗂 {name}",
                "entry": entry,
            }
        elif entry_type == "compaction":
            tokens_saved = entry.get("tokens_saved", 0)
            return {
                "type": "compaction",
                "label": f"📦 Compaction ({tokens_saved} tokens saved)",
                "entry": entry,
            }
        else:
            return {
                "type": entry_type,
                "label": f"❓ {entry_type}",
                "entry": entry,
            }

    def on_select(self, item_key: str) -> None:
        """Handle selection of a tree item.

        Args:
            item_key: The key of the selected item (session_path for roots)
        """
        self._selected = None
        if item_key in self._roots:
            # Session selected — create SessionInfo
            sessions = self._session_manager.list()
            for si in sessions:
                if si.session_path == item_key:
                    self._selected = SessionInfo(
                        session_path=si.session_path,
                        cwd=si.cwd or "",
                        model=si.model,
                        model_name=si.model_name,
                        timestamp=si.created_at,
                    )
                    break
        if self._selected is not None:
            self._on_select(self._selected)

    def on_tree_selected(self, node_data: dict) -> None:
        """Handle tree selection (called by Textual widget).

        Args:
            node_data: dict with 'type' and optionally 'data' key
        """
        node_type = node_data.get("type", "")
        if node_type == "session":
            session_info = node_data.get("data")
            if session_info:
                if isinstance(session_info, SessionInfo):
                    self._selected = session_info
                elif hasattr(session_info, "session_path"):
                    self._selected = SessionInfo.from_session_manager_info(
                        session_info
                    )
                self._on_select(self._selected)
        else:
            # Message, compaction, or other — pass the entry
            entry = node_data.get("entry")
            if entry:
                self._selected = entry
                self._on_select(self._selected)
