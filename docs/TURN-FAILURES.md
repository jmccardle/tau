# A turn that fails keeps what it produced

Built 2026-10-03. A turn that raises leaves a `turn_error` message the user sees
and the model does not. A stream that fails partway is written as the message it
was, with `stop_reason: "error"`. A tool says which of its failures the model
reads by raising `ToolError` or `ToolHalt`.

## 1. What was wrong

tau-code reported it on 2026-10-03: after a turn failed, a reload showed the
prompt with nothing under it. Two paths in `AgentLoop._stream_response` lost
content.

- **A provider error after content had streamed.** The first delta opens an
  incomplete `message` entry with `content: []`. The `ErrorEvent` branch emitted
  a made-up `"Error: …"` assistant message to the heads, never finalized the
  entry, and raised. The text, reasoning and tool-call deltas held in memory
  were dropped. On disk was one empty incomplete entry, and a reload moved to the
  prompt above it (`docs/TAU-SERVE.md` §4.3).
- **A stream that ended without a `DoneEvent`.** The same, with no event to
  show the heads.

A provider error before any content wrote nothing but the prompt. The error was
in `agent_end` and in the daemon's log line, and in no entry, so a reload could
not say why the prompt had no answer.

One more path sent the model text it should not have read. A tool that raised
anything at all became an `is_error` result holding `str(exc)`. That is right
when the tool reports a failure on purpose, and wrong when the exception is a
bug in the tool or in τ.

## 2. A failed stream is written as what it was

Every delta event carries `partial`, the provider's own accumulation of the
message so far (`tau_llm.streaming`). The loop keeps the latest one. When the
stream fails, by an `ErrorEvent`, by ending without a `DoneEvent`, or by an
exception out of the iterator, `AgentLoop._stream_failed`:

1. finalizes the open entry with that partial, `stop_reason: "error"` and
   `error_message`;
2. emits `message_end` with the same message, in place of the made-up one;
3. answers each tool call in it with an error result, `UNRUN_TOOL_RESULT`,
   through the same `_aborted_batch` an abort uses. Every provider rejects a
   tool call with no result, so without this the session could not continue.

**Built note (2026-10-03, found by tau-code against a stub server).** The OpenAI
SSE transport never reached this path in two cases. It skipped a mid-stream
`data: {"error": {...}}` frame, which has no `choices` and is what OpenAI,
OpenRouter and vLLM send when they fail after the 200. And it finalized a stream
that closed with neither a `finish_reason` nor `[DONE]` as `stop_reason:
"stop"`, so the model read a half sentence as a whole answer. Both now yield an
`ErrorEvent` (`_stream_transport`). Either end marker alone is still a complete
stream, because servers differ in which they send.

The message is not filtered out of the next request. An interrupted message is a
valid message, and the model reads what it said. No provider drops a
`stop_reason: "error"` message on replay, so nothing in `tau-llm` changes.

The exception keeps its type. A `ValueError` out of the iterator is re-raised
as itself, and an `ErrorEvent` is still a `RuntimeError`, because a caller that
catches one specifically must keep working (`test_abort_persistence.py`). The
messages written for it ride `STREAM_RECORDED_ATTR` on the exception and join
`completed_messages(exc)`.

**Alternative rejected: an assistant message with `stop_reason: "error"` that is
filtered from model input.** That was the first proposal. It made an exception
to "the model's input is the path" for a message that is valid as it stands.

## 3. A tool chooses what the model reads

| A tool… | The model reads | The user also sees | The turn |
|---|---|---|---|
| returns an error value (bash exit 1) | the value | — | goes on |
| raises `ToolError(msg)` | `msg` | — | goes on |
| raises `ToolHalt(msg)` | `msg` | — | ends after the batch, `end_reason: "terminate"` |
| raises anything else | `INTERNAL_TOOL_ERROR` | `details["exception"]` | goes on |

`AgentToolResult.from_exception` is the one place this is decided. Every site
that turns a tool exception into a result uses it: `_execute_tool`, the parallel
executor's after-hook path, and `ErrorCall` from preparation.

The exception goes in `details`, which every provider ignores: OpenAI reads the
call id and content, Anthropic the id, `is_error` and content, and Google the
name, id, `is_error` and content. `tau_agent_core.projections.tool_result_for_user`
composes the text a head shows, and the TUI's live path, its reload path, the
REPL replay and `tau --connect` all call it.

`ToolHalt` maps onto the existing `terminate` flag rather than a new
`end_reason`. A new value would change the event schema on both wires, and a
client tells a halt from a normal stop by the result's `is_error`.

The built-in NATS bus extension was the one tool in the tree that raised on
purpose. Its empty-field, no-ack and failed-ack errors are now `ToolError`; a
missing connection and a malformed ack stay internal.

## 4. `turn_error`

`AgentSession` catches `Exception` around `loop.run` and `loop.run_continue`,
appends `turn_error_message(exc)` through the turn's writer, and re-raises. It
is a `custom` message with `customType: "turn_error"`, `display: true` and
`visibleToModel: false`, which is the display-only shape `api.send_message`
already uses. The TUI's reload (`MessageList.add_persisted_message`), the REPL's
replay and tau-code (`packages/ui/src/messages.ts`) already render a displayed
custom message, and `convert_to_llm` drops it.

It is written for every turn that raises, including one whose partial message
was kept, so a head needs one rule: a turn that failed ends with a `turn_error`.
`CancelledError` is not an `Exception` and writes nothing, because an abort is
not a failure.

**Alternative rejected: a `customEntry`.** The model never sees one either, but
no head renders it on reload, so each would need a new renderer.

## 5. Absent

- **Live `error_message` on the wire.** `WireEvent` carries `message_end`'s
  `stop_reason` and not its `error_message`. A live client reads the error from
  `agent_end`; a reload reads it from the entry. Adding the field is a protocol
  bump on both wires.
- **A head crash.** Under `tau serve` the daemon writes, so a client crash does
  not touch the store. A local TUI process that dies mid-stream still loses the
  text of the streaming message (`docs/TAU-SERVE.md` §4.2).
- **Parallel calls when one halts.** The other calls in the batch run to
  completion; `ToolHalt` does not cancel its siblings.
