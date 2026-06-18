"""τ-agent-core session manager: JSONL session persistence with tree structure.

Implements `SessionManager` — the file-level session management. It handles:
1. Creating and loading session files (JSONL format)
2. Appending entries to the active path
3. Building the active message path (respects tree structure)
4. Listing sessions (current directory and all)
5. Forking and cloning sessions
6. In-memory mode for testing

Reference: PHASE-2-SUBPHASE-2.md, SUBPHASE-0.0.md "6. Session Entry JSON Schema"
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class SessionState:
    """State of a loaded session (return type of load()).

    Attributes:
        entries: All entries for this session (loaded from file or memory)
        session_path: Path to the JSONL session file
        active_entry_id: Current position in the tree (None = root)
        model: Model identifier used for this session
        model_name: Human-readable model name
        cwd: Working directory when session was created
        system_prompt: System prompt used
        session_name: Human-readable session name
    """

    entries: list[dict] = field(default_factory=list)
    session_path: str = ""
    active_entry_id: str | None = None
    model: str | None = None
    model_name: str | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    session_name: str | None = None


@dataclass
class SessionInfo:
    """Metadata about a session, for listing and display.

    Attributes:
        session_path: Path to the JSONL session file
        session_name: Human-readable session name
        cwd: Working directory for this session
        model: Model identifier used for this session
        model_name: Human-readable model name
        created_at: Creation timestamp (ms since epoch)
        message_count: Total number of message entries
        status: Session status string
        entries: Entries for this session (in-memory mode only)
    """

    session_path: str
    session_name: str | None = None
    cwd: str | None = None
    model: str | None = None
    model_name: str | None = None
    created_at: int = 0
    message_count: int = 0
    status: str = "idle"
    entries: list[dict] | None = None


class SessionManager:
    """File-level session management.

    Handles JSONL session persistence with tree structure,
    entry types, and session operations.
    """

    def __init__(
        self,
        cwd: str | None = None,
        sessions_dir: str | None = None,
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self._sessions_dir = sessions_dir or os.path.join(self.cwd, ".tau", "sessions")
        self._active_session_path: str | None = None
        self._active_entry_id: str | None = None
        # In-memory store (set by in_memory())
        self._memory_store: list[dict] | None = None
        self._memory_active_path: list[str] = []
        # Track in-memory session paths so list() can find them
        self._memory_session_paths: list[str] = []

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> SessionManager:
        """Create an in-memory session manager (no file persistence)."""
        mgr = cls(cwd)
        mgr._memory_store = []
        return mgr

    def new_session(self, model_id: str | None = None) -> str:
        """Create a new session file. Returns session path.

        Creates a JSONL file with a single session entry as the root.
        """
        if not os.path.exists(self._sessions_dir):
            os.makedirs(self._sessions_dir, exist_ok=True)

        session_path = os.path.join(
            self._sessions_dir,
            f"{uuid.uuid4().hex}.jsonl",
        )

        entry = {
            "id": uuid.uuid4().hex,
            "type": "session",
            "timestamp": int(time.time() * 1000),
            "parent_id": None,
            "model": model_id,
            "cwd": self.cwd,
        }

        # Set active session path BEFORE appending
        self._active_session_path = session_path
        self._active_entry_id = entry["id"]
        # Track in-memory session path
        if self._memory_store is not None:
            self._memory_session_paths.append(session_path)
        self.append_entry(entry)
        return session_path

    def load(self, session_path: str) -> SessionState:
        """Load a session from a JSONL file.

        Reads all entries from the file and returns a SessionState.
        """
        entries = self._read_file(session_path)
        state = SessionState(
            entries=entries,
            session_path=session_path,
        )

        # Extract metadata from the session entry
        if entries:
            first = entries[0]
            if first.get("type") == "session":
                state.model = first.get("model")
                state.model_name = first.get("model_name")
                state.cwd = first.get("cwd")
                state.system_prompt = first.get("system_prompt")
                state.session_name = first.get("session_name")
                state.active_entry_id = first["id"]
                self._active_session_path = session_path
                self._active_entry_id = first["id"]

        return state

    def save(self, state: SessionState) -> None:
        """Save session state — append new entries to the session file."""
        if state.session_path:
            with open(state.session_path, "a") as f:
                for entry in state.entries:
                    f.write(json.dumps(entry) + "\n")

    def append_entry(self, entry: dict) -> str:
        """Append a JSONL entry to the current session file.

        Returns the entry's id.
        """
        entry_id = entry.get("id", uuid.uuid4().hex)
        entry["id"] = entry_id
        if "timestamp" not in entry:
            entry["timestamp"] = int(time.time() * 1000)
        if "parent_id" not in entry:
            entry["parent_id"] = self._active_entry_id

        if self._memory_store is not None:
            self._memory_store.append(entry)
        elif self._active_session_path:
            with open(self._active_session_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

        self._active_entry_id = entry_id
        return entry_id

    def get_active_messages(self) -> list[dict]:
        """Get messages for the current active path (tree-aware).

        Returns messages by following the parent_id chain from the
        current active_entry_id back to root, skipping compacted entries
        and including compaction summaries.
        """
        entries = self._get_entries()
        if not entries:
            return []

        active_path = self._build_active_path(entries)
        messages = []
        for entry in active_path:
            if entry.get("type") == "message":
                msg = entry.get("message", {})
                messages.append(msg)
            elif entry.get("type") == "compaction":
                summary = entry.get("summary", "")
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[[Compaction summary: {summary}]]",
                        }
                    ],
                })
        return messages

    def list(self) -> list[SessionInfo]:
        """List sessions for the current working directory.

        Returns sessions sorted by creation timestamp (newest first).
        """
        return self._list_sessions_from_dir(self._sessions_dir)

    def list_all(self) -> list[SessionInfo]:
        """List all sessions across all directories."""
        return self._list_sessions_from_dir(self._sessions_dir)

    def _list_sessions_from_dir(self, sessions_dir: str) -> list[SessionInfo]:
        """List sessions from a specific directory."""
        results: list[SessionInfo] = []

        # If in in-memory mode, return in-memory sessions
        if self._memory_store is not None:
            # Find all ROOT session entries (parent_id is None or missing).
            # These are created by new_session(). Child session entries
            # (from fork/clone) have a parent_id and are NOT new sessions.
            session_ranges: list[tuple[int, int, dict]] = []
            for idx, entry in enumerate(self._memory_store):
                if entry.get("type") == "session" and not entry.get("parent_id"):
                    # Find the next ROOT session entry (exclusive) or end of store
                    next_session_idx = len(self._memory_store)
                    for later in range(idx + 1, len(self._memory_store)):
                        later_entry = self._memory_store[later]
                        if (
                            later_entry.get("type") == "session"
                            and not later_entry.get("parent_id")
                        ):
                            next_session_idx = later
                            break
                    session_ranges.append((idx, next_session_idx, entry))

            # Build SessionInfo for each session
            for sess_start, sess_end, sess_entry in session_ranges:
                # Get the path: use corresponding path from _memory_session_paths
                path_idx = min(sess_start, len(self._memory_session_paths) - 1)
                if path_idx < 0:
                    path_idx = 0
                sess_path = (
                    self._memory_session_paths[path_idx]
                    if self._memory_session_paths
                    else ""
                )

                # Entries for this session (between this session and next)
                session_entries = self._memory_store[sess_start:sess_end]
                message_count = sum(
                    1
                    for e in session_entries
                    if e.get("type") == "message"
                )

                info = SessionInfo(
                    session_path=sess_path,
                    session_name=sess_entry.get("session_name"),
                    cwd=sess_entry.get("cwd"),
                    model=sess_entry.get("model"),
                    model_name=sess_entry.get("model_name"),
                    created_at=sess_entry.get("timestamp", 0),
                    message_count=message_count,
                    entries=session_entries,
                )
                results.append(info)

            # Sort by creation timestamp, newest first
            results.sort(key=lambda s: s.created_at, reverse=True)
            return results

        if not os.path.exists(sessions_dir):
            return []

        for filename in os.listdir(sessions_dir):
            if not filename.endswith(".jsonl"):
                continue
            session_path = os.path.join(sessions_dir, filename)
            info = self._extract_session_info(session_path)
            if info:
                results.append(info)

        # Sort by creation timestamp, newest first
        results.sort(key=lambda s: s.created_at, reverse=True)
        return results

    def fork(
        self,
        entry_id: str,
        position: Literal["before", "at"] = "before",
    ) -> str:
        """Create a new session from a specific entry.

        Args:
            entry_id: The entry to fork from
            position: "before" copies entries before entry_id,
                      "at" copies entry_id and entries after it

        Returns:
            Path to the new session file.
        """
        if not self._active_session_path:
            raise RuntimeError("No active session")

        entries = self._read_file(self._active_session_path)

        if position == "before":
            # Copy entries up to but not including entry_id
            new_entries = []
            for entry in entries:
                if entry["id"] == entry_id:
                    break
                new_entries.append(entry)
        else:
            # Copy entry_id and all entries after it
            new_entries = []
            found = False
            for entry in entries:
                if entry["id"] == entry_id:
                    found = True
                if found:
                    new_entries.append(entry)

        # Create new session file
        new_session_path = os.path.join(
            self._sessions_dir,
            f"{uuid.uuid4().hex}.jsonl",
        )

        # Create session entry as the first entry
        session_entry = {
            "id": uuid.uuid4().hex,
            "type": "session",
            "timestamp": int(time.time() * 1000),
            "parent_id": None,
            "model": None,
            "cwd": self.cwd,
        }

        # Skip session entries in the forked entries
        # (we're creating a new session entry)
        non_session_entries = [
            e for e in new_entries if e.get("type") != "session"
        ]

        # Rewrite all entries with updated parent_id chain
        parent = session_entry["id"]
        updated_entries = []
        for entry in non_session_entries:
            entry["parent_id"] = parent
            updated_entries.append(entry)
            parent = entry["id"]

        with open(new_session_path, "w") as f:
            f.write(json.dumps(session_entry) + "\n")
            for entry in updated_entries:
                f.write(json.dumps(entry) + "\n")

        return new_session_path

    def clone(self, entry_id: str) -> str:
        """Duplicate the active path at entry_id into a new session.

        Returns:
            Path to the new session file.
        """
        if not self._active_session_path:
            raise RuntimeError("No active session")

        entries = self._read_file(self._active_session_path)
        active_path = self._build_active_path(entries)

        # Create new session file
        new_session_path = os.path.join(
            self._sessions_dir,
            f"{uuid.uuid4().hex}.jsonl",
        )

        # Create session entry
        session_entry = {
            "id": uuid.uuid4().hex,
            "type": "session",
            "timestamp": int(time.time() * 1000),
            "parent_id": None,
            "model": None,
            "cwd": self.cwd,
        }

        with open(new_session_path, "w") as f:
            f.write(json.dumps(session_entry) + "\n")
            for entry in active_path:
                if entry.get("type") in ("message", "compaction"):
                    f.write(json.dumps(entry) + "\n")

        return new_session_path

    def navigate(self, entry_id: str | None) -> SessionState:
        """Navigate to a specific entry in the tree.

        Updates the active entry ID and returns the session state
        at that position.
        """
        entries = self._get_entries()

        if entry_id is not None and entries:
            entry_by_id = {e["id"]: e for e in entries}
            if entry_id not in entry_by_id:
                raise KeyError(f"Entry {entry_id} not found")

        self._active_entry_id = entry_id

        # Find the session entry to get metadata
        state = SessionState(
            entries=entries,
            session_path=self._active_session_path or "",
            active_entry_id=entry_id,
        )
        for entry in entries:
            if entry.get("type") == "session":
                state.model = entry.get("model")
                state.model_name = entry.get("model_name")
                state.cwd = entry.get("cwd")
                state.system_prompt = entry.get("system_prompt")
                state.session_name = entry.get("session_name")
                break

        return state

    def _read_file(self, path: str) -> list[dict]:
        """Read all entries from a JSONL file."""
        entries = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def _get_entries(self) -> list[dict]:
        """Get all entries (from memory or file)."""
        if self._memory_store is not None:
            return self._memory_store
        elif self._active_session_path:
            return self._read_file(self._active_session_path)
        return []

    def _build_active_path(self, entries: list[dict]) -> list[dict]:
        """Build the active path by following parent_id chain.

        Starting from active_entry_id, walk backwards through parent_id
        links to reconstruct the path from root to the current entry.
        Then handle compaction: if a compaction entry is in the path,
        entries before it (closer to root) are compacted and replaced
        by the compaction summary.

        Returns entries in root-to-active order.
        """
        if not entries:
            return []

        # Build a lookup by id
        entry_by_id: dict[str, dict] = {}
        for entry in entries:
            entry_by_id[entry["id"]] = entry

        # Build the linear order of entries (from file/memory order)
        linear_order: dict[str, int] = {}
        for idx, entry in enumerate(entries):
            linear_order[entry["id"]] = idx

        # Walk backwards from active_entry_id to root
        path = []
        current_id = self._active_entry_id or (entries[0]["id"] if entries else None)
        visited = set()

        while current_id and current_id not in visited:
            visited.add(current_id)
            entry = entry_by_id.get(current_id)
            if entry is None:
                break
            path.append(entry)
            current_id = entry.get("parent_id")

        # Reverse to get root-to-leaf order
        path.reverse()

        # Now handle compaction:
        # Find any compaction entry in the path.
        # A compaction entry means all entries before it in the path
        # (closer to root) that appear before its first_kept_id
        # in the linear order are compacted.
        # We replace those with the compaction summary.

        # Walk through path and find the first compaction entry from root
        compaction_idx = None
        for idx, entry in enumerate(path):
            if entry.get("type") == "compaction":
                compaction_idx = idx
                break

        if compaction_idx is not None:
            compaction = path[compaction_idx]
            first_kept_id = compaction.get("first_kept_id", "")
            first_kept_order = linear_order.get(first_kept_id, len(entries))

            # Split: entries before compaction in path = compacted
            # entries from compaction onwards = kept
            kept_path = path[compaction_idx:]

            # Filter: entries in kept_path before first_kept_id in linear order
            filtered = []
            for entry in kept_path:
                # Always keep the compaction entry itself (it IS the summary)
                if entry.get("type") == "compaction":
                    filtered.append(entry)
                    continue
                entry_order = linear_order.get(entry["id"], len(entries))
                if entry_order >= first_kept_order:
                    filtered.append(entry)

            return filtered

        # No compaction in path — return full path
        return path

    def _extract_session_info(self, session_path: str) -> SessionInfo | None:
        """Extract session info from a JSONL file."""
        try:
            entries = self._read_file(session_path)
            if not entries:
                return None

            # Find the session entry
            model = None
            model_name = None
            created_at = 0
            message_count = 0
            cwd = None
            session_name = None

            for entry in entries:
                if entry.get("type") == "session":
                    model = entry.get("model")
                    model_name = entry.get("model_name")
                    created_at = entry.get("timestamp", 0)
                    cwd = entry.get("cwd")
                    session_name = entry.get("session_name")
                elif entry.get("type") == "message":
                    message_count += 1

            return SessionInfo(
                session_path=session_path,
                session_name=session_name,
                cwd=cwd,
                model=model,
                model_name=model_name,
                created_at=created_at,
                message_count=message_count,
            )
        except (json.JSONDecodeError, OSError):
            return None
