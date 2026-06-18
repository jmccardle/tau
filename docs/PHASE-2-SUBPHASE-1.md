# Phase 2 Subphase 1 — Agent Loop

> **Topic**: Implement the core agent loop that drives conversations, executes tools, and emits events.

## Scope

This is the most complex subphase. It implements `tau_agent_core.agent_loop.AgentLoop` — the direct port of pi's `agent-loop.js` logic. It:
1. Takes messages + context
2. Calls the LLM via τ-ai
3. Parses assistant response for text and tool calls
4. Executes tool calls (sequential or parallel)
5. Feeds results back to the LLM
6. Repeats until no more tool calls or termination

## Reference

- `SUBPHASE-0.0.md` lines 200-220: AgentEvent contract
- `docs/tau-agent-core.md` lines 50-200: agent loop design
- `docs/tau-agent-core.md` lines 200-300: tool execution
- pi's `agent-loop.js` (reference implementation)
- pi's `agent-session.js` lines 1-100: tool call preparation

## Implementation Outline

### `tau_agent_core/agent_loop.py`

```python
class AgentLoop:
    def __init__(self, config: AgentLoopConfig, emit: Callable[[AgentEvent], Awaitable[None]]):
        self.config = config
        self.emit = emit
        self._turn_index = 0

    async def run(
        self,
        prompts: list[UserMessage],
        context: list[Message],
    ) -> list[Message]:
        """Run the full agent loop for one or more prompts."""
        ...

    async def run_continue(
        self,
        context: list[Message],
    ) -> list[Message]:
        """Run another agent turn without adding new messages."""
        ...

    async def _stream_response(self, context: list[Message]) -> AssistantMessage:
        """Stream assistant response from LLM."""
        # 1. Convert context to LLM format
        # 2. Resolve API key
        # 3. Call stream_simple()
        # 4. Process events -> emit AgentEvents
        # 5. Return final AssistantMessage
        ...

    async def _execute_tool_calls(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls (sequential or parallel)."""
        ...

    async def _execute_sequential(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls one at a time."""
        ...

    async def _execute_parallel(
        self,
        assistant: AssistantMessage,
        tool_calls: list[ToolCall],
    ) -> ToolBatchResult:
        """Execute tool calls concurrently."""
        ...

    async def _prepare_tool_call(
        self, tool_call: ToolCall, tool: AgentTool,
    ) -> PreparedToolCall | BlockedCall | ErrorCall:
        """Prepare a tool call: validate args, run before hooks."""
        ...

    async def _execute_tool(self, call: PreparedToolCall) -> FinalizedToolCall:
        """Execute a single tool with error handling."""
        ...

    async def _apply_after_hooks(self, result: FinalizedToolCall) -> FinalizedToolCall:
        """Apply after-tool-call hooks (extensions)."""
        ...

    def _to_llm_tool(self, tool: AgentTool) -> dict:
        """Convert AgentTool to LLM tool format."""
        ...
```

### Key Behaviors

1. **Tool call preparation**: validate args against JSON schema, run `before_tool_call` hooks
2. **Sequential execution**: run each tool one at a time, stop if any returns `terminate: true`
3. **Parallel execution**: run all tools concurrently with `asyncio.gather()`, stop if any returns `terminate: true`
4. **Early termination**: if a tool result has `terminate: true`, don't make another LLM call
5. **Error handling**: catch exceptions during tool execution, emit as error results
6. **Abort signal**: check `config.abort_signal` during long-running operations

### Steering Messages and Follow-ups

The agent loop checks for steering/follow-up messages:
- After each turn, check if new steering messages are pending
- After the loop ends, check for follow-up messages
- Steering messages are delivered while streaming (as assistant messages)
- Follow-up messages are delivered after the loop finishes

### Tool Call Format in Context

When feeding tool results back to the LLM:
- Each tool result is a `ToolResultMessage` with role "tool"
- `tool_call_id` links back to the original tool call
- `tool_name` is included for clarity
- `content` is the tool's output (list of ContentBlocks)
- `is_error` indicates if the tool failed

## Done Criteria

- `AgentLoop.run()` correctly handles:
  - Pure text response (no tool calls)
  - Single tool call (text → tool call → result → text)
  - Multiple tool calls in one response (sequential mode)
  - Multiple tool calls in one response (parallel mode)
  - Multiple turns (LLM calls itself until done)
  - Early termination (one tool returns `terminate: true`)
  - Tool execution errors
  - Abort during tool execution
  - Abort during LLM streaming
- `AgentLoop.run_continue()` works (runs another turn with existing context)
- All events are emitted at the correct points (see AgentEvent contract)
- Token usage is tracked and emitted
- Tool arguments are validated before execution

### Event Sequence for Text-Only Response

```
agent_start
message_start (user prompt)
message_end (user prompt)
message_start (assistant partial)
  → multiple message_update events
message_end (assistant final)
turn_start (0)
turn_end (0, message=assistant, tool_results=[])
agent_end (messages=[...])
```

### Event Sequence for Tool Call Response

```
agent_start
message_start (user prompt)
message_end (user prompt)
message_start (assistant partial)
message_end (assistant final)
turn_start (0)
tool_execution_start (call_id, tool_name, args)
tool_execution_update (call_id, partial_result)  # optional
tool_execution_end (call_id, result, is_error)
message_start (tool result)
message_end (tool result)
turn_end (0, message=assistant, tool_results=[tool_result_msg])
agent_end (messages=[...])
```

## Testing Strategy

### Test 1: Text-only response

```python
async def test_text_only():
    events = []
    loop = AgentLoop(config=AgentLoopConfig(...), emit=lambda e: events.append(e))
    messages = await loop.run(
        prompts=[UserMessage(content=[TextContent(text="hi")])],
        context=[],
    )
    assert len(messages) == 2  # prompt + response
    assert events[-1].type == "agent_end"
    # Verify event sequence
    types = [e.type for e in events]
    assert types == ["agent_start", "message_start", "message_end",
                     "message_update", "message_end",
                     "turn_start", "turn_end", "agent_end"]
```

### Test 2: Single tool call (sequential)

```python
async def test_single_tool_call_sequential():
    events = []
    loop = AgentLoop(config=AgentLoopConfig(
        model=..., system_prompt="test",
        tools=[bash_tool],
        tool_execution_mode="sequential",
    ), emit=lambda e: events.append(e))

    messages = await loop.run(
        prompts=[UserMessage(content=[TextContent(text="run ls")])],
        context=[],
    )

    # Verify tool was called
    tool_starts = [e for e in events if e.type == "tool_execution_start"]
    tool_ends = [e for e in events if e.type == "tool_execution_end"]
    assert len(tool_starts) == 1
    assert len(tool_ends) == 1
    assert tool_ends[0].tool_name == "bash"
```

### Test 3: Multiple tool calls (parallel)

```python
async def test_parallel_tool_calls():
    events = []
    loop = AgentLoop(config=AgentLoopConfig(
        model=..., system_prompt="test",
        tools=[bash_tool, read_tool],
        tool_execution_mode="parallel",
    ), emit=lambda e: events.append(e))

    messages = await loop.run(
        prompts=[UserMessage(content=[TextContent(text="list and read")])],
        context=[],
    )

    tool_starts = [e for e in events if e.type == "tool_execution_start"]
    assert len(tool_starts) == 2  # both tools called
    # In parallel mode, both start before either ends
```

### Test 4: Early termination

```python
async def test_early_termination():
    # Tool that returns terminate=True
    events = []
    loop = AgentLoop(config=AgentLoopConfig(
        model=..., system_prompt="test",
        tools=[terminate_tool],
    ), emit=lambda e: events.append(e))

    messages = await loop.run(
        prompts=[UserMessage(content=[TextContent(text="terminate")])],
        context=[],
    )

    # Only 1 LLM call (second call should be skipped)
    assistant_messages = [e for e in events if hasattr(e, 'message') and e.message and e.message.role == "assistant"]
    assert len(assistant_messages) <= 2  # prompt + 1 response
```

### Test 5: Tool error handling

```python
async def test_tool_error():
    events = []
    loop = AgentLoop(config=AgentLoopConfig(
        model=..., system_prompt="test",
        tools=[failing_tool],
    ), emit=lambda e: events.append(e))

    messages = await loop.run(
        prompts=[UserMessage(content=[TextContent(text="fail")])],
        context=[],
    )

    error_events = [e for e in events if e.type == "tool_execution_end" and e.is_error]
    assert len(error_events) == 1
```

### Test 6: Abort during tool execution

```python
async def test_abort_during_tool():
    signal = AbortSignal()
    events = []
    loop = AgentLoop(config=AgentLoopConfig(
        model=..., system_prompt="test",
        tools=[slow_tool],
        abort_signal=signal,
    ), emit=lambda e: events.append(e))

    # Start the loop in a task
    task = asyncio.create_task(loop.run(
        prompts=[UserMessage(content=[TextContent(text="slow")])],
        context=[],
    ))

    await asyncio.sleep(0.1)
    signal.abort()
    await asyncio.sleep(0.1)

    # Loop should have stopped
    assert not task.done()  # or task.cancel() if we want to force it
```

## Success Signal

The agent loop works end-to-end for all 6 response types (text-only, single tool, multiple sequential, multiple parallel, early termination, error). Events are emitted in the correct sequence. An agent can drive the loop purely through events — no direct tool execution knowledge needed.
