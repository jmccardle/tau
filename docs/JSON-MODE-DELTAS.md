# `--mode json` sends deltas, and stops chasing pi

**Status: built 2026-09-17.**

Two things were bundled under one roadmap item, `pi-faithful --mode json`, and
they had opposite answers. One was a real defect in τ's own stream, measured and
fixed here. The other was an objective that outlived its premise and is struck.

---

## 1. The defect: a 10 KB answer wrote 10.6 MB

`AgentEvent.message` holds the **whole assistant message so far**, and the agent
loop re-emits it on every fragment (`agent_loop.py:806-814` — `partial_text`,
not the delta). `--mode json`'s serializer was `event.model_dump(exclude_none=True)`,
so every `message_update` line carried the entire answer to date.

The stream was quadratic in the answer's length. Measured by driving the real
`TauBackend` bus through `run_print` with the LLM boundary faked, the way
`test_json_mode.py` does:

| Answer | Before | After | |
|---|---|---|---|
| 50 chars | 5.5 KB | 4.5 KB | |
| 500 chars | 57 KB | 26 KB | |
| 2,500 chars | 778 KB | 123 KB | 6.3× less |
| 5,000 chars | 2.8 MB | 244 KB | 11.5× less |
| 10,000 chars | **10.6 MB** | **486 KB** | **21.8× less** |

After: doubling the answer doubles the bytes — 1.98×, then 1.99×.

There is no backpressure on this stream either (`headless.py` writes and flushes
unconditionally), which is what made the size a real problem rather than an
aesthetic one.

## 2. The fix is τ's own rule, applied to the surface that skipped it

`rpc/wire_events.py` has solved this since unit 2B: `MessageDeltaProjector` turns
the cumulative snapshots into per-block deltas, and `WireEvent` excludes
`message` outright because nothing unbounded goes on a stream a host cannot
backpressure. `--mode json` never got the fix.

`backends.tau_event_to_pi_event` is replaced by `backends.json_mode_payloads`,
which takes a projector and returns a **list** of lines, for the same reason
`project_event` does — one `message_update` is not one line:

| Input | Lines out |
|---|---|
| A text or thinking fragment | one, carrying `delta` / `block_type` / `replace`, with `message` removed |
| A re-sent unchanged block | none |
| A `toolCall` streaming its arguments | none |
| Anything else | one, unchanged |

The `toolCall` rule matches the RPC wire's and for the same reason: the call's id
and name already ride `tool_execution_start` and its arguments ride
`tool_execution_end`, so the partial block was a third transmission of what two
other events carry — and it is the O(n²) one.

Two dead keys are gone as well. `blocked` and `is_error` are `bool` with a
`False` default rather than `None`, so `exclude_none` could not drop them and
every line of every type carried both.

**The projector is per-sink, not shared.** `stream_submission` builds one for
the JSON capture rather than reusing `TauBackend._delta_projector`: the TUI's is
reset on its own lifecycle, and two consumers sharing one accumulator would each
see the other's resets.

### The gate

`test_the_stream_scales_linearly_with_the_answer` renders 2,000 and 4,000
characters and asserts the ratio is under 2.2. It is falsifiable and was
falsified: `ran` against the old `model_dump` shape it reports **3.7×** and
fails.

The 2.2 bound is pi's, from its own regression #7290
(`test/suite/regressions/7290-json-stream-linear.test.ts`). pi shipped this
defect, hit it, and gated it. That is evidence the defect is real — not a reason
to adopt anything else of pi's.

## 3. What was struck, and why

The objective was to make `--mode json` emit pi's `AgentSessionEvent` schema.
It is retired. Two measurements settle it.

**Nothing speaks pi's schema.** `ran grep` for consumers: pi's only one is pi's
own in-process `rpc-client.ts`. τ's only ones are `examples/ext_kit/`, which read
three type names — `turn_start`, `message_end`, `tool_execution_start` — off
τ-snake fields, and never touch `message_update` at all. Claude Code's
`--output-format stream-json` is a third schema again, which matching pi would
not get you.

**Adopting it would cost.** The two vocabularies overlap in their frame — a
`type: "session"` header line first, then type-discriminated JSONL, and ten
shared type *names* — and in nothing else:

| | τ | pi |
|---|---|---|
| Field case | `tool_call_id`, `is_error` | `toolCallId`, `isError` |
| `timestamp` | on every event | absent from the union |
| Provenance | `submission_id`, `source`, `submitter`, `correlation` | none |
| `turn_start` | carries `turn_index` | carries nothing |
| `agent_end` | `end_reason`, `error` | `willRetry` |
| Session-level types | 3 (`side_completion_*`) | 14 |

Dropping `timestamp` and the provenance quartet would break exactly the use
`examples/51_delegate_fleet.py` describes — a parent multiplexing several
children over this stream, which is what `submission_id` exists for.

`CLAUDE.md` retired pi parity on 2026-09-03. This entry outlived that and was
still stated in pi's terms.

## 4. What pi's session events were worth reading for

Not copying — reading. Two findings.

**`compaction_start` / `compaction_end` carry a `reason`.** τ's
`side_completion_*` did not, so a reader watching a compaction could not tell one
they asked for from one the auto-trigger imposed. `SideCompletionReason` now
rides all three events and the RPC wire: `manual` (`/compact`, the `compact`
verb), `threshold` (`_maybe_auto_compact`), `navigate` (the tree browser's
summarising move). `_perform_compaction` takes it as a **required** parameter —
a default would be one caller's answer standing in for the other's.

pi has a third, `overflow`: compact and retry after a request came back over the
window. τ has no such recovery path, so declaring the value would put a branch on
the wire that nothing can reach.

**`entry_appended` is narrower than it looks, and τ's gap is real.** It fires
from exactly one place in pi — `ExtensionAPI.appendEntry` — and pi's only
consumer renders it when `entry.type === "custom"`. It is not a general "the log
grew" event.

τ has the parallel for `send_message` (the `custom_message` channel, which
`RenderRouter` subscribes to) and **nothing** for `append_entry`:
`AgentSession._append_custom_entry` announces on no channel. So a durable
`customEntry` an extension writes is invisible to every head until a reload. Not
fixed here; it belongs with the RPC wire rather than with JSON mode.

## 5. What this did not do

- **`--mode json` and the RPC wire still have two projections.** They now share
  the projector and the drop rules, but not the payload shape: `WireEvent`
  carries `cursor` and `message_count` a print run has no use for, and
  `--mode json` carries a header line and a `timestamp` RPC does not. pi shares
  one type between them and only had to fix #7290 once. Whether τ should
  converge is open.
- **No backpressure.** pi's print mode awaits
  `waitForRawStdoutBackpressure()` between events. τ writes and flushes. The fix
  above makes the volume survivable; it does not make the stream governable.
- **`side_completion_*` still renders in no head but the TUI**, including this
  one (`docs/STREAMING-SIDE-WORK.md` §7). The events reach `--mode json` and the
  wire; nothing reads them there.

## 6. Cross-references

* `docs/STREAMING-SIDE-WORK.md` — where `side_completion_*` came from; §3 is the
  vocabulary this adds `reason` to.
* `docs/REMOTE-CONTROL.md` — D3, why `WireEvent` is a declared projection rather
  than a dump of the model.
* `~/Development/pi` — provenance, never authority (`CLAUDE.md`).
