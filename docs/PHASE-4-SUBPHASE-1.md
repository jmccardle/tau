# Phase 4 Subphase 1 — TUI App Shell

> **Topic**: Fork Parley's app.py, replace backends with τ-agent-core, and set up the basic layout.

## Scope

This subphase creates the **app shell** of τ-coding-agent. It is a Textual app that:
1. Forks parley.py (keep the 30Hz throttle, incremental mounting, catppuccin theme)
2. Replaces the backend system with τ-agent-core's `AgentSession`
3. Provides the basic layout: sidebar + chat + input
4. Handles CLI arguments

No agent-specific widgets yet — just the shell.

## Reference

- `SUBPHASE-4-SUBPHASE-0.md`: TUI contracts
- `docs/tau-coding-agent.md` lines 1-60: what Parley gives us
- `docs/tau-coding-agent.md` lines 60-100: what changes from Parley
- parley.py (source to fork from)
- parley.tcss (theme to keep)
- `docs/textual-headless-testing.md`: headless Textual testing patterns and fixtures

## Implementation Outline

### `tau_coding_agent/app.py`

```python
from textual.app import App
from textual.widgets import Header, Footer, RichLog, TextArea
from textual.containers import Container, Vertical

from tau_agent_core import AgentSession

class ChatDisplay(Container):
    """Container for chat messages. 30Hz throttle on updates."""
    pass

class InputBar(TextArea):
    """User input area. Handles Enter, Ctrl+Enter, @, !."""
    pass

class ParleyApp(App):
    """Main τ coding agent app (fork of Parley)."""

    CSS_PATH = "themes/catppuccin.tcss"

    def __init__(
        self,
        session: AgentSession | None = None,
        print_mode: bool = False,
    ):
        super().__init__()
        self._session = session
        self._print_mode = print_mode
        self._is_streaming = False
        self._throttle_timer = None

    def on_mount(self):
        """Set up the layout and subscribe to agent events."""
        self._setup_layout()
        self._subscribe_to_events()

    def _setup_layout(self):
        """Create the basic layout: header + sidebar + chat + input + footer."""
        ...

    def _subscribe_to_events(self):
        """Subscribe to agent session events."""
        if self._session:
            self._session.subscribe(self._handle_event)

    def _handle_event(self, event):
        """Dispatch agent events to widgets."""
        if event.type == "message_update":
            self._update_streaming_message(event)
        elif event.type == "agent_end":
            self._on_agent_end(event)

    def _update_streaming_message(self, event):
        """Update the streaming message with 30Hz throttle."""
        # Same 30Hz throttle as parley.py
        if self._throttle_timer:
            self._throttle_timer.stop()
        self.call_later(self._do_update_streaming_message)
        self._throttle_timer = self.set_timer(1/30, lambda: None)

    def _do_update_streaming_message(self):
        self.query_one(ChatDisplay).update_streaming_message(...)

    def _on_agent_end(self, event):
        self._is_streaming = False
        self._re_enable_input()

    def _on_input_submitted(self, text: str):
        """Handle user input submission."""
        if self._print_mode:
            self._handle_print_mode(text)
        else:
            self._handle_interactive(text)

    async def _handle_interactive(self, text: str):
        """Send text to agent session for processing."""
        self._is_streaming = True
        self._re_disable_input()
        try:
            messages = await self._session.prompt(text)
        finally:
            self._re_enable_input()

    def _handle_print_mode(self, text: str):
        """In print mode: stream response and exit."""
        ...

    def _re_disable_input(self):
        self.query_one(InputBar).disabled = True

    def _re_enable_input(self):
        self.query_one(InputBar).disabled = False
```

### Key Parley Features to Preserve

1. **30Hz throttle**: `self._throttle_timer` accumulates streaming deltas and updates the display at most 30 times per second. This prevents UI thrashing during high-speed LLM streaming.

2. **Incremental mounting**: Messages are appended one at a time rather than rendered all at once. The `ChatDisplay` container uses `mount()` for each new message.

3. **Catppuccin-mocha theme**: Copy `parley.tcss` to `tau_coding_agent/themes/catppuccin.tcss`. This is the default theme.

4. **Config system**: Load settings from `.tau/settings.json` in the project directory, then from `~/.tau/settings.json`.

5. **Command palette**: Textual's built-in `CommandPalette` with τ-specific commands.

### CLI Entry Point

```python
# tau_coding_agent/cli.py
import typer
from tau_coding_agent.app import ParleyApp

app = typer.Typer()

@app.command()
def main(
    prompt: list[str] | None = None,
    model: str | None = None,
    thinking: str = "off",
    tools: str | None = None,
    extension: list[str] | None = None,
    no_extensions: bool = False,
    continue_: bool = typer.Option(False, "--continue", "-c"),
    print_mode: bool = typer.Option(False, "--print", "-p"),
):
    # Build AgentSession from CLI args
    # Launch ParleyApp
    session = build_session(...)
    ParleyApp(session=session, print_mode=print_mode).run()

if __name__ == "__main__":
    app()
```

## Done Criteria

- `ParleyApp` is importable and runnable
- The layout includes: header, sidebar, chat area, input bar, footer
- `AgentSession` is created from CLI args and passed to `ParleyApp`
- Agent events are subscribed to and dispatched
- Streaming text is throttled to 30Hz
- Input is disabled during streaming and re-enabled after
- Print mode streams response and exits
- Catppuccin theme is applied
- Config is loaded from `.tau/settings.json` and `~/.tau/settings.json`

## Testing Strategy

### Test 1: App import and instantiation

```python
async def test_app_import():
    app = ParleyApp(print_mode=False)
    assert app._is_streaming is False
```

### Test 2: Layout elements present

```python
async def test_layout_elements(app):
    await app._setup_layout()
    assert app.query_one(Header)
    assert app.query_one(ChatDisplay)
    assert app.query_one(InputBar)
    assert app.query_one(Footer)
```

### Test 3: Event subscription

```python
async def test_event_subscription(app):
    received = []
    handler = lambda e: received.append(e)
    app._session.subscribe(handler)
    await app._session._events.emit(AgentEvent(type="agent_start"))
    assert len(received) == 1
```

### Test 4: 30Hz throttle

```python
async def test_throttle():
    updates = []
    app = ParleyApp()
    for i in range(100):
        await app._update_streaming_message(
            AgentEvent(type="message_update", message=...)
        )
    # Only ~3 updates per second worth of calls should result in actual updates
    # (verify that _do_update_streaming_message is called at most 30 times per second)
```

### Test 5: Input disable/enable

```python
async def test_input_disable_enable(app):
    app._re_disable_input()
    assert app.query_one(InputBar).disabled is True
    app._re_enable_input()
    assert app.query_one(InputBar).disabled is False
```

### Test 6: Agent end handler

```python
async def test_agent_end_handler(app):
    app._is_streaming = True
    await app._on_agent_end(AgentEvent(type="agent_end"))
    assert app._is_streaming is False
```

## Success Signal

All 6 test categories pass. The app is a runnable Textual app with the correct layout, event subscription, and 30Hz throttling. It can be launched with `tau` and will show the basic UI. No agent-specific rendering yet — just the shell.
