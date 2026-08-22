# Live check of τ's Google client — 2026-08-22

First calls through `tau_llm/providers/google.py` against the real API, rather
than through `scripts/gemini_capability_probe.py`. The probe measured the *wire*;
this measured the *client*.

Free-tier key, `generativelanguage.googleapis.com/v1beta`.

## What was checked

Four checks per model, in ascending order of risk:

| | Check | What it proves |
|---|---|---|
| A | streaming text | `TextDeltaEvent` arrives, `DoneEvent` closes, usage populated |
| B | aggregate-only read | ignoring deltas and using `DoneEvent.final` works |
| C | tool call | `ToolCallDeltaEvent`, and a thought signature captured onto the `ToolCall` |
| D | **multi-turn tool replay** | the replayed `functionCall` is ACCEPTED |

D is the one that mattered. It is the path with no unit coverage — the tests stub
the SDK — and the one Gemini 3 rejects with a 400 if the signature is mishandled.
A, B and C all pass whether or not D works, so nothing before it would have
caught the failure.

## Results

| Model | A | B | C | signature captured | D replay |
|---|---|---|---|---|---|
| `gemma-4-31b-it` | pass | pass | pass | yes | **pass** |
| `gemma-4-26b-a4b-it` | pass | pass | pass | yes | **pass** |
| `gemini-3.6-flash` | pass | pass | pass | yes | **pass** |
| `gemini-3.7-flash` | 503 | pass | 503 | — | not reached |

Every D returned the tool's value in the answer ("The temperature in Paris is
11°C" from an `output` of `"11"`), so the result was not merely accepted — it was
paired to the right call.

`gemini-3.7-flash` returned 503 "This model is currently experiencing high
demand" on two of three requests, with B succeeding in between. That is Google's
capacity, not τ. It did exercise the error path usefully: τ surfaced the 503 as
an `ErrorEvent` naming the model and the endpoint, rather than hanging or
raising through the caller.

## Streaming vs not

τ has no non-streaming path. `Provider` exposes only `stream_chat`, and `"stream"`
is stripped from options on every provider. Check B is therefore what "not
streaming" means for a τ caller: ignore the deltas, read `DoneEvent.final`. Both
readings work on every model reached.

Deltas arrived as a single chunk for these short answers (`deltas: 1`), so this
run does NOT demonstrate multi-chunk text assembly. The unit tests cover
fragment accumulation; a long generation would exercise it live.

## What this changed in the client

**Automatic function calling was on by default, and is now explicitly off.**

The SDK logged "direct use of automatic function calling (AFC) is not
recommended" on every streamed tool request. Checking rather than assuming:
`_extra_utils.should_disable_afc()` answers `False` for a config without the
flag and `True` with it. So the SDK's own tool loop was enabled.

It had nothing to execute, because τ passes function declarations rather than
Python callables. But "inert because of how we happen to call it" is not "off",
and AFC running would bypass τ's tool execution, its
`tool_execution_start`/`tool_execution_end` events, and its permission checks.
Two tests now pin the flag on tool requests and its absence on plain ones.

The SDK still logs that warning either way — it is emitted once per process
before the flag is consulted, so it is not a signal about this setting.

## Not covered

* No 1M-token or long-generation run, so multi-chunk text assembly is unproven live.
* No image input, no parallel tool calls in one turn, no `reasoning_replay="all"`.
* No Gemini below major 3 — none is callable on a new key (see the probe record).
