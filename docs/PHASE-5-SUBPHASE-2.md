# Phase 5 Subphase 2 — Session Operations and Settings

> **Topic**: Implement fork, clone, navigate, and settings management.

## Scope

This subphase implements the session manipulation operations and the settings system. These are used by both the agent loop (Phase 2) and the TUI (Phase 4).

## Reference

- `SUBPHASE-5-SUBPHASE-0.md`: settings and result types
- `docs/tau-agent-core.md` lines 350-450: session operations
- `docs/IMPLEMENTATION-PLAN.md` lines 420-460: session operations spec

## Implementation Outline

### Session Operations (in SessionManager)

```python
def fork(self, entry_id: str, position: Literal["before", "at"] = "before") -> str:
    """Create a new session from a specific entry.

    Args:
        entry_id: The entry to fork from.
        position: "before" — copy entries before entry_id
                  "at" — copy entry_id and entries after

    Returns:
        Path to the new session file.
    """
    entries = self._read_file(self._active_session_path)
    target_entries = self._entries_up_to(entries, entry_id, position)

    # Create new session file
    new_path = os.path.join(self._sessions_dir, f"{uuid4().hex}.jsonl")
    with open(new_path, "w") as f:
        for entry in target_entries:
            f.write(json.dumps(entry) + "\n")
    return new_path

def clone(self, entry_id: str) -> str:
    """Duplicate the active path at entry_id into a new session.

    Returns:
        Path to the new session file.
    """
    entries = self._read_file(self._active_session_path)
    # Copy from root to entry_id (the active path)
    target_entries = [e for e in entries if self._is_on_active_path(e, entries, entry_id)]

    new_path = os.path.join(self._sessions_dir, f"{uuid4().hex}.jsonl")
    with open(new_path, "w") as f:
        for entry in target_entries:
            f.write(json.dumps(entry) + "\n")
    return new_path

def navigate(self, entry_id: str) -> SessionState:
    """Navigate to a specific entry in the tree."""
    entries = self._read_file(self._active_session_path)
    # Find entry and update active_entry_id
    for entry in entries:
        if entry["id"] == entry_id:
            self._active_entry_id = entry_id
            return SessionState(entries=entries, session_path=self._active_session_path, ...)
    raise ValueError(f"Entry {entry_id} not found")
```

### Settings

```python
@dataclass
class Settings:
    default_model: str = "gpt-4o"
    thinking_level: str = "off"
    compaction_enabled: bool = True
    context_margin: int = 2000
    extension_dirs: list[str] = field(default_factory=lambda: [
        str(Path.home() / ".tau" / "extensions"),
    ])
    api_keys: dict[str, str] = field(default_factory=dict)
    custom_system_prompt: str | None = None
    tool_execution_mode: str = "parallel"
    max_retries: int = 3
    temperature: float = 0.7

    @classmethod
    def load(cls, cwd: str | None = None) -> "Settings":
        """Load settings from ~/.tau/settings.json and project-local override."""
        settings = cls()

        # Load global settings
        global_path = Path.home() / ".tau" / "settings.json"
        if global_path.exists():
            settings = settings._merge(global_path)

        # Load project-local settings
        if cwd:
            local_path = Path(cwd) / ".tau" / "settings.json"
            if local_path.exists():
                settings = settings._merge(local_path)

        return settings

    def _merge(self, path: Path) -> "Settings":
        """Merge settings from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        # ... merge data into settings
        return self
```

### Branch Summarization

```python
async def summarize_branch(
    session: AgentSession,
    branch_entry: dict,
    model: Model,
    system_prompt: str,
) -> str:
    """Summarize an abandoned branch of the session tree.

    Used when navigating back to a previous entry — the branch
    from that entry to the current tip is summarized.
    """
    branch_messages = _extract_branch_messages(branch_entry)
    prompt = f"""Summarize this conversation branch:
{branch_messages}

Provide a concise summary that captures the essential context."""

    # Call LLM and return summary
    ...
```

## Done Criteria

- `fork()` creates a new session file with entries up to (but not including) the specified entry
- `fork(entry_id, "at")` includes the specified entry
- `clone()` creates a new session with entries on the active path up to the specified entry
- `navigate()` updates the active entry ID and returns the session state
- `Settings.load()` loads from both global and project-local files
- Project-local settings override global settings
- `summarize_branch()` extracts messages from a branch and generates a summary

## Testing Strategy

### Test 1: Fork at entry

```python
async def test_fork_at_entry(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    for i in range(5):
        mgr.append_entry({
            "id": f"e{i}", "type": "message", "timestamp": i,
            "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]}
        })

    forked = mgr.fork("e2", "at")
    forked_entries = mgr._read_file(forked)
    forked_ids = [e["id"] for e in forked_entries if e["type"] == "message"]
    assert forked_ids == ["e2", "e3", "e4"]
```

### Test 2: Fork before entry

```python
async def test_fork_before_entry(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    for i in range(5):
        mgr.append_entry({
            "id": f"e{i}", "type": "message", "timestamp": i,
            "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]}
        })

    forked = mgr.fork("e2", "before")
    forked_entries = mgr._read_file(forked)
    forked_ids = [e["id"] for e in forked_entries if e["type"] == "message"]
    assert forked_ids == ["e0", "e1"]  # not including e2
```

### Test 3: Clone

```python
async def test_clone(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    for i in range(3):
        mgr.append_entry({
            "id": f"e{i}", "type": "message", "timestamp": i,
            "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]}
        })

    cloned = mgr.clone("e1")
    cloned_entries = mgr._read_file(cloned)
    cloned_ids = [e["id"] for e in cloned_entries if e["type"] == "message"]
    assert cloned_ids == ["e0", "e1"]
```

### Test 4: Navigate

```python
async def test_navigate(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path))
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    for i in range(5):
        mgr.append_entry({
            "id": f"e{i}", "type": "message", "timestamp": i,
            "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]}
        })

    state = mgr.navigate("e2")
    assert mgr._active_entry_id == "e2"
    messages = mgr.get_active_messages()
    assert len(messages) == 3  # e0, e1, e2
```

### Test 5: Settings loading

```python
async def test_settings_loading(tmp_path, monkeypatch):
    global_dir = tmp_path / "home" / ".tau"
    global_dir.mkdir(parents=True)
    (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5-turbo"}')

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    settings = Settings.load()
    assert settings.default_model == "gpt-3.5-turbo"
```

### Test 6: Project-local overrides global

```python
async def test_settings_override(tmp_path, monkeypatch):
    global_dir = tmp_path / "home" / ".tau"
    global_dir.mkdir(parents=True)
    (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5-turbo"}')

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".tau" / "settings.json").write_text('{"default_model": "gpt-4o"}')

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    settings = Settings.load(cwd=str(project_dir))
    assert settings.default_model == "gpt-4o"  # project overrides
```

### Test 7: Fork in-memory mode

```python
async def test_fork_in_memory():
    mgr = SessionManager.in_memory()
    session_path = mgr.new_session()
    mgr._active_session_path = session_path

    for i in range(5):
        mgr.append_entry({"id": f"e{i}", "type": "message", "timestamp": i,
                          "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]}})

    forked = mgr.fork("e2", "at")
    assert forked is not None  # in-memory mode returns a path-like object
```

## Success Signal

All 7 test categories pass. Fork and clone create correct session files. Navigate updates the active path. Settings are loaded from both global and project-local files with correct precedence.
