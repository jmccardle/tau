# v0.11.0 — persistence stops freezing the screen, and a compaction stops being a toast

Two units, and a minor bump rather than a patch because the first one breaks an
interface extension authors use.

---

## BREAKING: the `SessionLog` appenders are coroutines

The eight `SessionLog` appenders are `async def`. So are three `ExtensionAPI`
methods. `id`, `cursor` and `entries()` are unchanged.

| Was | Is |
|---|---|
| `api.append_entry(...)` | `await api.append_entry(...)` |
| `api.send_message(...)` | `await api.send_message(...)` |
| `api.request_user_action(...)` | `await api.request_user_action(...)` |
| `log.append_*(...)` (eight methods) | `await log.append_*(...)` |

**Extension authors are the larger audience here**, not foreign `SessionLog`
implementors. If an extension calls any of those three, it needs an `await` and
its caller needs to be `async`.

The freeze this fixes: the agent loop runs on the head's own event loop, so the
JMFTS store's ~21 synchronous HTTP appends per tool-bearing turn froze painting
and input for the length of persistence. This is `docs/BLOCKING-PERSISTENCE.md`
§3's Option B, recommended on 2026-08-28 and built now.

**An `async` signature moves nothing off the loop by itself**, and the three
shipped stores answer it differently. `InMemorySessionLog` and the file
`Session` run to completion and await nothing; only `JmftsSessionLog` hops to a
thread. That hop is safe for three reasons local to that one store — an
`httpx.Client` it owns, the Protocol's one-writer precondition, and callers that
await one append before the next — and none of the three holds for a foreign
implementor. So the obligation is the store's, not the contract's.

Two consequences worth knowing before you write one:

- **`AgentSession.__init__` cannot await**, so the `agent_spec` entry is queued
  and written by the first method that appends. `AgentSession.start()` is the
  awaited door for a reader that has not prompted yet; `tau --mode rpc` calls it
  once before `RPCHandler.run`.
- **The RPC cursor invariant was re-established, not inherited.** Persistence can
  now suspend part-way through, and with a suspending store the wire carried
  `cursor: null` — the stale-tip failure E5/F3 exist to prevent, measured.
  `AgentSession.persistence_settled` states the ordering and
  `RPCHandler.await_outbound_prerequisites` waits on it before an `agent_end` is
  framed. The gate fails when that wait is removed.

---

## Side work becomes visible

A compaction or a branch summary used to be a toast that said `Compacting…` and
then a second one that said it finished. The tokens it spent were reported
nowhere, and the work itself was a pause.

**It now opens a box in the transcript and streams into it**, carrying the
summariser's model while it works and what it spent when it finishes. A
*reloaded* summary draws the same box, so watching one happen and scrolling back
to it look alike. On a failure the box says so rather than closing silently.

`side_completion_start` / `_update` / `_end` join the event vocabulary and the
RPC wire (**protocol 1.6 → 1.7**), carrying `purpose` (`compaction` or
`branch_summary`) and `reason` (`manual`, `threshold` or `navigate`) — so a host
can tell a fold someone asked for from one the auto-trigger imposed.

### `/compact` was undoing itself

`/compact` ran a **different** compaction from the auto-trigger. The manual one
was count-based, wrote no durable entry, and assigned the head's working list
directly — so the next turn's `completion_end` re-derived that list from
`session.context` and the compaction was silently gone. It lasted exactly one
turn.

`/compact` now runs the same token-budget compaction the auto-trigger runs,
which appends a `compaction` entry the next turn cannot undo. The count-based
cut is **retired rather than ported**: `ROADMAP.md` carries what it would need
to come back as, hooked to a prompt-cache miss rather than to a keystroke.

### The header's `ctx` number answered the wrong question

It read the last completion's reported prompt size — a measurement of the
*previous* request, which stops being what the next one carries the moment a
fold drops a prefix. That is why compacting appeared to change nothing.

It now keeps the measurement while it is still true and falls back to the
estimator `should_compact` already decides on once a summary sits after the last
measured completion, so the header and the auto-trigger answer one question from
one source.

### The subtitle stopped covering its own numbers

Nine sites wrote a gerund straight over the header subtitle — `Compacting…`,
`Thinking… (Esc to cancel)`, `Summarizing branch…`, and six more — so the
context size and the token arrows went off screen for exactly the operations
that move them. An activity is a **term** now:

```
Compacting… · local-llm · 1 tool · ↑500 ↓42 R100 · 500 ctx
```

### Two smaller ones

- The empty chat names the **live** model, so picking "new chat with …" from the
  command palette changes what the front page says.
- A tool call's arguments no longer ride `--mode json` three times over (below).

---

## `--mode json` sent a 10 KB answer as 10.6 MB

`AgentEvent.message` holds the whole assistant message so far, and the agent loop
re-emits it on every fragment. `--mode json` serialized that verbatim, so the
stream was **quadratic** in the answer's length:

| Answer | Before | After | |
|---|---|---|---|
| 2,500 chars | 778 KB | 123 KB | 6.3× less |
| 5,000 chars | 2.8 MB | 244 KB | 11.5× less |
| 10,000 chars | **10.6 MB** | **486 KB** | **21.8× less** |

After, doubling the answer doubles the bytes — 1.98×, then 1.99×. There is no
backpressure on this stream, which is what made the size a real problem rather
than an aesthetic one.

The fix is τ's own rule applied to the surface that skipped it: `--mode json`
emits prefix-diffs through the same `MessageDeltaProjector` the RPC wire has used
since unit 2B. A `message_update` now carries `delta`, `block_type` and
`replace`; an unchanged block and a streaming `toolCall` produce no line at all,
because the call's identity already rides `tool_execution_start` and its
arguments ride `tool_execution_end`.

Two dead keys are gone too: `blocked` and `is_error` default to `False` rather
than `None`, so `exclude_none` could not drop them and every line of every type
carried both.

**The gate is falsifiable and was falsified.** It renders 2,000 and 4,000
characters and asserts the ratio is under 2.2; against the old shape it reports
3.7× and fails.

### `pi-faithful --mode json` is struck

The roadmap carried an objective to make `--mode json` emit pi's
`AgentSessionEvent` schema. It is retired, on two measurements:

- **Nothing speaks pi's schema.** pi's only consumer is pi's own in-process
  `rpc-client.ts`. τ's only consumers read three type names off τ-snake fields.
  Claude Code's `--output-format stream-json` is a third schema again.
- **Adopting it would cost.** pi's events carry no `timestamp` and none of the
  four provenance fields τ stamps on every event — which is exactly what a parent
  multiplexing several children over this stream needs.

`CLAUDE.md` retired pi parity on 2026-09-03; this entry outlived that and was
still stated in pi's terms. pi having hit the quadratic defect too (its
regression #7290, whose 2.2 bound the gate above borrows) is evidence the defect
is real, not a reason to copy anything else.

---

## Also in this release

- **The leakage scan reaches the test trees.** Since 0.10.3 a push publishes
  every tracked file, and `tau-*/tests/` was in neither root tuple of
  `test_no_host_addresses.py` — the one published surface nothing walked. It has
  its own scope now, held against the strict pattern rather than the prose one,
  because a fork both reads those files and runs them.
- **The render path is re-derived from the code.** `docs/TOOL-CALL-PIPELINE.md`
  and `docs/tau-coding-agent.md` both drew a TUI that streamed text deltas
  through a callback and painted at 30 Hz. Neither has been true since B3-a.
- **Two `@agent_facing` docstrings named methods 0.10.0 removed** —
  `ctx.ui.confirm` and a cross-reference to it. Both were published twice over,
  in `docs/library/reference/` and in the site's exported copy.

## New design records

* `docs/STREAMING-SIDE-WORK.md` — the side-completion vocabulary, the `/compact`
  defect, the `ctx` number, the subtitle.
* `docs/JSON-MODE-DELTAS.md` — the measurement, the fix, and what pi's session
  events were worth reading for.
