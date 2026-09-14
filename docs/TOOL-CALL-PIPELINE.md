# Reference: The Tool-Call Streaming Pipeline

Re-derived from the code 2026-09-13. How a single user turn travels from HTTP bytes to a rendered tool call/result, across the three packages. This is the subsystem most worth understanding because it crosses **three event vocabularies** and transforms a "tool call" four times.

## Three event vocabularies

| Vocabulary | Defined in | Producer | Examples |
|---|---|---|---|
| **τ-llm streaming events** | `tau_llm/streaming.py` | the provider (`openai.py`) | `TextDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent` |
| **τ-agent-core AgentEvents** | `tau_agent_core/events.py` | the agent loop / event bus | `agent_start`, `turn_start`, `message_start`, `message_update`, `message_end`, `tool_execution_start`, `tool_execution_end`, `turn_end`, `agent_end` |
| **render events** | `tau_coding_agent/backends.py` | `TurnStream` / `RenderRouter` | ten dicts keyed by `"kind"`: `lane_start`, `turn_start`, `steer_message`, `text_delta`, `reasoning_delta`, `tool_call`, `tool_result`, `completion_end`, `lane_end`, `custom_message`. Nine carry a lane; `custom_message` deliberately does not (`RenderRouter.on_custom_message`), because a message `api.send_message` sent with no turn in flight belongs to the transcript rather than to a completion |

The agent loop **consumes** the first and **emits** the second. `RenderRouter` subscribes to the second — once, for the life of the session, via `Backend.subscribe_render` — and normalizes it into the third, one `TurnStream` per lane. **The TUI** renders the third and never the second; the other two heads do read the second directly — `tau -p --mode json` maps each raw `AgentEvent` to pi's shape (`headless.py`, `tau_event_to_pi_event`) and `tau --mode rpc` ships `AgentEvent`s on the wire. Do not confuse them: all three have a notion of "message" or "delta," and they are different shapes.

## End-to-end flow

```
HTTP SSE  ──►  OpenAICompletionsProvider.stream_chat   (tau-llm/providers/openai.py)
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
                        ▼  (persistent subscribe_render subscription)
            RenderRouter → TurnStream.feed               (tau-coding-agent/backends.py)
                 • message_update → MessageDeltaProjector → {"kind": "text_delta"/"reasoning_delta"}
                 • tool_execution_start / _end → {"kind": "tool_call"} / {"kind": "tool_result"}
                 • message_end → {"kind": "completion_end"}: usage, stop_reason, dropped_tool_calls
                 •                tool widgets come off tool_execution_*, never off message_end
                        │
                        ▼
            TauApp._on_render_event                      (tau-coding-agent/app.py)
                 • await ChatDisplay.handle_stream_event(event) — every delta, no throttle
                 • text_delta → MessageBox.append_content_delta → MarkdownStream.write
                 • tool_call → MessageBox.add_tool_call(...) → ToolBox; tool_result folds in
```

`_on_render_event` is attached to the SESSION, not to a call: it draws every lane the session runs, including a forked sub-agent's and a turn a bus, timer or extension submitted, which is why `TauApp._get_assistant_response` now only awaits its own submission and renders nothing. The `callback(delta)` shape the TUI used before B3-a still exists one layer down — `TauBackend.stream_submission` feeds the same `TurnStream` and calls `callback` per text delta — but only `tau -p` and the SDK-shaped callers use it; a head that subscribed as well would draw every token twice.

## The four shapes of a "tool call"

A tool call is re-encoded at every boundary. When debugging, follow `arguments` through all four:

1. **Provider** — `tau_llm.types.ToolCall` (pydantic): `{type:"toolCall", id, name, arguments: dict}`. Built in `_build_final_message` from the accumulated `arguments_parts`.
2. **Loop / event** — converted to a plain dict via `model_dump()` at the loop boundary: `{"type":"toolCall","id":...,"name":...,"arguments": {...}}`, carried inside `AgentEvent.message["content"]`.
3. **Render event** — `TurnStream.feed` emits `{"kind":"tool_call","lane",…,"id","name","arguments"}` off `tool_execution_start`, and a matching `{"kind":"tool_result",…,"result","is_error","blocked"}` off `tool_execution_end`. The `message_end` toolCall blocks are harvested separately (deduped by id) into `TurnStream.tool_calls` for chat persistence — `stream_submission` returns that list as `tool_calls_info` — and are never rendered, because the loop emits `message_end` twice per tool-bearing turn.
4. **TUI** — `arguments` is `json.dumps`-ed into a Markdown code block by `ToolBox._args_block`, which mounts on the box's first expand.

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
| Did validation reject them? | `_prepare_tool_call` → `validate_tool_arguments` (`tau_llm/tools.py`) |
| Did the tool receive correct args? | `_execute_tool` in `agent_loop.py` |
| Did the TUI get them? | `TurnStream.feed`'s `tool_execution_start` branch (`backends.py`), then `TauApp._on_render_event` → `ChatDisplay._on_tool_call` |
| Did the text reach the screen? | `TurnStream._feed_message_update` (`backends.py`), then `ChatDisplay._on_text_delta` → `MessageBox.append_content_delta` |
