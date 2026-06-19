# Reference: The Tool-Call Streaming Pipeline

How a single user turn travels from HTTP bytes to a rendered tool call/result, across the three packages. This is the subsystem most worth understanding because it crosses **two event vocabularies** and transforms a "tool call" four times.

## Two event vocabularies

| Vocabulary | Defined in | Producer | Examples |
|---|---|---|---|
| **τ-ai streaming events** | `tau_ai/streaming.py` | the provider (`openai.py`) | `TextDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent` |
| **τ-agent-core AgentEvents** | `tau_agent_core/events.py` | the agent loop / event bus | `agent_start`, `turn_start`, `message_start`, `message_update`, `message_end`, `tool_execution_start`, `tool_execution_end`, `turn_end`, `agent_end` |

The agent loop **consumes** the first and **emits** the second. `TauBackend` subscribes to the second to drive the TUI. Do not confuse them — both have a notion of "message" and "delta," but they are different shapes.

## End-to-end flow

```
HTTP SSE  ──►  OpenAICompletionsProvider.stream_chat   (tau-ai/providers/openai.py)
                 • parse `data:` lines → chunk dicts
                 • accumulate into _Accumulator (text / thinking / tool_calls)
                 • yield TextDeltaEvent / ToolCallDeltaEvent per chunk
                 • on finish_reason: build final AssistantMessage, yield DoneEvent
                        │
                        ▼  (via client.stream_simple)
            AgentLoop._stream_response                  (tau-agent-core/agent_loop.py)
                 • for each streaming event → emit AgentEvent(message_start/update)
                 • DoneEvent.final  ⇒  the authoritative AssistantMessage  ⇒  emit message_end, return it
                        │
                        ▼
            AgentLoop.run loop body
                 • tool_calls = assistant.get_tool_calls()      ← arguments come from here
                 • _execute_tool_calls → _prepare_tool_call (validate) → _execute_tool
                 • emit tool_execution_start / tool_execution_end
                 • append toolResult messages, loop until no tool calls or max_turns
                        │
                        ▼  (event bus subscription)
            TauBackend.stream_chat / capture_event       (tau-coding-agent/backends.py)
                 • message_update → text delta → callback(delta)
                 • message_end   → collect {"type":"toolCall"} blocks into tool_calls_info
                 • tool_execution_end → attach result text to the matching tool call
                        │
                        ▼
            Parley._get_assistant_response               (tau-coding-agent/app.py)
                 • stream text into a ChatMessage at 30 Hz
                 • display.add_tool_call(name, arguments) / add_tool_result(...)
```

## The four shapes of a "tool call"

A tool call is re-encoded at every boundary. When debugging, follow `arguments` through all four:

1. **Provider** — `tau_ai.types.ToolCall` (pydantic): `{type:"toolCall", id, name, arguments: dict}`. Built in `_build_final_message` from the accumulated `arguments_parts`.
2. **Loop / event** — converted to a plain dict via `model_dump()` at the loop boundary: `{"type":"toolCall","id":...,"name":...,"arguments": {...}}`, carried inside `AgentEvent.message["content"]`.
3. **Backend** — flattened into `tool_calls_info`: `{"id","name","arguments", "result"?, "error"?}`.
4. **TUI** — `arguments` is `json.dumps`-ed into a Markdown code block by `ChatDisplay.add_tool_call`.

The **authoritative** arguments — the ones actually validated and executed — are the ones on the `AssistantMessage` returned by `_stream_response` (shape #1, from `DoneEvent.final`). The `message_update` partials (shape #2 mid-stream) are display-only and may legitimately be incomplete during streaming.

## The accumulation contract (critical)

OpenAI-compatible streaming delivers tool-call arguments as **incremental fragments**: each chunk's `delta.tool_calls[i].function.arguments` is a *piece* of the JSON, and:

- `id` and `name` typically arrive **only on the first** delta for a given call;
- subsequent deltas carry **only `index` + an arguments fragment** (no `id`);
- fragments must be **concatenated in order**, then parsed once complete.

Correct accumulation therefore keys each in-progress call by the `index` field (falling back to `id`) and appends fragments. See pi's `openai-completions.ts` (`ensureToolCallBlock` + `block.partialArgs += …`). A current τ defect violates this contract — see `docs/TOOL-CALL-PARSING-BUG.md`.

## Where to instrument when tool calls misbehave

| Question | Look at |
|---|---|
| Did the server actually send tool-call deltas? | raw chunks in `openai.py` SSE loop (`stream_chat`) |
| Did the provider accumulate valid JSON? | `_build_final_message` `args_str` in `openai.py` |
| Did the loop see tool calls? | `assistant.get_tool_calls()` in `agent_loop.run` |
| Did validation reject them? | `_prepare_tool_call` → `validate_tool_arguments` (`tau_ai/tools.py`) |
| Did the tool receive correct args? | `_execute_tool` in `agent_loop.py` |
| Did the TUI get them? | `capture_event`'s `message_end` branch in `backends.py` |
