# A tool call the server cut off

**Built 2026-08-29.** A repeating `JSONDecodeError: Unterminated string starting
at: line 1 column 12` was killing the turn against a local llama.cpp server. This
document records what that column number proves, why the finalizer used to raise
on it, what it does now, and why grammar-constrained decoding would not have
prevented it.

---

## 1. What was reported

```
RuntimeError: Streaming error from model 'qwen38-27B' at
'http://192.168.0.104:8080/v1': JSONDecodeError: Unterminated string starting at:
line 1 column 12
```

Three things about it, all reported by the operator:

1. It repeats. It is not a one-off malformed generation.
2. The column is always 12.
3. The turn dies. The agent loop gets no chance to do anything about it.

And one thing the message did not say: **which tool call**. "It's probably an
agent tool call but I'm not certain."

## 2. What the column number proves

`json.JSONDecodeError` reports column 12 (char 11) when the string that never
closes *starts* at char 11. For a JSON object that is the opening quote of the
first value, after a 7-character key and a colon with no space:

```
{"command":"ls -la /home     -> Unterminated string ... line 1 column 12
{"pattern":"foo              -> Unterminated string ... line 1 column 12
{"file_path":"/a             -> Unterminated string ... line 1 column 14
{"command": "ls              -> Unterminated string ... line 1 column 13
```

τ's tool schemas with a 7-character FIRST property are `bash` (`command`) and
`grep` (`pattern`). `write` has `content`, but `path` comes first, so its
truncation lands elsewhere. So the calls dying were `bash` calls, and the buffer
ended somewhere inside the shell command.

Nothing about `{"command":"ls -la /home` is malformed. It is a **prefix**. The
model was still writing when something stopped it.

### What stopped it

τ has sent `Model.max_tokens` on the wire since `00601cc` (2026-08-21). Before
that the field was declared and never consulted, so a local server ran with
`n_predict = -1` — unbounded. After it, every request carries a cap.

A model entry that states no `max_tokens` resolves to `DEFAULT_MAX_TOKENS`, which
is **4096** (`backends.py`). The `local-llm` entry in the reporting operator's
`~/.tau/config.json` states none, and sets `enable_thinking: true`. Reasoning is
charged against the same 4096, so the budget can be most of the way gone before
the tool call starts.

I did not observe the failing requests, so I did not verify that these particular
ones carried `finish_reason: "length"`. The arithmetic above and the timing of
`00601cc` are what the diagnosis rests on. §5 says what happens if it is wrong,
which is that the next occurrence says so precisely.

## 3. What τ does about it now

### The provider drops the call instead of raising

`_build_final_message` (`tau_llm/providers/openai.py`) already had this branch for
one cause. An **aborted** stream (the user pressed Esc) leaves a tool call's
`arguments` mid-flight, and handing that buffer to the strict parser raised — the
raise became an `ErrorEvent`, the `ErrorEvent` became a `RuntimeError` out of
`AgentLoop._stream_response`, and every completed message of the turn died with
the frame. `docs/PLAN-0.9.4.md` §3 is that bug.

`stop_reason == "length"` is the same fact with a different cause. The finalizer
already had it and did not read it. It reads it now:

| `stop_reason` | An argument buffer that will not decode |
|---|---|
| `"aborted"` | dropped, counted |
| `"length"` | dropped, counted |
| `"stop"`, `"toolUse"` | **raises**, and now names the call |

Dropped, never repaired and never given `{}`. A half-streamed
`{"command":"rm -rf /ho` must not become an executable call, and inventing an
empty argument set is the anti-pattern the strict path exists to prevent. The
message keeps its `stop_reason`, and `usage.extra["dropped_partial_tool_calls"]`
says how many were lost.

The count is written only when something was dropped. Absent and zero answer
different questions, and a reader of a persisted transcript can tell them apart
only if the happy path writes nothing.

### The TUI says the completion was cut off

Dropping a call the operator cannot see is a silent loss, which is the failure
this repo refuses on both sides. Two surfaces now carry it:

- **The truncation notice.** A `completion_end` carrying `stop_reason: "length"`
  mounts a system box saying so, quoting the cap actually in force, and a toast
  alongside it. `Parley._report_truncation`. The box is durable because a toast
  that has faded cannot be scrolled back to.
- **`dropped=N` in the exchange telemetry row**, beside `t/s` and `repairs`
  (`format_telemetry`). This also covers the abort case, whose drop count has
  been recorded since 0.9.4 and rendered nowhere.

The cap the notice quotes is read from the same config entry the backend resolves
its `Model` from, through the same default, so the number on screen is the number
on the wire. An entry it cannot read reports `max_tokens = unknown` rather than a
stand-in figure: quoting 4096 at an operator whose real cap is something else
sends them to change a number that was already right.

`stop_reason` reaches the TUI on `completion_end`, added to the event
`TurnStream` already built. The agent loop has carried it on `message_end` since
step S8; only `--mode json` was reading it.

### What an operator does

Set `max_tokens` on the model entry in `~/.tau/config.json`. There is no value τ
can infer here — the right cap depends on the server's `n_ctx`, the model, and
how much of the budget reasoning takes.

## 4. A grammar would not have prevented this

The operator's expectation was grammar-constrained decoding for tool calls from
llama-server. It does not address this failure.

Constrained decoding binds **which token may come next**. It does not bind **how
many tokens remain**. A grammar-constrained tool call that reaches the output cap
produces a well-formed prefix of a valid payload, which is exactly as undecodable
as an unconstrained one.

τ already says this about its own constrained generations, in `stream_chat`: a
`stop_reason == "length"` on a constrained call raises `ConstraintViolation`
rather than returning the text, because "the output is a PREFIX of a constrained
answer, not a constrained answer." The tool-call path had no equivalent reading of
the same field. That is what §3 adds.

I did not check whether the reporting operator's llama-server runs with `--jinja`,
so I do not know whether its tool calls are grammar-constrained today.

## 5. What still raises, and what it now says

A complete stream (`stop`, `toolUse`) whose arguments will not decode is a real
fault. `docs/TOOL-CALL-PARSING-BUG.md` is the corruption bug this repo already
fixed once, and "parse leniently" is how it comes back. That path is unchanged.

What changed is what it says. It used to re-raise the bare `JSONDecodeError`, so
`_describe_exception` produced the reported line and nothing else — no tool call
id, no tool name, no buffer, no stop reason. Every other guard in that same loop
quotes the call it is refusing; this one was the exception, which is why a run of
these could not be attributed to a tool at all.

It now raises a `ValueError` naming the call id, the tool, the endpoint, the
`stop_reason`, the original decoder message, and the buffer's length and contents.
Written across several lines on purpose — see §6.

This is also the diagnosis's own escape hatch. If the truncation hypothesis in §2
is wrong and these completions were reporting `"stop"` all along, the drop branch
never fires and the raise says exactly which stop reason arrived and what the
buffer held.

## 6. Reading a long error in the TUI

The operator could not read the whole message: "our markdown widget doesn't allow
sideways scrolling to see the rest of the error."

τ renders a turn failure into a fenced code block — verbatim, because a traceback's
line breaks *are* its stack frames. Textual 8.2.7's `MarkdownFence` is scrollable
(`overflow: scroll hidden`, `allow_horizontal_scroll` returns `True`) but its own
CSS sets `scrollbar-size-horizontal: 0`, so a line wider than the box is clipped
with nothing on screen saying so and nothing to drag.

`parley.tcss` now gives a fence inside a chat message `overflow-x: auto` and a
one-row horizontal scrollbar. `auto` rather than the widget's `scroll` so the row
appears only for a fence that overflows, leaving short code blocks as they were.

The provider's new message is multi-line for the same reason: one long line is the
shape this clips worst.

## 7. What this deliberately does not do

- **No retry.** A truncated call is not re-requested and no error tool result is
  fed back to the model. Feeding one back requires a `toolCall` block in the
  transcript for the result to answer, and its `arguments` is a `dict` — there is
  nothing to put there that is not fabricated. The turn ends, the operator raises
  the cap, and the conversation continues from a transcript that says what
  happened.

- **No repair of the truncated buffer.** `parse_streaming_json` can close an open
  string, and that leniency stays scoped to the live display where it belongs. A
  completed `{"command":"rm -rf /ho"}` is not what the model meant, and running it
  is worse than every other outcome on this page.

- **No change to `DEFAULT_MAX_TOKENS`.** 4096 is low for a reasoning model with a
  large context, and raising it would change what every existing config resolves
  to. That is a separate decision from making the failure legible, which is what
  this is.

- **No inferred cap.** τ does not read the server's `n_ctx` and pick a number.

## 8. Where the pieces are

| Piece | Where |
|---|---|
| the drop | `tau_llm/providers/openai.py` → `_build_final_message`, the `incomplete` branch |
| the raise that names the call | same function, the `parse_json_with_repair_info` guard |
| the operator hint, one wording | `tau_llm/providers/openai.py` → `_TRUNCATION_HINT` |
| `stop_reason` on the render event | `tau_coding_agent/backends.py` → `TurnStream` |
| the notice | `tau_coding_agent/app.py` → `Parley._report_truncation`, `_configured_max_tokens` |
| `dropped=N` | `tau_coding_agent/chat_widgets.py` → `format_telemetry` |
| the scrollable fence | `parley.tcss` → `.chat-message MarkdownFence` |
| tests | `tau-llm/tests/test_abort_finalize.py`, `tau-coding-agent/tests/test_truncated_completion_notice.py` |
