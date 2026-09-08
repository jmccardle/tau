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
  alongside it. `TauApp._report_truncation`. The box is durable because a toast
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

### 3.1 Three deliveries, one verdict

**Built 2026-09-07.** The heading above this one says "The TUI says", and that was
the whole of it: `tau -p` printed a truncated answer and said nothing, and an RPC
host could not see the stop reason at all because it rides inside `message`, which
`WireEvent` excludes. τ is headless and the TUI is one head
(`docs/HEADS-AND-MULTIPLEXER.md` §1), so a fact only one head can report is a fact
τ does not really carry — the same shape `docs/PROMPT-CACHING.md` §7 closed for
the prompt cache, and closed here the same way.

`tau_agent_core/truncation.py` is the reading, and it is pure: no clock and no
session state, because one completion's `stop_reason` is already the whole fact.
`truncation_notice` produces the sentence all three heads say; the cap it quotes
is the head's, because the head is what knows which config entry this run
resolved.

| Head | Where it lands | Unit | Carries the cap |
|---|---|---|---|
| TUI | a system box, plus a toast | one truncated completion | yes, from the config entry |
| `tau -p` | two lines on **stderr** | the whole turn's messages | yes, from `model_config` |
| `tau --mode rpc` | two fields on `message_end` | one completion | no — it ships the fact, not a sentence |

stderr for the reason `report_cache_miss` gives: stdout is a transcript in
`--mode text` and JSONL in `--mode json`, and both are routinely redirected, so a
diagnostic on either is corruption.

**The wire gets the FACT, not the sentence.** This is the one place the truncation
report diverges from `cache_notice`, which ships a sentence. A cache verdict needs
a clock, a latch and a threshold, and the evidence it reads is not on the wire
either, so a host cannot recompute it. A stop reason is a closed five-value enum
that a host should be able to branch on — `if stop_reason == "length"` — rather
than match a string against. So `WireEvent` gains two bounded fields lifted out of
the excluded `message`: `stop_reason`, redeclared as its own `Literal` with an
anti-drift test against `AssistantMessage.stop_reason` exactly as `type` has
against `AgentEvent.type`; and `dropped_tool_calls`, which is **null rather than
0** when nothing was dropped, so "none lost" and "not reported" stay distinct on
the wire the way §3 already keeps them distinct in `usage.extra`.

Excluding a whole `message` is about size. A closed enum and a small integer are
neither unbounded nor pullable in time to matter: a host that learns from
`get_messages` that the answer it already rendered was a prefix learns it too
late.

**The box now counts what was lost.** `completion_end` carries
`dropped_tool_calls` beside `stop_reason`, so the TUI says "2 tool calls were
dropped rather than run on a truncated argument list" instead of the older
hedge, "including a tool call, which is dropped". The count was already in
`usage.extra`; only the telemetry row read it.

**A turn reads as more than one completion.** `truncation_from_messages` SUMS
where `prompt_tokens` must not: two truncated completions are two separate losses,
not one conversation counted twice. Print mode holds finished messages and uses
it; the TUI holds a live event and reports `Truncation(1, n)` per completion.

**An abort's drops are not the cap's.** `completion_truncation` reports
`Truncation(0, 0)` for a message whose `stop_reason` is `"aborted"` even when it
dropped calls. The count is real and the telemetry row still shows it, but this
notice tells an operator to raise a cap, and an Esc the user pressed is not a cap
to raise.

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

`tau.tcss` now gives a fence inside a chat message `overflow-x: auto` and a
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

- **The cap is not on the wire** (added 2026-09-07). `message_end` says the
  completion stopped at the cap and how many calls that cost; it does not say what
  the cap was. A `max_tokens` field would be the model's configuration on an event
  stream, which is a different question from what this turn did, and the two heads
  that quote a number read it from the config entry they themselves resolved. I
  did not check whether any existing verb hands a host its model's `max_tokens`.

- **No per-session suppression.** The prompt-cache notice is said once per model
  because its subject is a configuration that will not change mid-session. This
  one is said every time, because every truncated completion is a separate answer
  the reader did not get.

## 8. Where the pieces are

| Piece | Where |
|---|---|
| the drop | `tau_llm/providers/openai.py` → `_build_final_message`, the `incomplete` branch |
| the raise that names the call | same function, the `parse_json_with_repair_info` guard |
| the operator hint, one wording | `tau_llm/providers/openai.py` → `_TRUNCATION_HINT` |
| the reading, head-agnostic | `tau_agent_core/truncation.py` |
| `stop_reason` + the drop count on the render event | `tau_coding_agent/backends.py` → `TurnStream` |
| the TUI notice | `tau_coding_agent/app.py` → `TauApp._report_truncation`, `_configured_max_tokens` |
| the print-mode notice | `tau_coding_agent/headless.py` → `report_truncation` |
| the wire fields | `tau_agent_core/rpc_event_schema.py` → `WireEvent`; `rpc/wire_events.py` → `_truncation_fields` |
| `dropped=N` | `tau_coding_agent/chat_widgets.py` → `format_telemetry` |
| the scrollable fence | `tau.tcss` → `.chat-message MarkdownFence` |
| tests | `tau-agent-core/tests/test_truncation.py`, `tau-coding-agent/tests/test_truncated_completion_notice.py`, `tau-coding-agent/tests/test_headless_truncation_notice.py`, `tau-llm/tests/test_abort_finalize.py` |
