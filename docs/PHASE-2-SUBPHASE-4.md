# Phase 2 Subphase 4 — Agent Session and SDK Entry Point

> **Topic**: Implement `AgentSession` (the public API) and `create_agent_session()` (the SDK entry point).

## Scope

This subphase ties together Phase 2.1 (agent loop), Phase 2.2 (session manager), and Phase 3 (event bus, extensions) into the `AgentSession` class that τ-coding-agent uses. It also provides the `create_agent_session()` SDK factory.

**Note**: Phase 3's event bus and extension system are prerequisites for this subphase. The `AgentSession` class wires everything together.

## Reference

- `SUBPHASE-0.0.md` lines 260-340: AgentSession interface contract
- `docs/tau-agent-core.md` lines 450-550: agent session design
- `docs/tau-coding-agent.md` lines 160-220: how TUI consumes AgentSession
- `MONOREPO-STRUCTURE.md` lines 40-45: AgentSession location

## Implementation Outline

### `tau_agent_core/agent_session.py`

```python
class AgentSession:
    """High-level session API. Combines agent loop, session manager, and events.

    This is the primary entry point for both SDK and TUI usage.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        model: Model,
        system_prompt: str = "",
        tools: list[AgentTool] | None = None,
        extensions: list[Callable] | None = None,
    ):
        self._session_manager = session_manager
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._events = EventBus()  # from Phase 3
        self._extensions = extensions or []
        self._is_streaming = False
        self._abort_signal = AbortSignal()

        # Register extensions
        for ext in extensions:
            ext(self._make_extension_api())

    # Properties
    @property
    def messages(self) -> list[Message]:
        """Current conversation messages (active path)."""
        return self._session_manager.get_active_messages()

    @property
    def state(self) -> "SessionState":
        """Read-only access to session state."""
        return SessionState(self)

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    # Public API
    def subscribe(self, handler: Callable[[AgentEvent], Any]) -> Callable[[], None]:
        """Subscribe to agent events. Returns unsubscribe function."""
        return self._events.on("all", handler)

    async def prompt(self, text: str, images: list[dict] | None = None) -> list[Message]:
        """Send a prompt and run the agent loop."""
        # 1. Create UserMessage
        # 2. Append to session
        # 3. Run agent loop
        # 4. Stream results back
        ...

    async def continue_conversation(self) -> list[Message]:
        """Run another turn without new messages."""
        ...

    async def compact(self, custom_instructions: str | None = None):
        """Trigger manual compaction."""
        ...

    def abort(self) -> None:
        """Abort the current agent turn."""
        self._abort_signal.abort()

    def _make_extension_api(self) -> ExtensionAPI:
        """Create an ExtensionAPI bound to this session."""
        ...
```

### `tau_agent_core/sdk.py`

```python
def create_agent_session(
    model: str | Model = "gpt-4o",
    provider: str = "openai",
    base_url: str | None = None,
    api_key: str | None = None,
    tools: list[str] | None = None,
    session_manager: SessionManager | None = None,
    extensions: list[Callable] | None = None,
    system_prompt: str | None = None,
    thinking_level: str = "off",
    cwd: str | None = None,
    settings: dict | None = None,
) -> AgentSession:
    """Create an AgentSession with all defaults.

    This is the main SDK entry point. It handles:
    - Model resolution (string → Model object)
    - Tool discovery (string names → AgentTool objects)
    - Extension loading (from ~/.tau/extensions/ and ./.tau/extensions/)
    - System prompt building (from AGENTS.md, .tau/SYSTEM.md)
    - Settings loading (from ~/.tau/settings.json)
    """
    # 1. Resolve model
    if isinstance(model, str):
        model = resolve_model(model, provider=provider, base_url=base_url)

    # 2. Discover and create tools
    tool_objs = _resolve_tools(tools)

    # 3. Load extensions
    ext_factories = _load_extensions(extensions)

    # 4. Build system prompt
    sys_prompt = system_prompt or _build_system_prompt(cwd, tool_objs)

    # 5. Create session manager
    if session_manager is None:
        session_manager = SessionManager(cwd=cwd)

    # 6. Create and return AgentSession
    return AgentSession(
        session_manager=session_manager,
        model=model,
        system_prompt=sys_prompt,
        tools=tool_objs,
        extensions=ext_factories,
    )
```

### Key Behaviors

1. **Message flow**: `prompt()` creates a `UserMessage`, appends it to the session, then runs `AgentLoop.run()`. The loop's events are forwarded to all subscribers.
2. **Event forwarding**: `AgentSession.subscribe()` adds a handler to the EventBus. The agent loop emits `AgentEvent`s to the bus, which dispatches to all handlers.
3. **Abort**: `abort()` calls `AbortSignal.abort()`. The agent loop checks the signal during tool execution and LLM streaming.
4. **State**: `state` property wraps `SessionState` to provide read-only access.
5. **Extension binding**: Each extension receives an `ExtensionAPI` instance bound to this session. Extensions can subscribe to events, register tools, and persist state.

## Done Criteria

- `AgentSession` can be instantiated with all parameters
- `AgentSession.messages` returns the current active path messages
- `AgentSession.subscribe()` adds a handler and returns an unsubscribe function
- `AgentSession.unsubscribe()` removes the handler
- `AgentSession.prompt()` sends a prompt, runs the agent loop, returns messages
- `AgentSession.continue_conversation()` runs another turn
- `AgentSession.compact()` triggers manual compaction
- `AgentSession.abort()` aborts the current turn
- `AgentSession.is_streaming` is True during `prompt()` and False otherwise
- `create_agent_session()` creates a fully configured session
- `create_agent_session()` resolves model strings to Model objects
- `create_agent_session()` discovers and loads extensions from default paths
- `create_agent_session()` builds system prompts from context files

## Testing Strategy

### Test 1: AgentSession creation

```python
async def test_create_agent_session():
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        tools=["read", "bash"],
    )
    assert session._model.id == "gpt-4o"
    assert len(session._tools) == 2
    assert session._is_streaming is False
```

### Test 2: Subscribe and unsubscribe

```python
async def test_subscribe_unsubscribe():
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    handler = Mock()
    unsub = session.subscribe(handler)
    assert len(session._events._listeners) > 0

    unsub()
    assert handler.called  # handler still exists
    # Next emit should not call handler
```

### Test 3: Prompt runs agent loop

```python
async def test_prompt_runs_loop(mock_openai):
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    session.subscribe(lambda e: None)  # no-op handler

    messages = await session.prompt("hello")
    assert len(messages) > 0
    assert session.is_streaming is False
    # Check session was updated
    msgs = session.messages
    assert any(m.role == "user" for m in msgs)
    assert any(m.role == "assistant" for m in msgs)
```

### Test 4: Abort during prompt

```python
async def test_abort_during_prompt(mock_openai):
    session = create_agent_session(
        model="gpt-40",
        session_manager=SessionManager.in_memory(),
    )

    async def abort_later():
        await asyncio.sleep(0.05)
        session.abort()

    asyncio.create_task(abort_later())

    # Should return (possibly partial) messages
    messages = await session.prompt("long response")
    assert session.is_streaming is False
```

### Test 5: Continue conversation

```python
async def test_continue_conversation(mock_openai):
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )

    # First prompt
    await session.prompt("hello")
    msg_count_1 = len(session.messages)

    # Continue
    messages = await session.continue_conversation()
    msg_count_2 = len(session.messages)
    assert msg_count_2 > msg_count_1
```

### Test 6: create_agent_session with model string

```python
async def test_model_resolution():
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    assert session._model.id == "gpt-4o"
    assert session._model.provider == "openai"
```

### Test 7: Extensions are loaded and receive API

```python
async def test_extensions_loaded():
    ext_called = []
    def my_ext(api):
        ext_called.append(api)

    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        extensions=[my_ext],
    )
    assert len(ext_called) == 1
    assert isinstance(ext_called[0], ExtensionAPI)
```

### Test 8: In-memory session is isolated

```python
async def test_in_memory_isolation():
    session1 = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    session2 = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )

    await session1.prompt("hello from session 1")
    assert len(session1.messages) > 0
    assert len(session2.messages) == 0  # isolated
```

## Success Signal

All 8 test categories pass. `AgentSession` is the complete public API — it wraps the agent loop, session manager, event bus, and extensions. `create_agent_session()` is the single entry point for SDK usage. An agent using `AgentSession` never needs to import anything else from τ-agent-core.
