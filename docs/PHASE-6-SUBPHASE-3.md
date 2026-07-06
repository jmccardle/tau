# Phase 6 Subphase 3 — Polish and Integration Tests

> **Topic**: Error handling, performance tuning, end-to-end integration tests, and documentation.

## Scope

This final subphase ensures the system works end-to-end. It implements:
1. Error handling (provider errors, tool errors, extension errors)
2. Performance tuning (30Hz throttle, large file handling)
3. End-to-end integration tests
4. Documentation (README, example extensions, SDK examples)

## Reference

- `docs/IMPLEMENTATION-PLAN.md` lines 500-560: polish and documentation spec
- `docs/PI-TO-TAU-COMPATIBILITY.md` lines 100-160: error handling mapping
- All previous subphase docs

## Implementation Outline

### Error Handling

1. **Provider errors**: When `stream_simple()` returns an `ErrorEvent`, the agent loop converts it to an `AgentEvent` with `is_error=True` and a human-readable message in the chat.

2. **Tool errors**: When a tool's `execute()` raises an exception, the agent loop catches it, wraps it in a `ToolResultMessage` with `is_error=True`, and sends it to the LLM.

3. **Extension errors**: When an extension handler raises an exception, the EventBus logs it and continues to the next handler. The agent loop is not affected.

4. **Network errors**: Handled by the OpenAI SDK. Retries are automatic. If all retries fail, an `ErrorEvent` is emitted.

### Performance Tuning

1. **30Hz throttle**: The chat display accumulates text deltas and updates the display at most 30 times per second. This prevents UI thrashing.

2. **Streaming message buffer**: Text deltas are accumulated in a buffer before being passed to the display widget. This reduces the number of widget updates.

3. **Large file handling**: Files larger than 1MB are read with offset/limit and displayed with truncation. The LLM gets a truncated view with a notice.

4. **Memory profile**: For long sessions (>1000 messages), the active path is lazily loaded from the JSONL file rather than kept entirely in memory.

### Integration Test Suite

```python
# tests/integration/test_full_flow.py

async def test_e2e_text_only(mock_openai):
    """User sends a text prompt, gets a text response."""
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    events = []
    session.subscribe(lambda e: events.append(e))
    messages = await session.prompt("Say hello")
    assert len(events) > 0
    assert events[-1].type == "agent_end"
    assert any(m.role == "assistant" for m in messages)

async def test_e2e_single_tool(mock_openai):
    """User sends a prompt, LLM calls a tool, gets a result."""
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        tools=["read"],
    )
    events = []
    session.subscribe(lambda e: events.append(e))

    # Write a file first (manual for test setup)
    await session.prompt("Write a file called test.txt with content 'hello world'")

    # Now read it
    messages = await session.prompt("Read test.txt")

    tool_events = [e for e in events if e.type == "tool_execution_end"]
    assert any(e.tool_name == "read" for e in tool_events)

async def test_e2e_multi_turn(mock_openai):
    """User sends a prompt, LLM calls tools, calls itself, finishes."""
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        tools=["write", "read"],
    )
    events = []
    session.subscribe(lambda e: events.append(e))

    messages = await session.prompt("Write a file and then read it back")

    assistant_msgs = [m for m in messages if hasattr(m, 'role') and m.role == "assistant"]
    assert len(assistant_msgs) >= 2  # at least 2 turns

async def test_e2e_with_extension(mock_openai):
    """User sends a prompt, extension intercepts tool call."""
    intercept_count = [0]

    def my_ext(api):
        api.on("tool_call", lambda e: intercept_count.append(1))

    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        tools=["bash"],
        extensions=[my_ext],
    )
    messages = await session.prompt("run echo hello")
    assert intercept_count[0] == 1  # extension intercepted

async def test_e2e_abort(mock_openai):
    """User aborts during streaming."""
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )

    async def abort_later():
        await asyncio.sleep(0.1)
        session.abort()

    asyncio.create_task(abort_later())
    messages = await session.prompt("write a very long essay")
    assert session.is_streaming is False

async def test_e2e_session_persistence(mock_openai):
    """Messages persist across session reload."""
    mgr = SessionManager.in_memory()
    session = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )

    await session.prompt("hello")
    stored_entries = len(mgr._memory_store)

    # Reload session
    session2 = create_agent_session(
        model="gpt-4o",
        session_manager=mgr,
    )
    assert len(session2.messages) > 0
```

### Documentation

1. **README.md** for each package (tau-ai, tau-agent-core, tau-coding-agent)
2. **Example extensions** (5-10):
   - Permission gate (blocks destructive commands)
   - Git checkpoint (auto-commits after each turn)
   - Dynamic env tool (reads environment variables)
   - Session logger (logs all events to file)
   - Custom tool (e.g., "greet" — simple tool)
3. **SDK usage examples**:
   - Create a session and send a prompt
   - Subscribe to events
   - Use in-memory mode for testing
   - Custom system prompt
4. **RPC protocol documentation**:
   - Request/response format
   - Available methods
   - Example client code
5. **Migration guide** from parley:
   - What changes
   - What stays the same
   - How to migrate existing code

## Done Criteria

- Provider errors are handled gracefully (user sees error message)
- Tool errors are wrapped and sent to the LLM
- Extension errors don't crash the agent loop
- Network errors are retried by the SDK
- 30Hz throttle prevents UI thrashing with >1000 tokens/second
- Large files (>1MB) are truncated with a notice
- All 6 integration tests pass
- README.md exists for each package
- 5 example extensions are documented
- SDK usage examples are provided
- RPC protocol documentation is complete
- Migration guide from parley is written

## Testing Strategy

### Integration Tests (all 6 must pass)

1. **Text-only flow**: `session.prompt("Say hello")` → text response
2. **Single tool flow**: `session.prompt("Write and read a file")` → tool call → result
3. **Multi-turn flow**: `session.prompt("Write and read")` → 2+ LLM calls
4. **Extension flow**: Extension intercepts tool call, count incremented
5. **Abort flow**: `session.abort()` → streaming stops
6. **Persistence flow**: Session reload → messages preserved

### Performance Test

```python
async def test_throttle_performance():
    """Verify 30Hz throttle with 1000 text deltas."""
    display = ChatDisplay()
    for i in range(1000):
        display.update_streaming_message(
            AgentEvent(type="message_update", message=AssistantMessage(content=[TextContent(text="x")]))
        )
    # Should have at most ~30 updates (30Hz * 1 second)
    assert display.update_count < 50  # generous margin
```

### Error Handling Tests

```python
async def test_provider_error_handling(mock_openai):
    """Provider error → error event → error message in chat."""
    mock_openai.api_key_auth = Mock(side_effect=Exception("Invalid key"))
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
    )
    events = []
    session.subscribe(lambda e: events.append(e))
    messages = await session.prompt("hello")
    assert any(e.type == "error" for e in events)

async def test_tool_error_handling(mock_openai):
    """Tool error → error result → sent to LLM."""
    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        tools=["failing_tool"],
    )
    messages = await session.prompt("use failing_tool")
    tool_results = [m for m in messages if hasattr(m, 'role') and m.role == "tool"]
    assert any(m.is_error for m in tool_results)

async def test_extension_error_handling(mock_openai):
    """Extension error → logged → agent continues."""
    def bad_ext(api):
        api.on("agent_start", lambda e: 1/0)

    session = create_agent_session(
        model="gpt-4o",
        session_manager=SessionManager.in_memory(),
        extensions=[bad_ext],
    )
    messages = await session.prompt("hello")
    # Agent should still produce messages despite extension error
    assert len(messages) > 0
```

## Success Signal

All integration tests pass. Error handling works correctly. Performance meets the 30Hz throttle target. Documentation is complete. An agent can read the README and write a working τ extension. The RPC protocol is documented with example client code. The migration guide from parley is accurate.


## Evaluator Feedback

- **2026-06-18 05:03** Evaluator: Phase 6 Subphase 3 is NOT 100% complete. The code implementation and tests are excellent (149 tests all passing across integration, error handling, and performance categories). However, 4 of 12 Done Criteria items related to documentation are missing:

1. **README.md for each package** — `tau-ai/README.md`, `tau-agent-core/README.md`, `tau-coding-agent/README.md` all do not exist.

2. **5 example extensions documented** — No `examples/` directory, no example extension files (e.g., permission gate, git checkpoint, dynamic env tool, session logger, custom tool).

3. **SDK usage examples provided** — No examples directory with SDK usage examples (create session, subscribe to events, in-memory mode, custom system prompt).

4. **RPC protocol documentation complete** — `rpc.py` code exists but no `.md` documentation covering request/response format, available methods, and example client code.

5. **Migration guide from parley written** — `docs/PI-TO-TAU-COMPATIBILITY.md` is a compatibility mapping document, not a migration guide. No guide covering 'what changes', 'what stays the same', and 'how to migrate existing code' exists.

To complete this subphase: create the 3 package-level READMEs, create an examples directory with 5 example extensions and 4+ SDK usage examples, create RPC protocol documentation, and write a migration guide from parley.