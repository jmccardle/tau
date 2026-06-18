# Phase 4 Subphase 3 — Session Tree and Input Bar

> **Topic**: Implement the session tree sidebar (replacing Parley's flat list) and the enhanced input bar.

## Scope

This subphase implements the two remaining major UI components:
1. **SessionTreeWidget** — tree-based session navigation with branches, fork, clone, and compaction display
2. **Enhanced InputBar** — TextArea with `@` file references, `!` bash commands, and tab completion

## Reference

- `SUBPHASE-4-SUBPHASE-0.md`: widget data contracts
- `docs/tau-coding-agent.md` lines 160-220: TUI event flow
- `docs/tau-coding-agent.md` lines 220-280: input bar and session tree designs
- `docs/IMPLEMENTATION-PLAN.md` lines 300-360: TUI spec
- `docs/textual-headless-testing.md`: headless Textual testing patterns and fixtures

## Implementation Outline

### SessionTreeWidget

```python
class SessionTreeWidget(Tree):
    """Tree-based session navigation widget."""

    def __init__(self, session_manager: SessionManager, on_select: Callable):
        super().__init__()
        self._session_manager = session_manager
        self._on_select = on_select
        self._refresh()

    def _refresh(self):
        """Load sessions and build tree from scratch."""
        self.clear()
        sessions = self._session_manager.list()
        for session in sessions:
            root = self.add_root(f"📁 {Path(session.cwd).name}", data=session)
            entries = self._load_entries(session)
            for entry in entries:
                self._add_entry_node(root, entry)

    def _load_entries(self, session) -> list[dict]:
        """Load entries from session file."""
        return self._session_manager._read_file(session.session_path)

    def _add_entry_node(self, parent, entry: dict):
        """Add a session entry as a tree node."""
        if entry["type"] == "session":
            label = f"🗂 {entry.get('session_name') or 'unnamed'}"
        elif entry["type"] == "message":
            msg = entry.get("message", {})
            if msg.get("role") == "user":
                # First user message in this session
                content = "".join(
                    c.get("text", "")[:50]
                    for c in msg.get("content", [])
                    if c.get("type") == "text"
                )
                label = f"📝 {content}"
            else:
                label = f"💬 assistant"
        elif entry["type"] == "compaction":
            label = f"📦 Compaction ({entry.get('tokens_saved', 0)} tokens saved)"
        else:
            label = f"❓ {entry['type']}"

        child = self.add_child(parent, label, data=entry)
        # Only add children for session nodes (they have entries)
        if entry["type"] == "session":
            ...

    def on_tree_selected(self, node):
        """Navigate to selected entry."""
        data = node.data
        if isinstance(data, SessionInfo):
            self._on_select(data)
        elif isinstance(data, dict) and data.get("type") == "session":
            self._on_select(SessionInfo.from_dict(data))

    def refresh(self):
        """Refresh the tree (after fork/clone/compaction)."""
        self._refresh()
        self.refresh()
```

### Enhanced InputBar

```python
class InputBar(TextArea):
    """Enhanced input area with @ file refs and !bash commands."""

    def __init__(self, cwd: str):
        super().__init__()
        self._cwd = cwd
        self._history: list[str] = []
        self._history_index = -1

        # Keybindings
        self.bind("enter", "submit", "Submit")
        self.bind("ctrl+enter", "multiline_submit", "Multiline Submit")
        self.bind("up", "history_up", "History Up")
        self.bind("down", "history_down", "History Down")

    def on_key(self, event: Key):
        """Handle special key sequences."""
        if event.key == "tab":
            self._complete_path()
        elif event.key == "backspace" and self.cursor_column == 0:
            # Remove leading !
            text = self.value
            if text.startswith("!"):
                self.value = text[1:]

    def _complete_path(self):
        """Tab completion for file paths."""
        text = self.value
        # Find last @ reference or bare word
        match = re.search(r'(@?)([\w./\\-]*)$', text)
        if match:
            prefix, partial = match.groups()
            cwd = self._cwd
            files = [f.name for f in Path(cwd).rglob("*") if partial in f.name][:10]
            if files:
                completion = files[0]
                new_text = text[:match.start()] + prefix + completion
                self.value = new_text

    def submit(self):
        """Submit the current text."""
        text = self.value.strip()
        if not text:
            return
        if text.startswith("!"):
            self._submit_bash(text[1:])
        else:
            self._emit(InputSubmitted(text))
        self.value = ""
        self._history.append(text)
        self._history_index = -1

    def multiline_submit(self):
        """Submit multiline text (Shift+Enter)."""
        text = self.value.strip()
        if text:
            self._emit(InputSubmitted(text, multiline=True))
            self.value = ""
            self._history.append(text)

    def history_up(self):
        """Navigate up in input history."""
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.value = self._history[-self._history_index - 1]

    def history_down(self):
        """Navigate down in input history."""
        if self._history_index > 0:
            self._history_index -= 1
            self.value = self._history[-self._history_index - 1]
        else:
            self._history_index = -1
            self.value = ""

    def _submit_bash(self, command: str):
        """Execute a bash command and send output to the agent."""
        ...

    def _submit_user_message(self, text: str):
        """Send text as a user message to the agent."""
        self._emit(InputSubmitted(text))

    class InputSubmitted(Event):
        def __init__(self, text: str, multiline: bool = False):
            self.text = text
            self.multiline = multiline
            super().__init__()
```

### Session Info Display

```python
@dataclass
class SessionInfo:
    session_path: str
    cwd: str
    model: str | None
    model_name: str | None
    timestamp: int | None
    entries: list[dict] | None = None

    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            session_path="",  # filled by session_manager
            cwd=d.get("cwd", ""),
            model=d.get("model"),
            model_name=d.get("model_name"),
            timestamp=d.get("timestamp"),
        )
```

## Done Criteria

- `SessionTreeWidget` loads sessions from the session manager and displays them in a tree
- Tree nodes are labeled with session names, message previews, and compaction summaries
- Clicking a session/navigates to that session (via `on_select` callback)
- `SessionTreeWidget.refresh()` rebuilds the tree
- `InputBar` handles Enter (submit), Ctrl+Enter (multiline), Up/Down (history)
- `InputBar` handles `!command` (bash) and `!!command` (silent bash)
- `InputBar` handles `@file` (file reference)
- `InputBar` handles Tab completion for file paths
- `InputBar` has a history buffer with up/down navigation
- `SessionInfo` dataclass has all required fields

## Testing Strategy

### Test 1: SessionTreeWidget loads sessions

```python
async def test_session_tree_loads():
    mgr = SessionManager.in_memory()
    mgr.new_session()
    mgr.new_session()

    tree = SessionTreeWidget(mgr, on_select=lambda s: None)
    assert len(tree.root.children) == 2  # 2 sessions
```

### Test 2: Tree node labeling

```python
async def test_node_labeling():
    mgr = SessionManager.in_memory()
    session_path = mgr.new_session()
    mgr._active_session_path = session_path
    mgr.append_entry({
        "id": "m1", "type": "message", "timestamp": 1,
        "message": {"role": "user", "content": [{"type": "text", "text": "hello world"}]}
    })

    tree = SessionTreeWidget(mgr, on_select=lambda s: None)
    # Should have a node labeled with "hello world"
    nodes = [n.label.plain for n in tree.root.children]
    assert any("hello world" in n for n in nodes)
```

### Test 3: Tree selection

```python
async def test_tree_selection():
    mgr = SessionManager.in_memory()
    selected = []
    tree = SessionTreeWidget(mgr, on_select=lambda s: selected.append(s))

    # Simulate selection
    class FakeNode:
        data = SessionInfo(
            session_path="/tmp/test.jsonl",
            cwd="/tmp", model="gpt-4o",
            model_name="GPT-4o", timestamp=1
        )
    tree.on_tree_selected(FakeNode())
    assert len(selected) == 1
```

### Test 4: InputBar submit

```python
async def test_input_bar_submit():
    events = []
    bar = InputBar(cwd="/tmp")
    bar.value = "hello"
    bar.on_key(Key("enter"))
    # Should emit InputSubmitted event
    events_found = [e for e in events if isinstance(e, InputBar.InputSubmitted)]
    assert len(events_found) == 1
    assert events_found[0].text == "hello"
```

### Test 5: InputBar history

```python
async def test_input_bar_history():
    bar = InputBar(cwd="/tmp")
    bar.value = "cmd1"
    bar.on_key(Key("enter"))
    bar.value = "cmd2"
    bar.on_key(Key("enter"))

    bar.value = ""
    bar.on_key(Key("up"))
    assert bar.value == "cmd2"
    bar.on_key(Key("up"))
    assert bar.value == "cmd1"
```

### Test 6: InputBar bash command

```python
async def test_input_bar_bash():
    events = []
    bar = InputBar(cwd="/tmp")
    bar.value = "!echo hello"
    bar.on_key(Key("enter"))
    # Should emit bash event, not user message
    bash_events = [e for e in events if isinstance(e, InputBar.InputSubmitted) and e.text.startswith("!")]
    assert len(bash_events) == 1
    assert bash_events[0].text == "!echo hello"
```

### Test 7: Tab completion

```python
async def test_tab_completion(tmp_path):
    (tmp_path / "myfile.txt").touch()
    (tmp_path / "other.py").touch()

    bar = InputBar(cwd=str(tmp_path))
    bar.value = "my"
    # Simulate tab
    bar.on_key(Key("tab"))
    # Should complete to "myfile.txt"
    assert "myfile" in bar.value
```

### Test 8: Session refresh

```python
async def test_session_refresh():
    mgr = SessionManager.in_memory()
    mgr.new_session()
    tree = SessionTreeWidget(mgr, on_select=lambda s: None)
    assert len(tree.root.children) == 1

    mgr.new_session()
    tree.refresh()
    assert len(tree.root.children) == 2
```

## Success Signal

All 8 test categories pass. The session tree correctly displays sessions with labels. Clicking navigates. The input bar handles submit, history, bash commands, and tab completion. The session tree refreshes correctly when new sessions are created.
