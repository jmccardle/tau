# Side work becomes visible

**Status: built 2026-09-16.**

τ makes completions on its own behalf. A compaction summary and a branch summary
are both real model calls: they spend tokens, they take as long as a turn, and
they produce text the reader wants. Neither belonged to any vocabulary τ had.
The TUI showed a toast, the numbers went nowhere a reader could see them, and
`/compact` — the one a person triggers deliberately — undid itself on the next
turn.

This record holds what that cost, what replaced it, and the one thing it
deliberately did not build.

---

## 1. Four defects, one shape

| | Before |
|---|---|
| Watching a compaction | a toast saying `Compacting conversation…`, then one saying it finished. Nothing in between, for as long as a summary takes. |
| What it cost | nowhere. `record_side_usage` banks it on the session's side ledger; the header reads only `self.messages`, so the number never reached a screen. |
| The compaction in the tree | for auto-compaction, a `compaction` entry the browser already draws. For `/compact`, nothing at all. |
| The summary in the transcript | a `user` message reading `[[Compaction summary: …]]`, in the same box as a line the reader typed. |

They share a shape: the work happens outside the agent loop, and everything that
reports on τ is written against the agent loop.

## 2. `/compact` was undoing itself

The sharpest of the four, and the reason this unit exists.

There were **two** compactions. `AgentSession.compact()` cut on the token budget
and appended a `compaction` entry. `AgentSession.compact_messages()` cut on a
message count, wrote nothing, and returned a shortened list. `/compact` called
the second.

`app.py` re-derives `self.messages` from `self.current_session.context` at every
`completion_end` and at every session swap — thirteen assignments, twelve of them
from the session. `action_compact` was the one that was not. So:

1. `/compact` shortens the working list.
2. The next completion ends.
3. `self.messages = list(self.current_session.context)` — read from a tree with
   no compaction entry in it.
4. The full history is back, and nothing said so.

The measured tokens did not move because nothing had durably changed.

**`/compact` now calls `AgentSession.compact()`**, the same one the auto-trigger
runs. `TauBackend.compact_messages` is gone and `TauBackend.compact` replaces it;
the head re-reads `session.context` afterwards like every other path. The cut is
an entry, so it is also a node in the tree browser, which has styled `compaction`
rows since 0.9.7 and had nothing to draw for a manual one.

**What was lost, and where it went.** The count-based cut is a different feature,
not a worse version of this one: "shrink the prompt on a conversation that has
not outgrown its window". It is in `ROADMAP.md` under its own name, with a
trigger it did not have before — fire it when the prompt cache has expired, since
that request pays full price for input anyway and is the cheapest moment to send
less.

## 3. The vocabulary: `side_completion_*`

Three event types on the existing bus, and one new `AgentEvent` field group.

| Event | Carries |
|---|---|
| `side_completion_start` | `purpose`, and the summariser's model id in `message` |
| `side_completion_update` | `delta` — one text fragment |
| `side_completion_end` | `text`, `usage`, or `is_error` + `error` |

`purpose` is `"compaction"` or `"branch_summary"`. Side work stamps no
`submission_id` — it belongs to no turn — so `purpose` is what a renderer keys
on instead.

`AgentSession.watch_side_completion` is the bracket. **Every exit emits the end,
including an exception**, which then propagates: a renderer opens a box on the
start and has no other signal that the work is over, so a failure that emitted
nothing would leave that box open for the life of the session.

The two no-op returns in `_perform_compaction` — an empty conversation, or one
already ending in a compaction — emit **nothing**, not an empty pair. No tokens
were spent and no text was produced.

**`finish()` is separate from the fragments, because they are not the same text.**
Compaction stitches the read/modified file lists onto the summary after the
completion returns, and a split turn stitches on a second summary that never
streamed. So the accumulated deltas are a *prefix* of the result. The end event
carries what was actually written, and
`test_the_summary_the_end_carries_is_what_was_written_to_the_log` holds the two
equal.

**A split turn streams the history summary only.** The two completions run
concurrently (`asyncio.gather`), so feeding both into one sink would interleave
two documents into unreadable text.

### The transport already streamed

`complete_simple` is `stream(...).result()` — it has always driven a stream and
thrown the deltas away, and said so: *"which has no streaming UI to feed"*. It
gained an `on_text_delta` sink; passing `None` keeps the collapsed path
byte-for-byte, and nothing iterates the stream.

`resolved_complete` forwards the sink **only when it is not None**, so a
three-argument test double keeps working. A `complete_fn` that cannot take one
and is handed one raises `TypeError`, deliberately: the alternative is a double
that silently does not stream, which would make a streaming test pass vacuously.
Fifteen doubles in the suite grew the parameter.

## 4. Two things it broke, both caught by the suite

**The latency module's compaction marker.** `latency.py` marks a prompt as
compaction-bearing by finding an `agent_start`/`agent_end` pair *with nothing
between it* — its own docstring calls this "a sound necessary condition and an
imprecise sufficient one". The new events fire inside exactly that bracket, so
the one case the marker exists to catch stopped looking empty. Three tests
failed and were right to. `side_completion_*` no longer sets `saw_inner`: those
events **are** the compaction, not a turn inside it.

**The wire schema's anti-drift guards.** Four of them, each doing its job:
`WireEvent`'s type Literal, the `EventBus` channel table, the derived-field set
(`delta` stopped being wire-only, because `AgentEvent` now has one too) and the
excluded-field set (`text` and `usage` were untriaged).

`text` is excluded for the reason every other exclusion is excluded — a whole
summary is unbounded on a stream a host cannot backpressure — and it is
*pullable*: it lands in the log as a `compaction` or `branch_summary` entry that
`get_entry` serves. `usage` is excluded for a different reason, and is the one
excluded field that is not unbounded: the spend is already on the entry as
`summary_usage`, so putting it on the event too would give a host two sources for
one number and no rule for which wins.

`PROTOCOL_VERSION` 1.6 → **1.7**. Additive, so MINOR.

## 5. What a reader sees now

A compaction or branch summary opens a box in the transcript and streams into it,
with the summariser's model on the subtitle while it works and what it spent when
it finishes. On a failure the subtitle says so and the box closes.

A *reloaded* summary draws the **same** box: `add_persisted_message` checks
`conversation_tree.summary_message_of` first, which recognises the
`[[Compaction summary: …]]` / `[[Branch summary: …]]` wrapper and strips it. That
function is in the core rather than in the head because the core is what writes
the wrapper — a renderer matching the string itself would be a second copy of a
format it does not own, and the two would drift the first time the wrapper
changed. Both markers now come from one table, `_SUMMARY_MARKERS`, which the two
builders and the recogniser share.

## 6. The header's `ctx` number answered the wrong question

`_aggregate_label` read `prompt_tokens(usages[-1])` — the last completion's
reported prompt size. That is a measurement of the *previous* request, and it
stops being what the next one will carry the moment a fold drops a prefix.

`_context_size` keeps the measurement when it is still true and falls back to
`estimate_context_tokens` when a summary message sits after the last measured
completion. The estimator is what `should_compact` already decides on, so the
header and the auto-trigger answer the same question from the same source. An
estimate and a measurement do not agree exactly; the alternative was a number
that was confidently wrong.

Before the first completion it is **zero, not the estimate** — nothing has been
sent yet, and the header drops the whole `ctx` part on zero so an untouched chat
shows just the model.

### The number was right and still not on screen

Fixing `ctx` did not make it visible during a compaction, because the subtitle
was a single slot with two registers in it. Nine sites wrote a gerund straight
over `sub_title` — `Thinking… (Esc to cancel)`, `Cancelling…`, `Rolling back…`,
`Compacting…`, `Summarizing branch…`, `Navigating tree…`, `Building branch…`,
`Eliding span…`, and the two press-again offers — and `_refresh_subtitle` wrote
the model and the rollup over whichever of those was standing. So the header
showed the `ctx` number only while nothing was happening to it.

That was a convention, not a compaction-only slip: `LaneStrip`'s docstring cites
"the header subtitle to say it is working" as the reason a typed turn needs no
strip entry. What it cost was largest on the ordinary turn, where `Thinking…`
hid the rollup for the whole generation.

An activity is now a **term**, not a replacement. `_set_activity(text | None)`
records it and redraws; `_refresh_subtitle` joins activity, model and rollup with
the same `·`. The term goes first because `HeaderTitle` is `content-align: center`
with `text-overflow: ellipsis`, so the tail is what a narrow terminal loses, and
the transient half is the half carrying an affordance.

Two things fell out of the shape rather than being aimed at. `_restore_subtitle`
is gone — it existed only to special-case "an offer lapsed with no session yet",
which a `None` activity and an absent session now answer between them. And a
turn settling while a second submission is still in flight no longer clears
`Thinking…`, because the clear is gated on `is_generating` the way
`_settle_submission`'s own idle report already was.

## 7. What this did not do

- **No benchmark.** `docs/BLOCKING-PERSISTENCE.md` §5 has the same admission. The
  freeze a toast-only compaction produced was never timed, and neither is the new
  path. What changed is justified by the source, not by a number.
- **`tau -p --mode json` and `tau --mode rpc` render nothing new.** The events
  are on the bus and on the wire, so both *can*; neither was taught to. The TUI
  is the only head that draws a side completion today.
- **Concurrency is assumed, not enforced.** One side completion per `purpose` at
  a time — true because a compaction runs under the turn lock and a branch
  summary runs from a modal. Two at once would share a box.
- **The count-based cut is not built.** §2, and `ROADMAP.md`.

## 8. Cross-references

* `docs/BLOCKING-PERSISTENCE.md` — the other unit about work the head could not
  see happening.
* `docs/SESSION-TREE-IMPLEMENTATION.md` — §3.1/§3.3, the branch-summary path.
* `docs/PROMPT-CACHING.md` — §7's three gates, the signal the roadmap item in §2
  would trigger on.
* `docs/REMOTE-CONTROL.md` — D3, why `WireEvent` is a projection and not the
  model itself.
