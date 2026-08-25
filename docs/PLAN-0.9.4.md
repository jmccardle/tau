# 0.9.4 — the session that got long

0.9.3 was about the first hour after `pip install`: could a newcomer install τ,
point it at a model, and get a working conversation. That is answered.

This cycle is about the hour after that. Every item below is a thing that works
on a five-message conversation and stops working on a five-hundred-message one:
an 800-message transcript takes four minutes to redraw, streaming throttles to
six tokens a second, `Esc` loses the whole turn, and the tree editor — the one
surface built specifically for a conversation too large to read — becomes
unreadable at exactly the size that makes it necessary.

The unifying complaint is that **τ stops giving feedback as the session grows.**
Not that it becomes wrong. That it becomes silent.

The measurements bore that out more literally than expected. Two of the four
items turned out to be the same defect: 67 % of a reload and 91 % of streaming
CPU are one Textual layout pass that re-arranges the entire widget tree, and it
is quadratic in the number of messages on screen. The cost of a session is paid
again on every keystroke of the next one.

Two items do not fit that theme and are here because they are small and were
asked for: §6 (swappable themes) and §7 (`--fun` never reaches a packaged user).
Both are about the screen a released τ presents, which is adjacent enough.

**Provenance.** §1–§4, §6 and §7 come from the owner's hands-on use of the TUI
on 2026-08-23, reported as observations rather than as bug reports. They are
recorded here in the words they were reported in, with my reading of the code
kept separate and marked. Where I have not verified a claim, this document says
so; do not treat the reported symptom and my diagnosis as the same kind of
statement. §7 is the exception — its cause is verified, and the section says how.

---

## 1. Loading a long conversation is slow

### What was reported

> "long conversations take a long time to load — only a few messages per second
> can be added to the TUI."

The proposal, in the owner's words:

> "We should consider unloading context beyond the last N user turns or the last
> M messages (defaults: N=4, M=50). We still need to load the entire
> conversation, it'd just be nice to get a responsive TUI interface more
> quickly. This impacts resuming a session and every navigation action."

### What is true today

`ChatDisplay.reload_messages` is the single seam. Four gestures go through it —
`action_resume_session`, `action_compact`, `action_browse_tree` and
`_elide_span_flow` — each by the same two lines: swap `self.messages`, then
`await reload_messages(self.messages)`. So one fix reaches all four, and any
regression reaches all four too.

### Measured 2026-08-23 — it is quadratic

Benchmarked against the real `ChatDisplay` in a bare app, text-only transcript:

| messages | reload | throughput | widgets |
|---|---|---|---|
| 50 | 3.34 s | 15.0 msg/s | 605 |
| 200 | 24.74 s | 8.1 msg/s | 2 480 |
| 800 | **251.29 s** | **3.2 msg/s** | 9 980 |

Four times the messages costs 7.4× then 10.2× the time. That is roughly n^1.8 —
effectively quadratic. **An 800-message session takes over four minutes to
redraw, and it redraws on every resume, `/compact`, tree navigation and elide.**
With the placeholder facts callable the real app passes, it is worse still: 200
messages went from 28.7 s to 46.2 s.

Profiled over one 150-message reload, 29.96 s of CPU:

```
screen._refresh_layout          126 calls   20.12s   67.2%
  compositor.add_widget         251 calls   16.20s   54.1%
  compositor.reflow             126 calls   10.14s   33.9%
ChatDisplay._sync_placeholder   224 calls    3.58s   12.0%
widget.mount                   1996 calls    3.15s   10.5%
Markdown._parse                 962 calls    0.22s    0.7%
```

**The cost is neither widget mounting nor markdown rendering.** It is 126 full
screen layout passes, roughly one per two awaited mounts, each arranging the
entire widget tree. The tree grows as the loop runs, so the total is Σ O(i).
Markdown parsing is 0.7 % and is not the problem.

The trigger is the `await`. `reload_messages` performs three awaited mounts per
user→answer span, and each `await` hands control back to the loop, where
Textual's screen-update timer fires and re-arranges everything built so far.

**One line of that is self-inflicted.** `ChatDisplay.mount` is overridden to call
`_sync_placeholder`, which runs two full-subtree DOM queries per mount. That is
12 % of the reload, it has nothing to do with Textual, and it is fixable on its
own.

### Does the proposal work?

**Yes, and by more than linearly.** The cost is entirely in rendering — the
message list itself is a plain slice of `session.context` and does not appear in
the profile at all. Because the cost is quadratic in the *mounted* widget count,
capping at M = 50 does not save 94 % of an 800-message reload. It saves about
99 %: 251 s becomes 3.3 s.

Two caveats the design has to weigh:

* **Deferring the mount defers the bill, it does not cancel it.** Whatever
  gesture mounts the older messages later re-enters the same quadratic path.
  Mounting the remaining 750 in a background pass still costs ~250 s of
  event-loop CPU, and the TUI is unresponsive for all of it.
* **The same quadratic is what makes streaming slow** (§2a). A cap on mounted
  widgets is one lever for both symptoms. That is an argument for treating the
  cap as a standing bound on what is on screen, not as a one-time load
  optimization.

### The shape of the proposal

Two numbers, whichever is reached first: the last **N = 4** user turns, or the
last **M = 50** messages. Everything older is loaded into `self.messages` but
not mounted as a widget.

Two things the design has to answer, neither settled:

* **What does the reader see where the older messages are?** A count, the way
  `TreeDetailPane` writes `⋯ N earlier`, is the established idiom in this
  codebase and states the truth. A blank gap does not.
* **How does the reader get them back?** Scrolling up is the obvious gesture and
  the expensive one, because it re-enters the same mounting cost that the
  deferral avoided. A key that mounts everything is cheaper to build and easier
  to reason about.

Fail Early note: the whole conversation is still loaded. This is a *rendering*
bound, not a truncation of the data. Nothing may be dropped from
`self.messages`, from the session log, or from what the model is sent — if the
deferral ever changes model input it has become a silent context bug, which is
the failure mode `docs/TREE-BROWSER-AS-EDITOR.md`'s tree-as-truth invariant
exists to forbid.

### Built — 2026-08-24

Three changes, in the order they were measured.

**1. `_sync_placeholder` reads the direct children.** It ran two full-subtree
`query()` calls on every mount. Timed in isolation against the tree a
200-message reload leaves behind — 223 calls, the number a 150-message reload
makes: **3.403 s → 0.00013 s**. The predicate is unchanged in meaning: a nested
`MessageBox` is a step inside an `ExchangeBox`, and that exchange is a direct
child, so the shallow scan answers the same question and stops at the first box
instead of collecting every one.

**2. The reload mounts inside `App.batch_update`.** Every awaited mount handed
control back to the event loop, where Textual's screen timer re-arranged the
whole tree. Counted on a 200-message reload: **78 layout passes without the
placeholder, 104 with it → 5**. Wall clock on that reload, placeholder on:
**23.0 s → 11.8 s**, and the reader sees a finished transcript rather than one
being assembled a message at a time.

**3. The render cap.** `render_cap_start` walks backwards and stops at whichever
bound comes first, N = 4 user turns or M = 50 messages, and always lands on a
`user` message so no span is cut in half. Two cases give the bound rather than
the transcript: a single span longer than M mounts whole, and a transcript with
no user message mounts whole — rendering nothing is not a smaller version of
rendering something.

| transcript | reload before | reload after | mounted widgets | next turn's streaming |
|---|---|---|---|---|
| 50 msgs | 1.7 s | **0.28 s** | 606 → 82 | 13.0 → **22.3 tok/s** |
| 200 msgs | 21.0 s | **0.28 s** | 2 481 → 82 | 3.6 → **22.4 tok/s** |
| 800 msgs | 251 s | **0.24 s** | 9 980 → 82 | — → **22.4 tok/s** |

The mounted widget count is now flat in the transcript length, which is why the
reload time and the streaming rate both stop depending on it.

**What the reader sees, and how they get it back.** A `⋯ N earlier · click to
show them` row stands where the elided messages would be — `TreeDetailPane`'s
idiom, and its stylesheet treatment. The row is clickable and there is a
`Show earlier messages` palette entry; no new footer key, which is already
carrying eight. The count excludes `system` messages, which never render, so it
never claims more is hidden than a reader could get back. The action announces
itself (`Mounting 792 earlier messages…`) because it deliberately re-enters the
quadratic, and says `The whole conversation is already on screen.` rather than
doing nothing visible.

**Fail Early.** The cap bounds the widget tree and nothing else. The list handed
to `reload_messages` is not copied, sliced or mutated — the display keeps the
caller's own object, which is also what lets `show_all_messages` render the rest
with no second source of truth to fall out of step. There is a test for exactly
that.

### What was NOT built, and what it would cost

**The `scroll_end(animate=False)` per delta is not a contributor.** The
measurement above the fold called it out as making streaming worse. It does not:
removing it and standing a scroll anchor in its place gives **108 layout passes
either way**, and 133 vs 151 ms/delta at a 100-message prefill — inside the
noise. It posts an `InvokeLater` per delta, which is cheap next to the layout
pass that was going to happen regardless. Switching to `Widget.anchor()` would
still be worth doing for a different reason — today a reader who scrolls up
mid-turn is yanked back to the bottom on the next delta — but that is a
scroll-behaviour change nobody asked for, so it is recorded here and not done.

**`display: none` is not a substitute for not mounting.** Worth checking,
because it would have made a standing bound reversible and nearly free. Hiding
all but the last 8 top-level boxes of a 200-message transcript drops the
compositor's placed-widget count from 2 483 to 103 and buys **272 → 208
ms/delta**. The cost is in the arrange walk over the *mounted* tree, not in
placement. A standing bound has to remove widgets.

**The cap applies at reload, so a long LIVE session still drifts.** Measured on
a session that streams turn after turn without ever reloading — 20 deltas per
turn, short answers:

| turns | widgets | streaming |
|---|---|---|
| 5 | 50 | 22.5 tok/s |
| 20 | 200 | 19.0 tok/s |
| 40 | 400 | 14.6 tok/s |
| 60 | 600 | 11.8 tok/s |

Gentler than the reload case (a short answer mounts ~10 widgets; a reloaded
markdown answer mounts ~12 per *message*), but the same curve. Every reload
gesture — resume, `/compact`, tree navigation, elide — resets it to 82.

Closing that needs a standing bound, and the obstacle is not the removal but the
count: the display would have to know how many *messages* each top-level widget
stands for to keep `⋯ N earlier` honest, and on the live path only `Parley` knows
that. The shape that works is to stamp each top-level content widget with the
index of the first message it renders, then drop the leading widgets whose index
is below `render_cap_start`. That touches the live streaming path, so it is a
separate decision rather than something to fold in here.

---

## 2. Nothing moves while the model does

### What was reported

> "general TUI responsiveness: just a token counter going up could help make it
> feel more alive while the model is responding. Thinking and response text
> seems to accumulate, but doesn't display on the TUI."

### Two separate problems

Investigated and measured 2026-08-23. The one reported symptom turned out to be
**two unrelated defects**, and thinking text and response text fail for
different reasons.

**2a. Reasoning text never renders. A real bug, and near-deterministic.**

`MessageBox.ensure_reasoning` constructs the `ReasoningRegion`, assigns it to
`self._reasoning`, and *then* mounts it into a slot that `compose()` creates. If
`compose()` has not run yet, the mount raises `AttributeError: 'MessageBox'
object has no attribute '_reasoning_slot'` — but the assignment on the previous
line already happened. So every later call takes the `is None` false branch and
hands back a widget **that was never mounted**. Deltas accumulate into it
forever and nothing paints.

The equivalent text path has the guard reasoning lacks: `append_content_delta`
checks `hasattr(self, "_md_widget")` and `on_mount` catches the buffer up.

This is not a rare race. `asyncio.Queue.get()` on a non-empty queue returns
without yielding, so the agent loop consumes stream events back-to-back with no
event-loop tick between them; and `_on_turn_start` does both its awaits *before*
mounting the step box. On a reasoning model the next event is always a reasoning
delta.

Measured: a burst of 8 reasoning deltas with no tick, then 20 paced ones →
**0 of 28 reasoning tokens visible**, region holds 135 buffered characters,
`rendered=False`. Insert one `pilot.pause()` before the first delta → 0 errors,
28/28 visible.

Two things this explains:

* **The existing test suite cannot see it.** `test_chat_rendering.py` awaits
  `pilot.pause()` after *every* event, on the written assumption that "the real
  backend never produces a synchronous burst". That assumption is wrong, and it
  is the reason a deterministic bug has no failing test.
* **The error is misattributed.** `EventBus.emit` catches it and routes it
  through the extension error surface, so the user sees
  `extension error in notify handler (message_update): …` once per turn. It is
  not an extension.

`Ctrl+R` does not gate this. New regions are constructed expanded and no
persistent hide state is consulted on the streaming path.

**Built — 2026-08-24.** Reproduced first, at the measurements above: a burst of
8 deltas then 20 paced ones gave 1 `AttributeError`, 99 buffered characters,
`is_mounted=False`, 0 visible. After the fix: 0 errors, 102 buffered, 101
rendered (the parser eats the trailing space).

Three changes, and the second is required by the first:

1. `MessageBox` buffers a child created before `compose()` ran and `on_mount`
   mounts it into its slot. `ensure_reasoning` and `add_tool_call` both route
   through the new `_mount_lazy`, so neither touches a slot that may not exist.
2. `ToolBox` buffers a result body written before it mounts.
   `Markdown.update()` on an unmounted widget keeps the title and the display
   flag and **silently drops the text** (verified: 0 blocks after the box
   mounts). A call and its result can arrive in the same burst, so buffering the
   box in (1) without this would have traded a crash for silent data loss.
3. `test_chat_rendering.py` gained `_send_burst` — the *unpaced* cadence — and
   six tests on it. Its `_send` docstring no longer claims the backend never
   produces a burst; that claim is why this shipped untested.

Two things found while building it:

* **`add_tool_call` had the same bug**, not just `ensure_reasoning`. The plan
  above named only reasoning. `_on_tool_call`'s two awaits (`_flush`,
  `_collapse_active_reasoning`) both return without reaching an `await` when
  there is no open stream, so a turn whose first event is a tool call hits
  `AttributeError: … no attribute '_tools_slot'`. Five of the six new tests fail
  at `7ed04c3`; the sixth (text) passes there and is a regression guard.
* **`_pending_children` is Textual's own attribute.** Naming the buffer that
  shadowed `Widget._pending_children`, which `mount_composed_widgets` iterates —
  the first run died on `'tuple' object has no attribute 'id'`. It is
  `_deferred_children`.

One deliberate non-change: when a whole turn arrives in one burst, the answer
folds the reasoning away *before* the region mounts, so the region mounts
already-collapsed and D1's deferred parse applies — its block tree is empty
while it is shut, and expanding it renders the buffer. That is the same end
state the paced path reaches by a different route, and the test asserts the
expand rather than the shut region's blocks.

**2b. Response text does arrive — but the render rate collapses with transcript
size.** This could not be reproduced as "never displays". At 40–50 tok/s on a
small transcript the screen tracks the stream with no lag. The problem is that
per-token render cost is O(the whole transcript):

| transcript | widgets | one layout pass | 100 deltas @50 tok/s (ideal 2.0 s) |
|---|---|---|---|
| empty | 9 | 0.4 ms | 2.25 s |
| 300 msgs | 3 159 | 98.9 ms | 6.76 s |
| 600 msgs | 6 309 | **191.8 ms** | **15.38 s (7.7×)** |

At 600 messages τ's maximum render throughput is **about 6 tokens per second**. A
model at 40 tok/s runs seven times ahead of it. The text arrives in lurches many
seconds behind generation and catches up only at the end, which is what
"accumulates but doesn't display" looks like from the outside.

Attribution, profiling 150 deltas on a 300-message transcript:

```
screen._refresh_layout   80 calls   20.51s   91.4% of CPU
Markdown.append         150 calls    0.10s    0.5%
markdown_it.parse       150 calls    0.05s    0.2%
```

Same root cause as §1. Markdown is 0.7 %; the streaming redesign that replaced
the old 30 Hz `Markdown.update()` gate did its job. What remains is Textual
layout, made worse by `scroll_end(animate=False)` on **every** delta.

**Built with §1 — 2026-08-24**, and that last clause is wrong: `scroll_end` was
measured and is not a contributor. See §1's *What was NOT built* for the number.
The fix is §1's render cap, which is the same lever: streaming after a reload now
runs at **22.4 tok/s regardless of transcript length**, against 3.6 tok/s at 200
messages. §1 also records the case the cap does not reach — a long live session
that never reloads — with its measurements.

**2c. There is no liveness indicator at all.** Confirmed by inspection: **nothing
on screen changes between `lane_start` and `lane_end` except the streamed text
itself.** The subtitle's only mid-turn write is "Cancelling…" on `Esc`. The
footer refreshes on `is_generating` and then stops. `LaneStrip` — the widget
architecturally shaped for "something is live" — returns early for the user's own
typed turn by design.

### What a token counter can honestly say

The question the design hung on is now answered: **there is no measured token
count during a completion.** `TextDeltaEvent.partial` carries an
`AssistantMessage` whose `usage` is hardcoded to all zeros. The provider does
capture `usage` when a chunk carries it, but only reads it when building the
terminal `DoneEvent`, and servers send that block only on the final chunk.

So a mid-completion counter is an estimate and must be labelled one, per the rule
already enforced in `SessionTreeModal._marks_summary`.

Three things *are* honestly measurable and were not obvious:

1. **Elapsed turn time.** The app already records the lane start.
2. **Chunks received.** A true count of stream events. For most
   OpenAI-compatible servers one chunk is one token, but that is not guaranteed —
   so it must be called "chunks", not "tokens".
3. **Real per-completion totals, mid-turn.** In a tool-bearing turn `message_end`
   fires once per completion, so after the first tool call `TurnStream.usage_totals`
   holds genuinely measured `output_tokens`. A counter can show a measured figure
   that steps at each completion boundary, plus a labelled estimate for the
   completion in flight.

Option 3 is the interesting one. It is the only design that shows a real number
during a long turn.

### Not in scope

A progress bar, a spinner, or an elapsed-time readout are all cheaper than a
counter and none of them was asked for. Build the counter.

### Built — 2026-08-24

Option 3, on the **exchange title**, all three measurable things at once:

```
Working… · 0:00                                just opened
Working… · ~7 chunks · 0:03                    first completion, reasoning
Working… · 812 out · 0:19                      boundary — a tool is running
Working… · 812 out · ~143 chunks · 0:47        second completion streaming
Working… · 2.4k out · ~1 chunk · 2:08
```

`N out` is measured and nothing else. `~N chunks` is the completion in flight,
labelled twice — the `~` and the word *chunks*, because one chunk is not
guaranteed to be one token. The two are never added together, for the same
reason `set_summary` never adds `ctx` to `out`.

Four changes:

1. **`TurnStream` publishes the completion boundary.** `_harvest_message_end`
   already summed the real per-completion usage and kept it to itself until
   `lane_end`; it now returns a `{"kind": "completion_end", "output", "context"}`
   render event carrying the same running totals. That is the whole measured
   half of the counter — a figure that steps at every tool call.
2. **`ExchangeBox.set_live`**, the running counterpart of `set_summary`, with
   the same part-omission rule: a part with nothing to say is left out rather
   than printed as zero.
3. **`_LaneRender` carries the counter state** — `started`, `measured_output`,
   `chunks` — so two concurrent lanes count separately.
4. **A 4 Hz timer in `ChatDisplay`**, created paused and running only while a
   lane is open.

**Why a timer rather than a repaint per delta.** The silence that matters most
is the one with no deltas in it: a thirty-second tool call, or a reasoning model
thinking before it answers. A counter fed only by stream events goes dead
exactly then. Measured on a 50-message transcript: 200 ticks with nothing else
happening cost 9 layout passes, and interleaving 25 ticks into 100 deltas moved
the per-delta cost from 41.43 ms to 41.34 ms — inside the noise.

**Why the exchange title and not the header subtitle.** Two reasons, and the
first is not a preference. The subtitle is one line, and two concurrent lanes (a
`fork`, a bus submission arriving mid-answer) have two different answers to
"what is happening" — a single readout could only report one of them, and
`LaneStrip` already exists to say *that* other lanes are live. Second, the
subtitle is written by `Cancelling…`, `Rolling back…`, `Compacting…` and
`_refresh_subtitle`; a 4 Hz timer writing over those would need a lock on each,
which is machinery in service of a worse readout.

**What this does not cover:** a reader who has scrolled up sees no counter,
because the exchange it is on is off screen. The `⋯ N earlier` row from §1 is
the same shape of problem and the same answer — scroll back down. A pinned
readout is a second surface and was not built.

---

## 3. `Esc` loses the turn

### What was reported

> "if I hit 'esc' to stop the model from responding, it looks like nothing
> persists to disk, due to a JSON parse traceback. For file or JMFTS backed
> conversations, every complete message or tool result should be persisted, and
> the partial one that gets interrupted should be recovered to the extent
> possible."

### Why this is the most serious item in the cycle

This is a Fail Early violation in the worst direction. An abort is a *normal*
gesture — it is bound to `Esc` with `priority=True` and the user is expected to
press it. A normal gesture that discards completed work, and reports the loss as
a traceback rather than as a sentence, hides a problem instead of surfacing one.

The second sentence of the report is the requirement, and it is stronger than
anything the code currently states anywhere:

> **every complete message or tool result should be persisted, and the partial
> one that gets interrupted should be recovered to the extent possible.**

Note what it does *not* say. It does not say the partial message must be
recovered — "to the extent possible" concedes that a half-streamed assistant
message may be unrecoverable. It says the *complete* ones must survive, which is
a much easier bar and is the one currently being missed.

### Root cause — diagnosed and reproduced 2026-08-23

**Escape itself is fine.** It is a clean cooperative abort: `action_cancel_generation`
calls `backend.abort()`, which reaches `AgentSession.abort`, which trips an abort
signal that the provider polls once per SSE line and the loop checks between
turns and between sequential tool calls. Nothing about that mechanism loses data.

The loss needs one more condition: **the abort has to land while a tool call's
`arguments` are mid-stream.** Then four things happen in order.

1. The abort breaks out of the SSE reader at a line boundary, sets
   `stop_reason = "aborted"`, and falls through to the ordinary finalizer.
2. The finalizer joins the accumulated `arguments` fragments and parses them
   **strictly**. There is no branch on `stop_reason`, so a buffer that is known
   to be truncated is handed to the strict parser, which raises
   `JSONDecodeError`. `repair_json` cannot help — it fixes control characters
   and bad escapes, not unterminated strings.
3. That becomes an `ErrorEvent`, which `AgentLoop._stream_response` turns into a
   raised `RuntimeError`.
4. **`AgentSession` persists the whole turn as one batch, after `await
   loop.run(...)` returns.** The loop accumulates messages into a local list. If
   it raises, the local dies with the frame and every append site is skipped —
   the user's prompt, the assistant message, and every completed tool result.

So the traceback is not a side effect of the loss. **It is the cause.** The
control case proves it: abort mid-*text*, with no tool call in flight, produces a
clean `DoneEvent(stop_reason="aborted")`, `loop.run` returns normally, and the
turn persists correctly.

### Three findings that change the shape of the fix

**The stores are not the problem.** Both the file store and the JMFTS store are
per-append durable — one JSON line, or one `create_document` POST, per entry.
Neither batches anything. The batching is entirely in `AgentSession._run_one_turn`,
which is why the file-backed and JMFTS-backed behaviour is identical, and why
fixing this is one change rather than two.

**A test currently asserts the data loss as correct.** `test_error_handling.py:478-493`
asserts `session.messages == []` after a provider error, commented "The turn
never completed, so nothing was persisted either." It was written about a 503 and
lands in the same branch. Fixing the bug means changing that test, and the
change is the interesting part of the review — a 503 mid-turn should probably
keep the completed messages too.

**There is a second, latent defect on the same path.** When the abort lands
during *tool execution* rather than during the stream, the sequential executor
just breaks and synthesizes nothing. If the turn then persists, it records an
assistant message with `tool_call_id`s that no result ever answers. pi does not
do this — it returns an "Operation aborted" error result for every outstanding
call. Worth fixing in the same pass, because a fix that starts persisting
aborted turns will start persisting this malformation too.

### What pi does, on three counts

pi has none of this, for three independent reasons, and each is a candidate fix:

1. **Its finalizer is lenient.** It parses the final buffer with the same
   forgiving parser it uses for streaming display, so a truncated buffer cannot
   throw. τ's strict finalize is a deliberate Fail Early divergence and is right
   for a *complete* stream — `docs/TOOL-CALL-PARSING-BUG.md` is why it exists.
   The gap is that the aborted path reuses it without consulting `stop_reason`.
2. **It treats a provider error as a value, not a raise.** It sets a stop reason
   and returns the partial message, so the turn ends normally and persists.
3. **It answers every outstanding `tool_call_id` on abort.**

Note that fix 1 must not become "parse leniently everywhere". The strictness is
load-bearing on a complete stream — a silently repaired tool call is the
corruption bug this repo already fixed once. The condition to branch on is
`stop_reason == "aborted"`, which is a fact the finalizer already has and does
not read.

### Acceptance

Two tests, neither of which exists today:

1. Abort a stream mid-tool-call-`arguments` and assert the preceding **completed**
   messages are on disk — including the user's prompt.
2. Abort during tool execution and assert every `tool_call_id` on the persisted
   assistant message has a matching result.

The existing abort tests cover the text-only case, which is exactly the case
that already works.

### Built — 2026-08-24

All three of pi's answers, because each closes a different half and any one alone
leaves a defect.

**1. The finalizer reads `stop_reason`** (`tau-llm/.../providers/openai.py`,
`_build_final_message`). On `stop_reason == "aborted"` a tool call that cannot be
finished is **dropped** — not repaired, not defaulted to `{}`. Three cases reach
it: a truncated `arguments` buffer, a buffer that decodes to something that is
not an object, and a call whose `function.name` had not arrived. Each would
otherwise raise, and the nameless-call guard would have blamed a gateway for the
user's `Esc`.

Dropped rather than repaired is the load-bearing word. A half-streamed
`{"path": "/etc/pas` turned into *something* is a tool call the model never
issued, run against arguments it never finished choosing, after the user asked
for the turn to stop. Dropping loses nothing, because the call was never made —
and `usage.extra["dropped_partial_tool_calls"]` records how many, present only
when it is non-zero so "nothing was lost" and "not reported here" stay
distinguishable.

The branch is narrow on purpose and the test file pins both directions: the same
unfinishable buffer must **still error** on a complete stream. That pairing is
also the proof the branch does the work, since the only difference between the
two tests is the abort.

**2. A failed run carries what it finished** (`agent_loop.py`,
`COMPLETED_MESSAGES_ATTR` / `completed_messages`). `run` and `run_continue`
already close their `agent_start` bracket from an `except` that re-raises; they
now also attach `final_messages` to the exception on the way past. An attribute
rather than a wrapper exception, deliberately: the loop raises several types
(`RuntimeError` from a provider `ErrorEvent`, `ValueError` from a malformed tool
call, `CancelledError` from a hard cancel) and a caller catching one of them
specifically has to keep working.

`AgentSession._run_one_turn` catches, persists, and re-raises unchanged. The
persist block moved into `_persist_turn_inputs` so the success path and the
failure path write the same things in the same order — it was inline, and
therefore only on the success path, which is why an aborted turn lost the user's
own prompt as well as the assistant's reply. `persist=False` is honoured on both
paths, so a failed turn is not a way to get a silent submission recorded.

**3. Every outstanding `tool_call_id` is answered** (`agent_loop.py`,
`_aborted_batch`). A guard at the top of `_execute_tool_calls` covers the abort
that landed during streaming, and it sits there rather than in the two executors
because **the parallel path has no abort check of its own** — it gathers every
prepared call at once, so without a guard above it an abort mid-stream would
still have run the whole batch. That is worse than the sequential defect the plan
named, and was not in the diagnosis. Mid-batch aborts keep the results that
already completed and answer only the rest, so "answer everything outstanding"
cannot overwrite work the user did get. Each synthesized result emits a
`tool_execution_start`/`end` pair, because a front-end folds an end into the box
its start created and an end with no start is silently dropped.

**The test §3 called out is inverted, not deleted.**
`test_error_handling.py::test_provider_error_propagates_uncaught_through_session_prompt`
asserted `session.messages == []` under a comment that read as a description
("The turn never completed, so nothing was persisted either") and was pinning the
defect. It now asserts the user message survives. The review question §3 raised —
should a 503 keep the completed messages too? — is answered **yes**, and for the
same reason: "the turn failed" is not a reason to discard what the user typed.

**Coverage:** 25 new tests, 14 in `tau-llm/tests/test_abort_finalize.py` (which
sweeps every SSE line position, because the bug's window was narrow enough that a
single-position test would pass with the defect still present) and 11 in
`tau-agent-core/tests/test_abort_persistence.py`. Suite 4752 passed / 140
skipped, no existing test changed behaviour except the one above.

#### Verified live — 2026-08-24

Against llama.cpp at `192.168.1.100:8080`, `Qwen3.8-27B-Q4_0`. This matters
because the unit tests drive a mock SSE body: the real server emitted **12
`arguments` fragments** for one two-key tool call, which is the aggressive
fragmentation the bug needed and the thing a single-chunk cloud response masks.

| | what it exercises | result |
|---|---|---|
| A | control: a complete tool-calling turn | tool call intact, `stop_reason=toolUse` |
| B | provider, abort mid-`arguments` | no `ErrorEvent`, `stop_reason=aborted`, 1 dropped |
| C | session, abort mid-`arguments` | no raise, prompt on disk |
| D | session, abort after the call completed, sequential | call answered `Operation aborted`, no tool ran |
| E | the same, parallel executor | same |

The buffer at B's abort was `{"path": "/etc` — truncated inside a value, and
dropped rather than repaired.

Two things this found that the unit tests could not:

* **C's tool-call-id check passes vacuously**, and so does the equivalent unit
  test's reasoning if read carelessly. When the abort lands mid-`arguments` the
  call is dropped, so there is no id left to answer and `_aborted_batch` is never
  reached. D and E exist because of that: they abort *after* the stream finishes,
  which is the only way a surviving call reaches the executor guard.
* **`parse_streaming_json` shows both argument keys before the raw buffer has its
  closing brace.** The first attempt at D watched `message_update` for a complete
  argument dict and still got a dropped call, because the display-side partial
  parse is optimistic and the finalizer reads the raw string. Anything reasoning
  about "is this call complete yet" from a streamed partial is reasoning about the
  wrong object.

**Not verified: `pytest -m llama` itself.** It errors at *setup*, before any of
its six tests, for a reason unrelated to this work: the probe deliberately posts
`reasoning_effort: "banana"` to see what an unknown value does, and this model's
chat template raises a Jinja exception, so llama.cpp answers HTTP 500 and the
probe treats that as fatal rather than as the answer. None of its six tests is
about aborts, so it is not the instrument for this change — but the probe's
handling of a 500 on that request is its own small defect, unscheduled.

---

## 4. The tree editor is unreadable once it is large

### What was reported

> "I consider it a major work in progress. I probably need to make the message
> preview collapsible to see more of the tree, and we've got something subtly
> wrong about how elements are decided for indentation. Perhaps the solution is
> more about being able to collapse an agent's turn instead of filling the tree
> view with every tool/action they selected."

And, explicitly:

> "This isn't actionable until we do discussion, hands-on feedback."

### Status: UNBLOCKED — hands-on feedback received 2026-08-24

The owner worked the browser against a real forked session and reported four
things. They are recorded verbatim in *The four items* below, with what each one
turned out to be. Two of the candidate diagnoses below (4d, 4e) are confirmed
and one is answered in a way I did not predict; 4a, 4b and 4c were not raised.

The standing deferral of styling decisions is unchanged.

### The four items

> "the tree editor widget is 1 to 3 cells too wide, probably forgot to calculate
> the vertical scrollbar's width, so there's a dud of a horizontal scrollbar."

**Confirmed, and the guess is right. One line.** `_relabel` sizes every label
against `tree.content_size.width`. In textual 8.2.7 `content_size` is
`region.shrink(styles.gutter)` — border and padding only. The scrollbar is
subtracted by `scrollable_content_region`, which `_relabel` does not use. So on
any tree tall enough to scroll, every label is sized two cells too wide, the row
overflows, and the tree grows a horizontal scrollbar that then eats a row.

Measured on a 40-turn session at 100×30:

| | before | after |
|---|---|---|
| width `_relabel` sized against | 100 | 98 |
| `virtual_size.width` | 100 | 98 |
| `max_scroll_x` | 2 | 0 |
| `show_horizontal_scrollbar` | True | **False** |
| `scrollbar_gutter.bottom` | 1 | 0 |

"1 to 3 cells" rather than exactly 2 because a row at widget depth `d` charges
`(d + 1) * guide_depth` and the elision can land inside a wide character.

> "there's rarely anything to fold and the placement of nodes still routinely
> confuses me. When we navigate to a user message, the intent is to *give a
> different message* that would be a fork of that message's parent: we're
> navigating to the bottom side of a node instead of the top side of it. Pi
> populates the edit box with the selected node's text. (Navigation to the bottom
> side of a node is correct for agent/tool messages… user messages shouldn't be
> submitted twice in a row, so the bottom side of a user message is not that
> useful of a position to navigate to.)"

**A semantics bug, not a rendering one.** `action_commit` answers
`TreeIntent("navigate", (cursor_id,))` for every node, and
`action_browse_tree` hands that id to `navigate_tree`, which moves the cursor to
it — so the next thing typed becomes that node's CHILD. For an assistant or tool
row that is right: continue from below it. For a user message it produces the
one shape a conversation cannot have, two user turns in a row. What the reader
means by pointing at a user message is "ask this differently", which is a fork
from that message's **parent** with the old text in hand to edit.

> "user turns are the actual marker we're not using. We've flattened child nodes
> into visual siblings, but I think it went too far. A user message could show
> those tool calls and the assistant message as its children, and start
> collapsed. Those assistant messages don't need a triangle to expand/collapse,
> unless they were used as a branch point."

**Confirms 4d and answers it.** 4d says the rows that most need folding cannot
be folded, because §2's rule gives a non-forking run no widget parent, and calls
that a direct collision with §2. The answer is that there is a marker §2 did not
use: **a user message is a turn boundary.** A user message becomes the widget
parent of everything down to the next user message, so a turn folds; the next
user message is its SIBLING, so indentation still does not grow with the
conversation's length. §2's rule survives — forks still nest — and gains one
more level-opening node kind.

Note this is **not** pi parity. pi's `flattenTree`
(`tree-selector.ts:200-320`) indents on branch points only, exactly as §2 does;
its screenshots read as turn groups because the sessions shown fork at every
turn. This is a τ decision, and the owner's, so it is recorded as one.

> "the navigation actions actually aren't of any use to users in the tree -
> they're not real content."

**Confirms 4e, for `navigate` specifically.** A `navigate` entry records that the
cursor moved. It carries no message, is not a branch target worth naming, and
sits between an assistant message and the user message that forked off it —
which is the one place an extra row does the most damage to the shape the reader
is trying to read.

### What is NOT settled

pi has a **filter mode** the browser does not:
`FilterMode = "default" | "no-tools" | "user-only" | "labeled-only" | "all"`
(`tree-selector.ts:93`), with a `visibleParentMap`/`visibleChildrenMap` pair
that keeps navigation coherent while rows are hidden. Hiding `navigate` is the
first row-hiding rule τ would have, and the shape of that mapping is the same
work whether one kind is hidden or five.

**Settled 2026-08-24: `navigate` only, and no filter mode.** `model_change` and
`agent_spec` keep their rows — each records a real change to what the model is
and what it was told, which is worth seeing while browsing history even though
neither is a useful branch target. The general filter is not built: it would
decide four questions nobody asked, and the row-selection function step 2 adds
is where it would go if it is ever wanted.

### Candidate diagnoses, from before the feedback

These are my readings of the code. Each is a guess at what the reported symptom
corresponds to. None has been confirmed with the owner.

**4a. The detail pane cannot be dismissed.** `TreeDetailPane` is stacked under
the tree and takes half the body. The only thing that hides it is a terminal
shorter than `DETAIL_MIN_HEIGHT` (20 rows). There is no key. So the reader gives
up half the tree permanently, and the cost is worst when they are trying to see
shape rather than content. I read the reported "make the message preview
collapsible to see more of the tree" as naming this, rather than the one-line
row label.

**4b. Indent depth never decreases.** In `SessionTreeModal.on_mount`'s `_add`, a
fork's children each take `depth + 1` and a single-child run stays at its
parent's depth. There is no rule anywhere that brings a depth back down. So once
a path passes through a fork, every row after it on that path is permanently one
level deeper — including rows on the main line that have nothing to do with the
branch. A conversation with six forks behind it ends six levels in. This
reproduces the run-off-the-right-edge symptom that §2 of
`docs/TREE-BROWSER-AS-EDITOR.md` was written to remove, only more slowly.

**4c. A sub-agent's branch and a user's fork are the same shape.** This is
stated as a deliberate design decision in `spawn_branch`'s own docstring —
"Nothing on disk marks them as a sub-agent's… they are meant to read the same".
The consequence for the browser is that a sub-agent detour costs a permanent
indent level under 4b, and the tree cannot say which of a fork's two children is
the line the reader is on. Whether this is a browser problem or a data problem
is exactly the kind of question the discussion needs to settle.

**4d. The rows that most need folding are the ones that cannot be folded.** An
agent turn is one assistant entry followed by N tool-result entries, each with a
single child. The fork-counting rule in §2 turns precisely that shape into
siblings with no widget parent — so `←` on any of those rows finds no children
and jumps out to the enclosing fork instead. There is no node to collapse. I
read "collapse an agent's turn instead of filling the tree view with every
tool/action" as naming this, and it is a direct collision with the §2 rule
rather than a gap in it. Any turn-level grouping needs a widget parent for a
non-forking run, which is the thing §2 removed.

**4e. Structural entries are drawn at the same weight as content.**
`agent_spec`, `navigate` and `elide` entries each get a row and carry no
message. Possibly a contributor to "filling the tree view"; possibly irrelevant.
Listed for completeness.

### The build, in the order it should be done

**Step 1 — the width (item 1).** `_relabel` reads
`tree.scrollable_content_region.size.width`. The comment there already reasons
carefully about the toggle and the guide depth and is exactly right about both;
it is the scrollbar it does not mention. A test asserts
`show_horizontal_scrollbar is False` and `max_scroll_x == 0` on a tree tall
enough to have a vertical one — assert the symptom, because that is what was
reported, and it is what a later change to the label arithmetic would break.

Independent of everything below. One line and one test.

**Step 2 — a presentation tree (items 3 and 4).** Both items say the same thing
in different directions: the rows drawn are not the entries stored. Today
`on_mount._add` walks `ConversationTree.tree()` directly, so every entry is one
row and the only nesting rule is the fork.

Introduce one pure function that takes the data roots and returns the ROWS —
what to draw, under what, at what depth — and let `_add` walk that instead. Pure
so it is testable without a terminal, which is where the shape rules belong; the
existing browser tests all drive a `Pilot`.

Two rules in it:

1. **Hidden rows.** A `navigate` entry is dropped and its children attach to its
   nearest visible ancestor — **unless** it is the cursor or has more than one
   child. That exception is not a special case for tidiness: a hidden cursor
   leaves the reader with no `◀ current` row, and a hidden fork point silently
   merges two branches into one run. Both are worse than the row. Fail Early:
   the browser never draws a shape the log does not have.
2. **Turn groups.** A `message` whose role is `user` opens a widget level. Every
   following row on its path attaches to it until the next `user` message, which
   becomes its sibling at the same depth. A fork still opens a level, as §2 says
   — the two rules compose, they do not compete.

Groups mount collapsed, except the ancestors of the cursor: a browser that opens
without showing where you are has failed at the one thing it must do.

`action_collapse` and `action_expand` need no code change — both are written
against "a node with widget children" — but their docstrings claim such a node
IS a fork, which stops being true here. Reword rather than leave a comment that
lies.

**Step 3 — what Enter on a user message means (item 2).** `TreeAction` gains
`"revise"`. The modal emits it when the cursor is on a `user` message and
`"navigate"` otherwise; the id is still the node the reader named, because the
modal's job is to report what was pointed at (§5.3 / §11.1) and it is the caller
that knows the question.

`action_browse_tree` reads the action:

* `navigate` — unchanged. Target is the named node; the next turn is its child.
* `revise` — target is the named node's **parent**, and the `ChatInput` is
  prefilled with the named node's text after the browser closes.

The mode chooser still runs: "ask this differently" and "ask this differently
and summarize what the old answer found" are both real.

`_elide_span_flow`'s second pick is unaffected. It asks a different question of
the same browser and reads `sole_id`, which both actions carry — but it must say
so, because relying on that by accident is how the elide flow acquires the
branch flow's semantics in a later edit.

**Not in this step:** the detail pane is where a reader confirms what they are
about to revise, and 4a (it cannot be dismissed) was not raised. Left alone.

### Built — 2026-08-24

All three steps. `docs/TREE-EDITOR-MANUAL.md` is updated to match and re-dated.

**Step 1** is the one line. `_relabel` reads `scrollable_content_region`.
`test_the_rows_leave_room_for_the_vertical_scrollbar` asserts
`show_horizontal_scrollbar is False` at both sizes and fails at either without
the fix. The test that should have caught this —
`test_a_long_chain_still_fills_the_row_at_80_columns` — compared
`virtual_size.width` against `content_size.width`, the same wrong number, over
eight-character previews that could not overflow anything; both halves are fixed.

**Step 2** is `plan_tree_rows`, pure and module-level, with `TreeRow`,
`_row_is_hidden` and `_drawn_children`. `on_mount` is now a transcription of its
output. It is also iterative where the recursive `_add` it replaces was one frame
per entry — a linear session over ~1000 entries would have raised.

The expansion rule is worth recording because the obvious version is wrong.
"Expand the groups the cursor is inside" has to mean the cursor row's **widget**
ancestors, not its `parentId` ancestors. In a linear conversation every earlier
user message is a `parentId` ancestor of the cursor and none of them is a widget
ancestor — rule 2 makes them siblings — so keying off the data chain leaves every
turn in the session open, which is the state item 3 asks to get out of. Measured
on a 30-turn linear log: 29 of 30 groups folded off the widget chain, 0 off the
data chain.

The rendered shape, on the owner's reported structure:

```
user: read /tmp/context_test
  assistant: No such file. Want me to create one…?
    user: Yes, write your favorite number to it.          ▸ folded
    user: Actually, check again! What would you guess…    ▾
      assistant:
      toolResult: 42
      assistant: Whoops. It's there now… ◀ current
```

A side effect worth naming: `_hidden()` (§5.3's set 4) had no producer, because
nothing was ever collapsed at mount. It has one now, so `tree--zone-hidden` is
live and `_marks_summary` can report a mark inside a folded turn.

**Step 3** adds `revise` to `TreeAction` and
`ConversationTree.message_text(entry_id)` — the whole body, where
`TreeNode.preview` is the first line elided to a row; prefilling from the preview
would hand back a truncated version of what the reader typed. The modal decides
the action from the row's kind and still names the row; `action_browse_tree`
resolves the parent and writes the input. A parentless user message is refused
with a sentence rather than falling back to landing on it.

### Fine tuning — 2026-08-24

> "can we hide the arrow on Tool/Assistant messages which have no branches? They
> can be clicked and toggled, but there is nothing to expand or hide, so it's a
> bit distracting."

`Tree.render_label` draws its toggle off `allow_expand` **alone** and never asks
whether the node has children (textual 8.2.7), so every row wore one. `TreeRow`
gained `has_children`, which is a property of the PLAN rather than of the entry —
an assistant whose only child is a hidden `navigate` has a child in the log and
none on screen, and the arrow is a promise about the screen. `_relabel` follows
it: a row with no toggle gets those two cells back for its preview.

**And it turned up a real defect the turn groups had introduced.** Sizing the
labels against the CURRENT scrollbar state is chasing a moving target that
`_relabel` itself moves: shortening labels can retire the horizontal scrollbar,
which gives back a row of height, which can retire the vertical one, which
widens the labels again. Measured: three `_relabel` passes at mount, all three
seeing a vertical scrollbar that was gone once the tree settled. Worse, a fold
moves it too — a tree with a long first turn and a short last one opens at four
rows with no scrollbar, and expanding the long turn put every label two cells
over: `max_scroll_x == 2`, the reported symptom back again through a gesture
step 2 had just added.

So `_relabel` now reserves `styles.scrollbar_size_vertical` unconditionally —
the style value, not `Widget`'s property of the same name, which answers 0 when
the bar is hidden and is the very state this refuses to depend on. The cost is
two cells of preview on a tree short enough not to scroll. A stable answer that
is occasionally two cells conservative beats a tight one that oscillates, and it
means no fold handler has to refit anything.

**Two things this changed that were not asked for**, both consequences rather
than choices:

* `test_tree_modal_navigates_and_selects_interior_node` walks the cursor onto a
  user message, so its intent is now `revise`. The test is about the walk and
  the id; the action moved with the row's kind.
* A test reaching a row inside a folded turn has to open it first. That is the
  gesture a reader makes, and it is written down as such rather than worked
  around.

### Second round — 2026-08-24

Six more items after the same hands-on pass. Four are tree-editor work and stay
here; the other two (`Ctrl+C`, `Esc` `Esc`) are conversation-view keys and are
§4b below, kept in this section because they arrived in the same message.

> "Can the tree editor get some additional spans for style? The message type
> would be a helpful label to color."

**Built.** `ZoneTree.render_label` paints a fourth range: the row's type tag —
the `user:` / `toolResult:` prefix, up to and including the colon — and leaves
the preview after it neutral, so the left edge is scannable without reading it.
`_TREE_KIND_CLASS` maps eleven tags onto **five** classes (`tree--kind-user`,
`-assistant`, `-tool`, `-system`, `-structural`), because the reader is
separating sides of a conversation rather than enumerating entry kinds; a class
per kind would put eight colours on one screen and say nothing more. The four
conversation hues are the transcript's own `$tau-role-*` variables, so a role
cannot be one colour in the chat and another in the tree.

The tag is painted BEFORE the zones, so a zone paints over it: what the reader
has done to a row outranks what the row is. The cursor row is left alone, tag
included, for the reason `render_label` already left it alone.

*A collision worth recording.* `tree--zone-summary` borrowed `$tau-role-user`
when it was "the one role no other zone had borrowed" (§4.3). That is no longer
true, and the two now resolve to the same `Style` — which broke
`test_a_branch_summary_and_the_branch_it_summarizes_read_as_a_pair`, whose
oracle compared styles by value. The product is fine (a zone covers the whole
label; a tag stops at the colon), so the test helper was split into
`_row_span_styles` / `_row_tag_styles` by span RANGE. The alternative — giving
the kind classes hues of their own — would have made the tree disagree with the
transcript about what colour a user message is.

> "Elide is still a confusing tool to use. Any chance of greying out /
> preventing invalid messages from being selected for the second choice in the
> tree? … Elide actually should be removed from the modal. We have the "space to
> select" functionality — `Ctrl+e` to elide, tooltip only visible if the node
> under the cursor is a valid elide target?"

**Built, and it removed a whole screen.** `Elide a span ending here…` was a
fourth button on `TreeModeModal` that re-opened the entire tree browser with a
different caption to ask for the second node — and an illegal pair was reported
after both screens had closed, over a conversation whose shape was no longer on
screen. That is the disorientation reported.

It is `Ctrl+E` in the browser now. `TreeAction` gains `"elide"`, the only action
carrying TWO ids, which is why `TreeIntent.sole_id` raises instead of returning
`ids[0]`.

**Which node is which is decided by the tree, not by the gesture order** — the
owner's amendment, and it is what makes the pair worth marking at all. The two
ends of an elide are an ancestor and a descendant of each other; the deeper one
is always the anchor, because the shallower one is by construction on its path
and the reverse is impossible. So `ElidePlan` sorts by `_depth_of` and the
reader never has to remember which end they picked first. With no mark at all
the other end is the current leaf, which is the ordinary elide ("fold the
history behind where I am") and would otherwise cost a mark to say.

The greying is `TreeZones.ineligible`, painted **only while exactly one row is
marked**: a mark is what says "I am choosing a span", and greying half the tree
at somebody who is browsing answers a question they did not ask. It carries the
ancestry rule alone — the other illegality, a legal pair whose span is empty, is
one row and is refused by name at the keypress rather than computed for every
row, which would cost a context walk per row. `_elide_line` is cached against
the MARK, not the cursor, so an arrow key does not recompute it.

The offer line is `_elide_offer`, appended to the marks readout: `ctrl+E: elide
4 messages`, and the empty string when there is nothing to offer. Every check
`TauBackend.elide_span` performs is performed there first, on the same rules
against the same tree, so the line can only ever offer an elide the backend will
accept. The backend still validates — the modal checked a tree built when it
opened and the session is live underneath it — and the old refusal tests now
drive it from a hand-built intent, which is that stale case rather than a
synthetic one.

> "We never made the message preview collapsable: double-click on its border or
> ctrl+m to make it 1 cell tall and hide the content?"

**Built — this is 4a**, which was listed as a candidate diagnosis and never
raised until now. `Ctrl+D` folds `TreeDetailPane` away and one row stands in its
place: `▸ detail pane hidden — ctrl+D, or click here, to show it`. Double-click
on the pane (which is reachable only at its border and padding — a click inside
lands on a `MessageBox`) folds it; one click on the marker restores it, because
a fold you have to double-click your way out of is a trap.

`Ctrl+D` and **not the `Ctrl+M` that was asked for**: a terminal sends one byte
(0x0D) for both `Enter` and `Ctrl+M`, and textual says so — `KEY_ALIASES` maps
`enter` to `["ctrl+m"]` (keys.py). A `Ctrl+M` binding on this screen would fire
on every `Enter`, which is the commit key. There is no way to tell the two
apart, so the gesture takes the next free key rather than one that works some of
the time.

The fold and the height rule are kept apart in `_apply_detail_pane`: the height
rule is the layout's and gets no marker (below `DETAIL_MIN_HEIGHT` the last rows
are better spent on tree), the fold is the reader's and does. A terminal growing
back past the minimum does not un-fold a pane the reader folded.

**Two consequences that were not asked for.** The help line had to be shortened
— `^E` and `^D` pushed it to 94 columns and it wrapped to two rows at 100, which
takes a row from the tree to tell it about a key; it is `Tab/^D: pane` and two
spaces between items now, 75 columns, one row at 80. And `Binding("ctrl+c", …)`
had to keep `show=False`: making it `priority` stopped textual's own system
binding shadowing it, and `^c Quit` appeared in the Footer for the first time,
costing ten columns to advertise the most widely known key in a terminal. Three
snapshot scenes caught that, which is what they are for.

### §4b — two conversation-view keys — 2026-08-24

> "let's capture "Ctrl+C" and do three additional things: if generating: stop
> generating / if not generating: clear input box / if not generating and input
> box was empty: Set the top status bar for 3 seconds to say "press ctrl+C again
> to exit" / if not generating and input box is still empty and status bar was
> set less than three seconds ago: ordinary Textual exit behavior"
>
> "Same behavior for "esc, esc" in the conversation view: set the top status bar
> to say "press Esc again to view the tree", on second keypress open it"

**Both built**, over one shared mechanism: `_offer_again(name, message)` returns
whether that key's offer was already standing and, on a first press, writes the
message into the header and starts a three-second timer.

The one decision inside it: **a press of either key withdraws the other key's
offer.** There is one status bar, and an offer nobody can see any longer must
not still be answerable — a reader who presses `Esc` and then `Ctrl+C` gets
`Ctrl+C`'s warning, not an exit.

`ctrl+c` was bound straight to `quit`, so one mistimed press ended a session with
a draft in the input and no warning. It is `action_interrupt` now, four steps
from "stop that" to "quit". `escape` idle did *nothing at all* — the binding is
`priority=True`, so the key was consumed and the action no-op'd — and now offers
the browser. Two presses rather than one because `Esc` is also the key people hit
to mean "never mind", and opening a full-screen modal on that is worse than
doing nothing was.

`check_action` gates `escape` and `interrupt` on the modal stack, which mattered
less when the idle action was a no-op: without it, `Esc` over a dialog would arm
a second modal instead of closing the first.

### Elide: the count was wrong and the wording was backwards — 2026-08-24

> "I don't know if elide is doing something wrong, or just not doing what I'd
> expect. My mental model is, given messages [1, 2, 3, 4, 5, 6], elide(2,4) would
> yield [1,5,6]. What does it actually do? Does it yield [2,3,4]?"

**It yields `[2,3,4]`.** Verified by running it: the two ends bracket what is
KEPT. An elide is the summary-less compaction anchor and a compaction keeps a
tail, so `_active_path_entries` emits the anchor and then its ancestors from
`firstKeptId` onward — the kept region is always ONE contiguous run ending at the
anchor. **Cutting a span out of the middle is not a shape the operation can
express**, and no amount of UI work makes it one.

Two things were wrong on τ's side, both mine, both in the offer line:

**The count was measured at the wrong node.** `_elide_plan` computed the drop as
`context_entries(anchor) - kept`, which is the set `TauBackend.elide_span`'s own
no-op check uses. That set cannot see the entries the *cursor move* abandons. On
the reported shape — `[1..6]`, cursor at 6, pairing 2 with 4 — it said `elide 1
message` while three left the context (`1` to the fold, `5` and `6` to the move).

`ElidePlan` carries both numbers now, because they answer two different
questions and neither substitutes for the other:

* `folded` — measured at the anchor. Gates the refusal, so the modal keeps
  refusing exactly what the backend refuses.
* `dropped` — measured at the CURRENT cursor. What the reader loses, so it is
  what the line reports.

Measuring both at the cursor would have been wrong the other way: it would offer
an elide whose fold hides nothing, which the backend then rejects.

**The wording set the wrong model.** `elide N messages` reads as "remove the N
between these two rows", which is the opposite of what happens. It is now `keep
this span, drop the other N entries`, plus `, and move back to it` when the
anchor is not the tip — the frame has to be at the gesture, because the manual is
not open at the moment somebody presses the key. `docs/TREE-EDITOR-MANUAL.md` §9
opens with the `[1..6]` example.

**Not built, and not decided: cutting out the middle.** `[1,5,6]` would need a
fold that keeps two disjoint runs, which `_active_path_entries` cannot emit and
`firstKeptId` cannot name. It is a core change (a span with two boundaries, or a
list of them), not a browser change, and nobody has asked for it as such.

### Closed — the reasoning block was the config, not τ — 2026-08-24

> "still can't see reasoning content. I *think* I saw a flash of it generating,
> but unlike tools, it doesn't leave a message block that can be expanded" …
> "model's 'reasoning' output appears visible before a tool call, but if the
> model calls no tools, then there is no block at all."

**Not reproduced, and the renderer is measured innocent.** Driving the REAL
`TurnStream` and `ChatDisplay` end to end with the `AgentEvent` sequence the loop
emits for a no-tool reasoning turn:

```
render events: turn_start, reasoning_delta ×3, text_delta ×2, completion_end
after finalize: MessageBox assistant 'The answer is 4.'
                  ReasoningRegion 'Thought'   ← survives
```

and for a tool-bearing turn the region survives inside the collapsed
`ExchangeBox`. Both cases keep the block. Read on the way through and found
sound: `openai.py` yields `ThinkingDeltaEvent` per chunk from `_extract_reasoning`
(streaming AND buffered transports), `agent_loop._stream_response` re-emits it as
a cumulative `thinking` block, `MessageDeltaProjector` diffs it per type,
`TurnStream._feed_message_update` turns that into `reasoning_delta`,
`_promote_answer` copies the region onto the promoted answer, and
`_reload_exchange` rebuilds it from a persisted `thinking` block.

So the loss is upstream of the TUI and specific to the model in use, and the next
step is one wire trace rather than a guess. **No fallback was added**: harvesting
the final `thinking` block at `message_end` would render something in the failing
case and hide whichever of those seven hops is actually dropping it.

`scripts/reasoning_trace.py` is the trace. Run it against the model that shows
the symptom, with a prompt that gets a plain answer and no tool call:

```
tau -p --mode json "In one sentence, why is the sky blue?" \
  | python scripts/reasoning_trace.py
```

It counts the content-block types on every bus event and separates the three
cases: `thinking` on `message_update` (it reached the bus, so the fault is in the
TUI after all), `thinking` only on `message_end` (produced but never streamed —
the provider yielded no `ThinkingDeltaEvent`), or no `thinking` anywhere (the
model or gateway reported none for this request).

#### The trace answered it: the third case, and the cause is `~/.tau/config.json`

Run against the model showing the symptom, the trace reported **no `thinking`
anywhere** — the server reported none. The `local-llm` entry carries

```json
"extra_body": { "chat_template_kwargs": { "enable_thinking": false } }
```

and `openai.py` spreads `model.extra_body` into every request body, so every call
against that model told the server not to think. Proved by A/B against the same
llama.cpp instance (`192.168.1.100:8080`, `Qwen3.8-27B-Q4_0`), same prompt,
`stream: false`:

| `enable_thinking` | message keys | `reasoning_content` | `content` |
|---|---|---|---|
| `false` | `content`, `role` | absent | 1208 chars |
| omitted | `content`, `reasoning_content`, `role` | 881 chars | 0 chars |

pi shows reasoning for the same model because pi's config does not carry the
flag: `~/.pi/agent/models.json` defines `local-llama` at the same `base_url` with
no `chat_template_kwargs`, and `settings.json` sets
`"defaultThinkingLevel": "medium"`.

**τ's extraction is not at fault and was not changed.** It reads the same three
fields in the same priority order as pi (`openai.py:583` /
`openai-completions.ts:561`).

The flag is the constrained-decoding workaround from
`docs/REASONING-VS-CONSTRAINED-DECODING.md`, written into the config as a
standing model default. It does not need to be: `_apply_constraints`
(`openai.py:436`) sets it itself, and only for a `grammar`/`choices` constraint —
a `json_schema` constraint returns before that line on purpose, and an
unconstrained call never reaches it. The static copy is strictly stronger than
the automatic one, and because the automatic path is guarded by
`if "chat_template_kwargs" not in payload`, it is also what τ's own gate defers
to. Removing the `extra_body` block restores reasoning on unconstrained calls and
changes nothing about constrained ones.

**Not changed, and worth naming:** τ does not warn that a model's `extra_body`
disables thinking. Detecting it would mean τ reading meaning out of an opaque
passthrough dict, which is the one thing `extra_body` exists not to do. Recorded
in the 0.9.4 release note under "Reasoning text reaches the screen" instead, where
a reader hitting the symptom will look.

#### Found by the same trace: `message_start` fired once per delta

The trace's own counts gave it away — 2137 `message_start` and 2137
`message_update` for one answer. `agent_loop._stream_response` emitted
`message_start` from its `TextDeltaEvent` branch, so the bracket that is supposed
to open ONE assistant message opened once per text delta, and never opened at all
for a completion that produced only reasoning or only a tool call. pi emits it
once, on the stream's `start` event (`agent-loop.ts:323`).

τ's streaming vocabulary has no `start` event, so the bracket now opens on the
first content event of any kind, through one `start_once` guard the three delta
branches and the `DoneEvent` branch all call. The `DoneEvent` call is what keeps
a completion that yields no delta from closing a bracket nothing opened.

The TUI never read `message_start`, so nothing rendered wrong; `tau -p --mode
json`, the RPC event stream and any SDK consumer pairing the two events did.
Five tests in `TestMessageStartBracketsOneMessage`; four of the five fail at
`985091f`, and the fifth is a regression guard on the aliasing the shared content
list would have introduced.

---

## 5. The tree editor has no manual

### What was reported

> "I need a elide / tree view 'draft manual' to figure out how to work the
> current tree editor view."

### Why this is separate from §4, and why it goes first

`docs/TREE-BROWSER-AS-EDITOR.md` is a *design* document. It says what each
decision was and why the rejected alternative was rejected. It does not say
which key does what. A reader who wants to operate the browser has to
reconstruct the key table from `BINDINGS`, the zone meanings from a
`_LABEL_ZONES` tuple, and the post-`Enter` flow from `action_browse_tree`.

§4 is blocked on hands-on feedback. Hands-on feedback is blocked on being able
to work the thing. So this is the first item in the cycle, and it is a
documentation task with no code in it.

**Built 2026-08-23: `docs/TREE-EDITOR-MANUAL.md`.** Task-oriented rather than
architectural: opening and leaving, what a row is, why indentation counts forks,
the key table, the detail pane, marks, the colour table, the four post-`Enter`
actions, and the two-node elide flow written as numbered steps.

It states the unfinished parts as unfinished rather than omitting them, in a
section of its own (§10): marks lead nowhere, there is no copy or plan buffer,
there is no archive gesture, a compaction's fold has no header row, the detail
pane cannot be dismissed, a turn cannot be collapsed, and nothing distinguishes
a sub-agent's branch from a user's fork. A manual that quietly skipped those
would teach the reader that a gesture is broken rather than unbuilt.

Two caveats on it. It was written from the source, not from a running terminal,
so a claim about what appears on screen is a claim about what the code says
should appear. And it is dated in its own header, because it describes a surface
that is expected to change.

---

## 6. Swappable TCSS themes

### What was reported

> "another punch list item, because I think it'll be cheap, and fun: TCSS
> themes, to write a few and make them selectable / swappable inside the TUI."

### What this changes about earlier decisions

This **supersedes the standing deferral of theme work.** The previous position
was that the TUI stays Catppuccin and the FFwF palette waits. That deferral was
about *choosing* a palette. This item is about being able to have more than one,
which is the thing that makes choosing cheap instead of committing.

The §10 non-goal on styling still holds for everything else: this authorizes a
theme mechanism and some themes, not a pass over individual widgets.

### Why it is cheap, if it stays cheap

The groundwork is already done and was done deliberately. Colour lives in
`parley.tcss` and not in `app.py` — the tree browser's ten zone classes, for
instance, are component classes resolved in the stylesheet, so a theme can
recolour every one of them without touching Python. That was the stated reason
for putting them there.

So the mechanism is: more than one `.tcss`, a way to name one, and a way to
change which is live without restarting.

### The parts that need deciding

* **Where a theme is selected.** A config key in `~/.tau/config.json` is the
  standing setting; a command or a picker is the in-session swap. Both were
  asked for ("selectable / swappable"), and they are two different surfaces.
* **What counts as a theme.** A whole replacement `parley.tcss`, or a small
  palette file layered over one shared structural stylesheet. The second is much
  less to write per theme and much less to keep in sync; the first cannot get
  out of sync at all. This is the one real design decision in the item.
* **Whether a user can ship their own.** The owner's standing preference is for
  software that can be forked and reused without hard-coded paths, which argues
  for reading a theme from a path the user names rather than only from a
  built-in list.

### Fail Early

A named theme that does not exist must be an error naming the theme, not a
silent fall back to the default. A theme that fails to parse must say so. The
snapshot suite compares rendered SVGs byte for byte, so whatever the default is,
it has to stay fixed and the tests have to keep getting it.

### Built — 2026-08-24

**Neither of the two candidate mechanisms. Textual already has a theme system
and this exact pin (8.2.7) has all of it**, so the answer is to use it rather
than to hand-roll a stylesheet swap beside it.

`textual.theme.Theme` carries a `variables: dict[str, str]` mapping that
`App.get_css_variables` merges into the CSS variable namespace, so
`parley.tcss` says `color: $tau-text` and the theme decides what that is.
`App.register_theme` / `App.theme = name` / `App.available_themes` are the
registry, the switch and the listing; setting `App.theme` calls `refresh_css`,
which is the live swap. Nothing was written that Textual would have done.

That resolves "what counts as a theme" as **the palette-layer shape (b)**,
because the palette is a Python object rather than a second `.tcss`. The reason
to prefer `Theme` over a bare variables dict is the half `parley.tcss` cannot
reach: `Footer`, `Header`, scrollbars, `Toast`, the command palette, the `Tree`
cursor line and every `Button` default are coloured from the design tokens
`$primary`/`$surface`/`$background`/`dark`, and `dark` is what puts
`-light-mode` on the app. A light τ palette laid over a dark Textual base is a
light chat pane under a dark Footer. A `Theme` is both halves at once, which is
what a theme actually is.

**The split is enforced, not asserted.** `parley.tcss` now contains zero colour
literals; `tests/test_themes.py::test_the_structural_stylesheet_names_no_colour`
fails on a hex, an `rgb()`/`hsl()`, or a named CSS colour in any declaration
value, and two more tests close the loop in both directions (a `$tau-…` no theme
defines, a palette key no rule reads). Without that first test, (b) degrades
into (a) with extra steps the first time someone types `color: #cdd6f4`.

The vocabulary is 25 role-named variables (`themes.TAU_PALETTE_KEYS`), not 19
hue-named ones. Where the old sheet deliberately reused one hue for two things —
the branch-summary pair borrowing the user cyan (§4.3's comment), the hover
divergence borrowing the assistant amber — the rules now name the same
*variable*, so the pairing survives a swap by construction instead of by two
literals happening to agree. Seven of the tree browser's ten zones borrow a role
or text variable that way; `$tau-zone-path` is the one zone with no role to
borrow.

**Themes:** `mocha` (default, Catppuccin Mocha — byte-identical to the
pre-theme TUI: its design tokens are `textual-dark`'s verbatim, re-derived from
`textual.theme.BUILTIN_THEMES` by a test so a Textual upgrade fails there rather
than in seven SVG diffs), `latte` (Catppuccin Latte, light — the unserved case),
`gruvbox` (warm, higher-contrast dark — chosen to be *unlike* the first two
rather than a third pastel).

**Selection:** `"theme": "<name>"` in `~/.tau/config.json` (now in the packaged
first-run template, so the key is discoverable) is the standing setting;
`ctrl+p → "Theme: <name>"` is the in-session swap, and it persists through the
same `update_config` read-modify-write `/prompt` uses. The active entry is
marked rather than hidden. `~/.tau/themes/<name>.json` (`extends`, `palette`,
`textual`) lets a user ship their own without forking the package, and a file
named after a built-in replaces it.

**Fail Early:** a configured name nothing answers to raises `ThemeError` (a
`ConfigError`, so `cli.main` prints it) naming the name and listing the
alternatives; so does a non-string `theme` key, an unparseable theme file (named),
an unknown `extends`, an unknown `palette` key, and an incomplete palette.

`devshot` grew `--theme NAME` (repeatable, in the output filename) because the
render/look/edit loop is how the light theme's contrast problems were found.

#### What resisted being themed

More useful than the themes. This is the inventory a future FFwF theme has to
fight.

1. **The seven `.box-*` role hues are the FFwF blocker, unchanged.**
   `.box-user`, `.box-assistant`, `.box-system`, `.box-toolCall`,
   `.box-toolResult`, `.box-error`, `.box-blocked` differ from each other by
   *border colour and nothing else* — same `solid` style, same geometry, same
   title placement. `role-blocked` and `role-assistant` are two variables that
   Mocha and Gruvbox both resolve to the same amber, which is exactly the
   degenerate case: with one accent, "an extension vetoed this call" and "the
   model answered" become the same border and the only remaining differentiator
   is the title text. Gruvbox is the miniature rehearsal — it has one purple, so
   `accent`, `role-system` and `role-foreign` all land on it, and the foreign
   lane stays readable only because its border is `dashed` and indented, i.e.
   because of *structure*. A one-accent theme needs that move generalised to the
   other six, and that is a structural change §10 currently forbids.
2. **Syntax highlighting is reachable only through the design tokens.**
   `textual.highlight.HighlightTheme` writes its token styles against
   `$text-accent`/`$text-warning`/`$text-primary`/`$text-success`/`$text-error`,
   never against `$tau-*`. So a fence's keywords and a tool call's JSON follow
   the theme's *Textual* half, and a theme that tunes the τ palette while leaving
   the design tokens at some other palette's values gets code blocks from a third
   palette. There is one highlight theme for light and dark alike (only the
   `ansi=True` variants branch).
3. **`Markdown CodeBlock` is dead CSS.** No `CodeBlock` widget exists in textual
   8.2.7 — a fence mounts `MarkdownFence` holding a `Label` — so that rule
   matches nothing and `$tau-code-fg` currently paints nothing. Labelled in place
   rather than deleted (a deletion is structural, and the intent is worth keeping
   if the widget returns). Point 2 is why nobody noticed.
4. **Some of Textual's derived defaults are wrong for τ and must be overridden
   per theme.** Latte needed `scrollbar*` (its `$primary` mauve as a 40-row
   scrollbar thumb is the loudest thing on a light screen, which inverts the
   hierarchy the palette is arranged around) and `footer-key-foreground` (its
   peach is 2.6:1 on the Footer panel). `_derive`'s `extra` argument exists for
   exactly this, and it is a per-theme cost that no amount of τ vocabulary
   removes.
5. **A borrowed palette is not a theme.** Catppuccin Latte's published accents
   are tuned as *borders* on a light base — its yellow is 2.3:1 against the chat
   pane, its peach 2.6:1 — and τ paints whole rows in them
   (`.tool-box > CollapsibleTitle`). Every Latte role hue is deepened, and
   `test_no_theme_leaves_text_unreadable_against_its_own_background` re-measures
   the pairings that actually occur in the sheet against tiered WCAG minimums,
   because a reviewer looks at one screenshot and the hue that fails is the one
   that screenshot did not contain. Dark themes get this for free; light is where
   the shortcut stops working.
6. **The relative-style channels came through untouched, and they are the
   escape hatch.** `text-style: dim|strike|italic|bold|underline` and
   `opacity: 55%` (the detail pane's context messages) compose with whatever
   colour lands on them, so `#chat-placeholder`'s "one colour plus Rich `dim`"
   pattern, the zone layer's strike/bold/italic distinctions and the hover
   underline all survive any palette, including a one-accent one. That is the
   direction a future FFwF theme has to go, and the zone layer is already there —
   seven of ten zones are role-hue *plus* a weight or decoration, so they would
   still read with every hue collapsed to red.

**Not attempted:** the FFwF theme (deferred on point 1, which this work did not
change).

### Second pass — 2026-08-24

Three decisions taken after reviewing the above with the owner.

**A theme that fails now toasts and falls back to the default.** This reverses
the "Fail Early" paragraph two headings up, and the reversal is the point. One
unparseable file in `~/.tau/themes` used to stop τ from starting — including
when the theme in use was a built-in and the broken file was one the user was
not selecting. `~/.claude/CLAUDE.md` draws exactly this line: Fail Early is
about not hiding a problem, not about manufacturing one. So
`build_theme_registry`/`load_user_themes` take an `errors` list; given one, a bad
file is collected and skipped, and `Parley` raises one error toast per failure in
`on_mount` and runs in `mocha`. The problem is still shown, on the one screen the
user is looking at, and τ still starts.

Passing no `errors` list keeps the raising contract, which is what the tests and
`testing.scenes.stage_scene` want — a screenshot captured in a theme other than
the one asked for is a wrong picture that looks like a right one, and there is no
user there to read a toast.

Three rules fall out, each with a test:

* A broken file named after a built-in leaves the **built-in** standing, so the
  fallback target cannot be removed from the registry by the failure it is
  falling back from.
* A *swap* to an unknown name reports and keeps the current theme. The startup
  rule would be wrong here: it would take the colours away from a user who was
  already running a theme they chose, because they mistyped a different one.
* A clean start raises no theme toast at all. A notice on every launch is a
  notice nobody reads, which would cost the failing case its only channel.

**`--theme NAME` exists**, one run only. It rides `cli_overrides` into
`self.config` in memory, and `action_set_theme`'s `update_config` re-reads the
*file* rather than writing `self.config` back — so `tau --theme gruvbox`, then
picking latte from the palette, saves latte and never gruvbox. It is deliberately
not validated in `cli.py`: the registry a name has to be in includes
`~/.tau/themes`, which only the app reads, and a second reader there would be a
second answer to "what themes are there".

**A fourth theme, `ansi`.** The only theme that can fit a terminal τ has never
seen: every value is an ANSI colour *name*, so the 16 colours the user already
curated decide the look. Verified before it was written — all 25 palette values
`Color.parse` to `ansi=` colours, two scenes render, 38 widget styles resolve to
an ANSI colour, and no `$tau-*` variable in `parley.tcss` is used with a
percentage or a `darken`/`lighten`, which was the one thing that could have
broken it.

It paints **no backgrounds**. Every surface is `ansi_default`, because
`ansi_black` is a black sidebar on a light terminal and invisible on a dark one,
and the theme cannot know which it is in. Roles use the non-bright half for the
same reason (a terminal tunes its normal six against its own background), with
bright used only for the two roles that need a second shade of a hue already
spent — the same pairs Mocha spends two adjacent hues on.

Two costs, measured on the `tools` scene: 9 distinct colours against mocha's 14.
The six-step text ramp collapses to three, and `border`/`border-subtle` become
one colour. `dark=True` is inherited from Textual's `ansi-dark` and is the one
guess in it; a light-terminal user overrides it with a two-line user theme
(`{"extends": "ansi", "textual": {"dark": false}}`), which is what that format is
for.

The measurement tests exclude it — not by name, but through `MEASURABLE_THEMES`,
which filters on whether a palette is hex. Asserting a contrast ratio on
`ansi_red` would be asserting a fact about the SVG exporter's stand-in palette,
i.e. about a terminal nobody is using. Two rules guard it instead: every value is
an ANSI name, and every surface is `ansi_default`.

Also closed: `docs/tau-coding-agent.md` had a CLI flag table and no config-key
section at all. It now documents the four themes, the three selection surfaces,
the failure behaviour and the user-theme format. A complete `config.json`
reference is still not written, and the new section says so rather than implying
it is one.

---

## 7. `--fun` never reaches anyone who installed τ

### What was reported

> "`--fun` is not correctly swapping on the distributed versions. WE NEED FUN
> ENABLED, it's disabled in dev for test suite perfect output only."

### Root cause — verified 2026-08-23

The report is correct, and the mechanism is a gap between two build paths.

`FUN_DEFAULT` is `False` in the source tree, on purpose, so a developer's
snapshots and `devshot` PNGs are deterministic without passing a flag.
`package.sh:79` flips it with a `sed` against the exact line, and
`package.sh:80-82` verifies the substitution landed rather than shipping a
silently unpatched build. That verification works. It is just attached to the
wrong artifact.

`package.sh` builds one thing: the `tau-<version>.tar.gz` attached to the GitHub
release. **The PyPI wheels and sdists do not come from it.**
`.github/workflows/publish.yml:202` runs `python -m build --sdist --wheel` per
package directly against the source tree, and never invokes `package.sh` at all.

So today:

| Artifact | Built by | `FUN_DEFAULT` |
|---|---|---|
| `tau-<version>.tar.gz` (GitHub release) | `package.sh` | `True` |
| The five PyPI wheels and sdists | `publish.yml` | `False` |

`pip install` is how essentially everyone gets τ, so the flip has never reached
a real user. The tarball nobody installs from is the only place it works.

### What is not broken

The `--fun` flag itself. `cli.py:423` parses it, `cli.py:425` defaults it to
`FUN_DEFAULT`, and `pick_tagline` consumes it. A packaged user typing `--fun`
gets random taglines today. What is broken is the *default*, which is the whole
point of the mechanism — the flag exists so the default can be overridden in
both directions.

### The fix has a choice in it

Three ways, and they are not equivalent:

1. **Do the same `sed` in `publish.yml`.** Smallest change. Also duplicates a
   rewrite rule in a second place, and the next build path added will have the
   same gap again.
2. **Have `publish.yml` call `package.sh`, or share one staging step.** Removes
   the duplication. Awkward, because the two produce different artifact shapes
   from different trees.
3. **Invert the default: `FUN_DEFAULT = True` in source, and have the
   deterministic surfaces ask for `fun=False` explicitly.** No build-time
   rewrite at all, so no build path can miss it. `test_chat_placeholder.py:323`
   records that `Parley()` already takes `fun=False` directly rather than
   reading `FUN_DEFAULT`, so most of the determinism does not depend on the
   default at all — but `test_chat_placeholder.py:303`, `:314` and `:350` assert
   the source literal and the `sed` rule, so those three tests are the change's
   real cost.

Option 3 is the one that cannot regress silently, and silent regression is
exactly what happened here. It is a recommendation, not a decision.

### Acceptance

Not "the code is changed". A wheel is built the way `publish.yml` builds one,
installed into a clean venv, and `tau` shows a tagline other than
`TAGLINES[0]` across a few runs. Anything short of that is the same class of
check that already passed while the bug shipped.

### Built — option 3, 2026-08-23

`FUN_DEFAULT = True` in `tagline.py`. The `sed` and its verification are gone
from `package.sh:65-84`, replaced by a comment saying why the script rewrites no
source; `publish.yml` is unchanged, because there is now nothing for it to miss.
`cli.py` remains the only reader of `FUN_DEFAULT`, and `Parley.__init__` still
defaults `fun` to the literal `False` — untouched, and now load-bearing: it is
what keeps `testing.scenes`, the snapshot suite and `devshot` deterministic
without reference to how the tree was built.

Four tests moved to the new arrangement rather than being deleted (the plan
above found three; `test_cli.py:918` asserted the old default too):

| Test | Now asserts |
|---|---|
| `test_chat_placeholder.py::test_fun_default_is_on_in_the_source_tree` | `FUN_DEFAULT is True`, and the source literal |
| `test_chat_placeholder.py::test_no_build_path_rewrites_the_fun_default` | neither `package.sh` nor `publish.yml` mentions `FUN_DEFAULT` outside a comment |
| `test_chat_placeholder.py::test_deterministic_surfaces_name_their_own_fun_rather_than_inheriting_it` | `inspect.signature(Parley.__init__)`'s `fun` default is `False` |
| `test_cli.py::test_main_launches_tui_with_overrides` | an unflagged TUI launch passes `fun=True` |

The second and third are the pair that keeps the defect from returning: the
default may not be patched in by a build step, and no deterministic surface may
inherit it.

Acceptance met as specified — `python -m build --wheel ./tau-coding-agent`,
installed into a clean venv: `FUN_DEFAULT` is `True`, `parse_cli_args([]).fun`
is `True`, 200 unflagged picks return all 9 taglines, and `--no-fun` returns
`TAGLINES[0]`. Suites: `tau-coding-agent/tests` 1446 passed / 3 skipped,
7 snapshots byte-identical (not regenerated).

---

## 8. Debts, unscheduled

Not in scope unless deliberately pulled in.

* ~~50 mypy findings under mypy 2.3.1~~ — **the debt does not exist. Closed
  2026-08-23.** `docs/PLAN-0.9.3.md` §6 recorded it, and I re-measured it at 52
  before investigating. Both numbers are measurement artifacts.

  The findings appear only when mypy runs against an environment that does not
  have the project's dependencies installed. `ignore_missing_imports = true`
  turns every unseen third-party base class into `Any` — `pydantic.BaseModel`,
  `rich.Console`, the Textual widgets — and `warn_return_any = true` then reports
  every method that returns one of those values. That is the whole of the 49
  `no-any-return` findings, and it explains the other three as well: the two
  `extensions/registry.py` ones are `model_validate` returning `Any` so that
  mypy cannot narrow the union `register_tool` normalizes at runtime, and the
  `app.py:3748` one is a Textual base class being invisible.

  The controlled experiment, run across both mypy versions and both
  environments:

  | mypy | dependencies visible | result |
  |---|---|---|
  | 2.3.1 | no | 52 errors in 14 files |
  | 2.1.0 | no | 52 errors in 14 files — **byte-identical output** |
  | 2.3.1 | yes | 0 errors, 95 files |
  | 2.1.0 | yes | 0 errors, 95 files |

  The mypy version is not the variable. It never was. Run a newer mypy with
  `--python-executable venv/bin/python` and it reports success.

  Nothing was changed in the source, and nothing should be: annotating these
  away would hard-code a workaround for a broken measurement into production
  code, which is the anti-pattern the owner's Fail Early rule names directly.

  **What is worth reconsidering is `ignore_missing_imports = true` itself.** It
  is what converts "you forgot to install the dependencies" from a loud
  `import-not-found` into 52 quiet, plausible-looking type errors that cost real
  review time to diagnose — twice now, in two different cycles. Turning it off
  would make a misconfigured checker say so. That is a gate-configuration
  decision, not a typing one, and it is unscheduled.

* **58 ruff findings outside the gate's scope.** `ruff check .` over the whole
  repo reports 58, and `ruff format --check .` wants to reformat 13 files. The
  gate deliberately scopes to the `src` trees, where both are clean. The
  out-of-scope findings are in `tau-agent-core/tests` (45), `run_agent_loop.py`
  (9), `experiments/m2` (3) and `tau-llm/tests` (1), and are only three rules:
  F841 unused local (33), F401 unused import (16), F541 f-string with no
  placeholder (9). None is a defect. Recorded because "ruff is clean" is said
  often enough in this repo to be worth qualifying.

* **Three synchronous call sites block the UI event loop.** Found while
  investigating §2, none of them the cause of it. The agent loop runs as an
  async Textual worker on the app's own event loop, with no thread, so anything
  synchronous freezes painting and input directly.
  - `grep` and `find` do a synchronous `os.walk` with no `to_thread`. `bash` is
    correctly async.
  - `read`, `write` and `edit` do synchronous file I/O.
  - **Worst: persistence at turn end.** `_persist_loop_messages` is a plain sync
    method called after `loop.run()` returns. With the JMFTS store each appended
    message is a synchronous `httpx` POST, so a ten-tool turn issues about 21
    blocking round-trips on the UI thread at the turn boundary.

  Not the streaming symptom, but a real freeze, and the last one gets worse with
  turn length.

* **Two docs describe a pipeline that no longer exists.**
  `docs/TOOL-CALL-PIPELINE.md` and `docs/tau-coding-agent.md` still describe
  `TauBackend.stream_chat` with a `callback(delta)` and a 30 Hz render throttle.
  The TUI has used `subscribe_render`/`RenderRouter` since B3-a, and the 30 Hz
  throttle was deliberately removed. `CLAUDE.md`'s architecture section inherits
  the same description. Cheap to fix and currently misleading anyone tracing the
  path — including me, earlier in this cycle.

* **`docs/TECTUM-NO-TOOLS-MIGRATION.md`** — six sites, still the Tectum owner's
  call, still live on the dev box. Carried from 0.9.3 §6 unchanged.

* **No retry or backoff anywhere in `tau-llm`** (0.9.3 §4.3). The seven-backend
  probe found UnoRouter 429s that name their own retry interval, so the
  information needed is on the wire and unused.

* **No repeat-tool-call detection in `agent_loop.py`** (0.9.3 §4.2 item 2). This
  got worse in this cycle, not better: `max_turns` now defaults to `None`, so
  the 50-turn ceiling that used to bound a model calling the same tool forever
  is gone. See `docs/RELEASE-NOTES-0.9.4.md`.

* **A stated `--max-turns` ceiling is still reached silently.** Nothing in the
  event stream distinguishes a truncated run from a finished one. Recorded in
  `docs/CLI-PLAN.md`.

* **Multi-vendor §4.4 step 5's remainder** — pluggable auth and a model
  resolver.

* **Seven CLI flags remain unbuilt.** `docs/CLI-PLAN.md` §3 is the inventory.

* **Tree browser steps not started** — `docs/TREE-BROWSER-AS-EDITOR.md` §10:
  item 4d (the compaction fold header, parked undecided because it rewrites row
  order so vertical position stops meaning time), step 7 (the plan buffer, copy
  entries, the commit algorithm — which is what would give `tree--zone-copied` a
  producer and would widen `TreeIntent` beyond one action and a tuple of ids),
  and the archive gesture (§11.2 decided archive is view state; only the
  collapse half exists).

---

## 9. Sequencing

1. **§5, the manual — done 2026-08-23.** No code. It unblocked §4's discussion
   and nothing else depended on it, so it went first.
2. **§7, `--fun` — done 2026-08-23.** Out of order on purpose. It was the
   smallest item in the cycle, its cause was already found, and it was the only
   one currently wrong in a shipped artifact. Also a prerequisite in spirit for
   §6: both are about what a packaged user's screen looks like, and there is no
   sense picking themes while the default screen is still the developer's.
3. **§3, `Esc` loses the turn.** Diagnose, then fix. First of the real work
   because it is the only item that destroys work, and because an abort that is
   safe makes every other experiment in this cycle cheaper to run.
4. **§2a, reasoning never renders.** A one-line-class bug with a measured
   reproduction and no test that can see it. Independent of everything else, and
   the cheapest real fix in the cycle. The test harness change matters as much
   as the fix: a suite that pauses after every event cannot catch a burst bug.
5. **The quadratic layout cost — §1 and §2b together. Done 2026-08-24.** These
   were written as two items and are one. 67 % of a reload and 91 % of streaming
   CPU are the same `Screen._refresh_layout`, and a cap on mounted widgets is
   the same lever for both. Treat them as one piece of work with two acceptance
   criteria. Within it, two cheap things first: `_sync_placeholder`'s two DOM
   walks per mount (12 % of a reload, self-inflicted, fixable alone) and the
   `scroll_end(animate=False)` on every delta. The first was worth 3.4 s of a
   200-message reload; the second turned out not to be a contributor at all. The
   third change, `App.batch_update` around the reload, was not in this list and
   is worth as much as the placeholder fix. §1 has the measurements and the one
   case the cap does not reach.
6. **§2c, the liveness indicator. Done 2026-08-24.** After 5, because a counter
   on a screen that repaints at 6 Hz is a counter nobody sees move. The design
   is now decided by a fact rather than a preference: measured per-completion
   totals that step at each tool boundary, plus a labelled estimate for the
   completion in flight. Built on the exchange title, driven by a 4 Hz timer
   rather than by deltas — the silence that matters most has no deltas in it.
   §2c has the readout, the measurements and the two rejected surfaces.
7. **§6, themes.** Independent of everything above and can slot in anywhere,
   but after §7 so the default it ships against is the real one.
8. **§4, the tree editor.** Unblocked 2026-08-24: the owner worked the browser
   against a real forked session and reported four things. §4 records them and
   the three-step build. Step 1 (the scrollbar width) is one line and
   independent; step 2 (a presentation tree — turn groups and hidden `navigate`
   rows) is the large one; step 3 (Enter on a user message forks from its parent
   and prefills the input) depends on neither but reads better after 2. One open
   question, in §4's *What is NOT settled*: whether to build pi's general filter
   mode or only the one hiding rule.

§8's mypy debt turned out not to exist and is closed. Nothing in §8 is
scheduled.

The ordering principle: **stop losing data, then start showing what is
happening, then make it fast.** §1 is the most visible annoyance and is
deliberately not first, because a faster path to a screen that still goes silent
mid-turn is a worse outcome than a slow path to one that does not.

One revision to that principle after the measurements. "Make it fast" turned out
not to be a separate phase: §1 and §2b are the same defect seen from two
gestures, and the transcript size that makes loading slow is the same one that
throttles streaming to 6 tokens per second. So step 5 is the largest single item
in the cycle, and the two cheap wins inside it are worth taking first because
they are independent of whatever the mounted-widget cap turns out to be.

---

## 10. Non-goals

* ~~**No tree-editor code changes before the §4 discussion.** Recorded twice on
  purpose.~~ **Lifted 2026-08-24** — the discussion happened. §4 has the four
  items and the three-step build; the styling deferral below still stands, so
  none of those steps changes a colour or moves a widget.
* **No per-widget styling.** §6 authorizes a theme mechanism and some palettes.
  It does not authorize revisiting individual widgets' layout or chrome, which
  the owner has deferred: "I'd like to see some more stuff land before I start
  making style decisions." Colour goes in the stylesheet; nothing moves.
* **No context-window management beyond what exists.** §1 is a rendering bound.
  Compaction and elide already handle the model-input side and are not reopened
  here.
* Trust gate (Tier 8), Tier 9 output surfaces, Tier 10, Tier 11 M4/M5 — still
  out, as in 0.9.3 §8.
