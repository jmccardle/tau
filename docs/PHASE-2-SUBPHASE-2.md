# Phase 2 Subphase 2 — Session Manager

> **Topic**: Implement JSONL session persistence with tree structure, entry types, and session operations.

## Scope

This subphase implements `tau_agent_core.session_manager.SessionManager` — the file-level session management. It handles:
1. Creating and loading session files (JSONL format)
2. Appending entries to the active path
3. Building the active message path (respects tree structure)
4. Listing sessions (current directory and all)
5. Forking and cloning sessions
6. In-memory mode for testing

## Reference

- `SUBPHASE-0.0.md` lines 220-260: session entry JSON schema
- `docs/tau-agent-core.md` lines 350-450: session types
- `docs/IMPLEMENTATION-PLAN.md` lines 100-180: session manager spec
- pi's `session-manager.js` (reference — ~400 lines)
- pi's `agent-session.js` lines 100-200: tree traversal

## Implementation Outline

### `tau_agent_core/session_manager.py`

```python
class SessionManager:
    def __init__(self, cwd: str | None = None, sessions_dir: str | None = None):
        self.cwd = cwd or os.getcwd()
        self._sessions_dir = sessions_dir or os.path.join(self.cwd, ".tau", "sessions")
        self._active_session_path: str | None = None
        self._active_entry_id: str | None = None  # for tree navigation
        self._memory_entries: list[dict] = []  # for in-memory mode
        self._memory_active_path: list[str] = []

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> "SessionManager":
        """Create an in-memory session manager (no file persistence)."""
        mgr = cls(cwd)
        mgr._memory_store = []
        return mgr

    def new_session(self, model_id: str | None = None) -> str:
        """Create a new session file. Returns session path."""
        session_path = os.path.join(
            self._sessions_dir,
            f"{uuid4().hex}.jsonl",
        )
        # Write session entry
        self.append_entry({
            "id": uuid4().hex,
            "type": "session",
            "timestamp": int(time.time() * 1000),
            "parent_id": None,
            "model": model_id,
            "cwd": self.cwd,
        })
        return session_path

    def load(self, session_path: str) -> SessionState:
        """Load a session from a JSONL file."""
        entries = self._read_file(session_path)
        return SessionState(entries=entries, session_path=session_path)

    def save(self, state: SessionState) -> None:
        """Save session state (append new entries)."""
        ...

    def append_entry(self, entry: dict) -> None:
        """Append a JSONL entry to the current session file."""
        if self._memory_store is not None:
            self._memory_store.append(entry)
        else:
            with open(self._active_session_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def get_active_messages(self) -> list[Message]:
        """Get messages for the current active path (tree-aware)."""
        if self._memory_store is not None:
            entries = self._memory_store
        else:
            entries = self._read_file(self._active_session_path)

        # Build active path by following parent_id chain from active_entry_id
        active_path = self._build_active_path(entries)
        # Extract messages from active_path, skipping compaction entries
        messages = []
        for entry in active_path:
            if entry["type"] == "message":
                messages.append(entry["message"])
            elif entry["type"] == "compaction":
                # Include compaction summary as a user message
                messages.append(UserMessage(
                    content=[ThinkingContent(
                        text=f"[[Compaction summary: {entry['summary']}]]"
                    )]
                ))
        return messages

    def list(self) -> list[SessionInfo]:
        """List sessions for the current working directory."""
        ...

    def list_all(self) -> list[SessionInfo]:
        """List all sessions across all directories."""
        ...

    def fork(self, entry_id: str, position: Literal["before", "at"] = "before") -> str:
        """Create a new session from a specific entry."""
        ...

    def clone(self, entry_id: str) -> str:
        """Duplicate the active path at entry_id into a new session."""
        ...

    def navigate(self, entry_id: str) -> SessionState:
        """Navigate to a specific entry in the tree."""
        ...

    def _read_file(self, path: str) -> list[dict]:
        """Read all entries from a JSONL file."""
        entries = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries
```

### SessionState

```python
@dataclass
class SessionState:
    entries: list[dict]  # All entries for this session
    session_path: str
    active_entry_id: str | None  # Current position in tree
    model: str | None
    model_name: str | None
    cwd: str | None
    system_prompt: str | None
    session_name: str | None
```

### Key Behaviors

1. **JSONL format**: Each line is a JSON object (one entry). No arrays.
2. **Tree structure**: Entries have `parent_id`. The "active path" is the chain of entries from root to `active_entry_id`.
3. **Compaction**: A `compaction` entry replaces entries before `first_kept_id`. When building the active path, skip compacted entries and include the compaction summary instead.
4. **Session files**: Named with UUID hex. Stored in `.tau/sessions/` within the project directory.
5. **In-memory mode**: All operations use a list in memory instead of files. Same API surface.

### Fork vs Clone

| Operation | Description | Result |
|-----------|-------------|--------|
| `fork(entry_id, "before")` | Copy entries before entry_id | New session with entries up to entry_id |
| `fork(entry_id, "at")` | Copy entry_id and entries after | New session starting from entry_id |
| `clone(entry_id)` | Copy the active path at entry_id | New session with copy of active path |

## Done Criteria

- `new_session()` creates a valid JSONL file with a session entry
- `load()` reads a JSONL file and returns a `SessionState`
- `append_entry()` appends to the active session file (or in-memory store)
- `get_active_messages()` returns messages for the active path, skipping compacted entries
- `list()` returns sessions for the current directory (grouped by date)
- `list_all()` returns all sessions
- `fork()` creates a new session file with the forked entries
- `clone()` creates a new session file with the cloned path
- `navigate()` updates the active entry ID
- In-memory mode works identically to file mode (same API)
- Session files are valid JSONL (each line is valid JSON)

## Testing Strategy

### Test 1: Session creation and loading

```python
async def test_new_session_and_load(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session(model_id="gpt-4o")

    assert os.path.exists(session_path)
    entries = mgr._read_file(session_path)
    assert len(entries) == 1
    assert entries[0]["type"] == "session"
    assert entries[0]["model"] == "gpt-4o"
```

### Test 2: Append and retrieve messages

```python
async def test_append_and_get_messages(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    mgr.append_entry({
        "id": "m1", "type": "message", "timestamp": 1,
        "message": UserMessage(content=[TextContent(text="hello")]).model_dump(),
    })

    messages = mgr.get_active_messages()
    assert len(messages) == 1
    assert messages[0].content[0].text == "hello"
```

### Test 3: Tree structure — navigate

```python
async def test_tree_navigation(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    # Add entries with parent_id chain
    mgr.append_entry({"id": "m1", "type": "message", "timestamp": 1, "parent_id": None,
                      "message": {"content": [{"text": "hello"}], "role": "user"}})
    mgr.append_entry({"id": "m2", "type": "message", "timestamp": 2, "parent_id": "m1",
                      "message": {"content": [{"text": "hi"}], "role": "assistant"}})

    # Navigate to m1 (go back in tree)
    mgr.navigate("m1")
    messages = mgr.get_active_messages()
    assert len(messages) == 1
```

### Test 4: Fork

```python
async def test_fork(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    mgr.append_entry({"id": "m1", "type": "message", "timestamp": 1, "message": {...}})
    mgr.append_entry({"id": "m2", "type": "message", "timestamp": 2, "message": {...}})

    forked = mgr.fork("m1", "at")
    forked_entries = mgr._read_file(forked)
    # Forked session should have m1 and m2
    assert any(e["id"] == "m1" for e in forked_entries)
    assert any(e["id"] == "m2" for e in forked_entries)
```

### Test 5: Compaction entry handling

```python
async def test_compaction_in_active_path(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    mgr.append_entry({"id": "old1", "type": "message", "timestamp": 1, "message": {...}})
    mgr.append_entry({"id": "comp1", "type": "compaction", "timestamp": 2,
                      "first_kept_id": "new1",
                      "summary": "Previous conversation was about X",
                      "tokens_saved": 500})
    mgr.append_entry({"id": "new1", "type": "message", "timestamp": 3, "message": {...}})

    messages = mgr.get_active_messages()
    # Should NOT include old1, should include compaction summary
    assert not any(m.content[0].text == "old" for m in messages)
    assert any("Compaction summary" in m.content[0].text for m in messages)
```

### Test 6: In-memory mode

```python
async def test_in_memory_mode():
    mgr = SessionManager.in_memory()
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    mgr.append_entry({"id": "m1", "type": "message", "timestamp": 1,
                      "message": {"content": [{"text": "hello"}], "role": "user"}})

    messages = mgr.get_active_messages()
    assert len(messages) == 1
    assert messages[0].content[0].text == "hello"
```

### Test 7: Session listing

```python
async def test_session_listing(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    mgr.new_session()
    mgr.new_session()

    sessions = mgr.list()
    assert len(sessions) == 2
    for s in sessions:
        assert s.session_path is not None
        assert s.model is not None
```

## Success Signal

All 7 test categories pass. Session files are valid JSONL. Tree navigation works correctly. Compaction entries are properly handled in the active path. In-memory mode is indistinguishable from file mode. The session manager is the single source of truth for session persistence.
