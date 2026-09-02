# The live transcript window

**Built 2026-08-31.** A τ session that keeps streaming stops growing its widget
tree. `ChatDisplay` capped a *reloaded* transcript from the start
(`RENDER_CAP_TURNS = 4`) and capped a *live* one never — so a chat that was
trimmed the moment it was opened climbed straight back past the cap as the reader
worked in it, and every turn made the next turn slower. `trim_to_cap` closes that
gap by applying the reload's own cap as each turn ends.

§1–§6 are the bound. **§7 is the window MOVING** — scrolling against either edge
slides it one turn along the transcript, so the reader can read the linear
history of any point without the mounted count ever growing. It was added the
same day and it reverses §6's first entry, which had said there would be no
scrollback.

## 1. The symptom, measured

Textual 8.2.7, this tree, headless `run_test(size=(120, 40))`. The feed is 200
deltas at 40/s, so a renderer that costs nothing finishes in **5.0 s**. The
backlog is built from complete turns — reasoning, three tool calls with results,
an answer with a fenced code block — which is **66 widgets per turn**.

| mounted turns | widgets | wall clock | lag |
|---|---|---|---|
| 0 | 1 | 5.2 s | 0.2 s |
| 20 | 1321 | 7.7 s | 2.7 s |
| 40 | 2641 | 11.7 s | 6.7 s |
| 60 | 3961 | 18.9 s | 13.9 s |

The lag is superlinear in the backlog. At 60 turns the TUI is still drawing
fourteen seconds after the model stopped talking. A 200-turn session — which is
an ordinary day's work — is about 13,000 widgets.

The cost is not per delta *in the message being written*. It is per delta
**times the size of the whole transcript above it**, which is the shape that
makes a long session progressively unusable rather than merely slow.

## 2. Why it is not τ's code

Every `MarkdownBlock` is a `Static`, and `Static.update` reaches:

```
Static.update → Widget.refresh → Widget._set_dirty → Widget.size
              → Screen.find_widget → _compositor.find_widget
              → _compositor.full_map
```

`full_map` builds the widget map with `visible_only=False` — it arranges the
**entire** screen tree, with none of the viewport pruning `reflow` does. It is
cached, and a delta changes the layout, which invalidates the cache. So the next
block update rebuilds the map over every widget in the transcript.

In a `cProfile` of 76 deltas at a 60-turn backlog, `_compositor.add_widget` was
**8.5 s of 17.6 s**, called 118,144 times for 67 arrange passes — about 1,760
widgets walked per pass.

Nothing τ writes can skip that call. τ can only make the tree it walks smaller.

## 3. What does not work

Each measured at a 60-turn backlog, per delta, against a 417–436 ms baseline
(the spread is run-to-run noise on the same configuration).

| candidate | result | factor |
|---|---|---|
| coalesce the stream writes to 10 Hz | 18.9 s → 15.5 s wall | 1.2× |
| `display: none` on the old exchanges | 301 ms | 1.4× |
| drop the collapsed exchanges' interiors | 286 ms | 1.5× |
| flatten each finished `Markdown` to one `Static` | 225 ms | 1.9× |
| remove τ's own per-delta `scroll_end` | 489 ms | none |
| **evict whole turns** | **58 ms** | **7.2×** |

Four of these are worth stating individually, because each is a plausible fix
that a reader would otherwise try.

**Hiding is not removing.** `display: none` on every exchange but the last three
bought 1.4×. `full_map` still pays for a hidden widget. Any design built on
hiding, collapsing, or `visibility` will not work, and this is the measurement
that says so.

**Collapsing a finished turn to one `Static` is worth 1.9×, not the problem.**
Replacing each finished `Markdown` and its block tree with a single
`Static(rich.markdown.Markdown(text))` — the widget Textualize discussion #6414
asks for — halves the DOM (3961 → 2041) and roughly halves the cost. It also cost
5.0 s to convert 60 turns, because it mounts and removes 250 widgets. It is a
real improvement and it does not change the shape of the curve: the cost is still
proportional to the whole transcript.

**Dropping only what is already invisible is worth 1.5×.** A finished
`ExchangeBox` is collapsed, so its steps, reasoning and tool boxes are not on
screen. Removing all of them left 1801 widgets and 286 ms, because what remains —
one promoted answer per turn, its `Markdown`, its blocks, the user box — is
itself proportional to the session.

**τ's own scrolling is innocent.** `MessageList.scroll_to_tail` runs
`scroll_end` on every delta while the reader is at the bottom. Removing it
entirely changed nothing (489 ms, inside the noise). Throttling it to 10 Hz
changed nothing. The suspicion was reasonable and it was wrong.

Only eviction works, because only removal is what `full_map` counts.

## 4. The mechanism

`ChatDisplay.trim_to_cap` (`app.py`) evicts the head of the live transcript down
to the same `RENDER_CAP_TURNS` / `RENDER_CAP_MESSAGES` a reload uses, and
`_maybe_trim` calls it as each turn's exchange closes.

**It reuses the reload cap rather than inventing a live one.** τ already had
`render_cap_start`, the `_elided` count, the clickable `⋯ N earlier` row, and
`show_all_messages`. The live window adds the eviction and
nothing else: one cap, one arithmetic, one number, one gesture to get it back.
A second live-only bound would be a second thing to explain and a second way for
the `⋯` row to mean something different on two paths.

### 4.1 The cut point is a user message, in both representations at once

`render_cap_start` names the cut as an index into the message list.
`trim_to_cap` walks the top-level children backwards for the same user box.

The two agree because **one top-level user `MessageBox` is mounted per user
message** — the live path mounts it in `add_message` before opening the exchange,
and the reload path mounts it at the top of each span. Counting user boxes from
the end and counting user messages from the end therefore arrive at the same
turn.

This is why the cut is not a widget budget, even though the cost law
(`delta_ms ≈ 45 + 0.10 × widgets`) is written in widgets. A budget lands
mid-turn: it would strand an `ExchangeBox` above the user turn that opened it,
and leave `_elided` with no honest number to report. A turn boundary is the only
place where the widget tree and the message list can be cut and still describe
the same place.

### 4.2 When it is safe to run

Two guards, both about not moving something someone is holding.

**Every lane must be closed.** `trim_to_cap` cuts by transcript position, which
says nothing about which lane a widget belongs to (B3-a: concurrent turns render
side by side). Trimming with a lane open could evict that lane's own exchange
out from under it.

**The reader must be at the tail.** Removing the head shifts every row under
someone who scrolled up to read, which is exactly what docs/TUI-STEERING.md's
scroll release exists to prevent. A trim that comes due while they are reading is
held in `_trim_deferred`, and `ChatDisplay.watch_scroll_y` runs it when they
return to the bottom. Reading while the model writes still works; the transcript
simply stops shrinking while they are up there.

### 4.3 One transcript attribute, re-read at every turn edge

`set_transcript_source(lambda: self.messages)`, wired once in `Parley.on_mount`.

A callable and not a list, for the same reason `_facts_source` is one. The app
**rebinds** its working list after every turn
(`self.messages = list(session.context)`) rather than appending to it, so a
reference taken at the last reload describes the conversation as it stood when it
was opened. Trimming against that would compute the cut for a transcript that
stopped growing, and `show_all_messages` would mount the same stale list: the
reader would click "show them" and be shown *less* than they already had.

The callable is **not** a second source consulted at the point of use.
`_refresh_transcript` calls it in `_maybe_trim`, before either guard, and writes
the result into `_reload_source`; `trim_to_cap` and `show_all_messages` then read
that one attribute. So `_reload_source` means "the transcript this display is a
view of", with two writers that agree — `reload_messages` when it is handed one,
and the turn edge when the app has moved on.

The first design did read the callable at the point of use, preferring it when
non-empty and falling back to `_reload_source` otherwise. That heuristic is wrong
and the test suite said so: after `action_new_chat` the app's list holds a system
message, so it is non-empty without being the mounted transcript, and
`show_all_messages` restored a one-message conversation. "Prefer whichever looks
fresher" is not a rule about which list is correct. Refreshing at the one moment
the app's list is known to be current is.

A display with no source — a bare renderer harness — keeps whatever
`reload_messages` gave it. That is not a fallback for a missing value: such a
display has no app behind it, so the list it was handed is the only transcript in
existence.

## 5. What it costs

The same feed as §1 — 200 deltas at 40/s, ideal 5.0 s — after 60 real turns
streamed through the live state machine:

| | widgets | user boxes | elided | 60-turn build | stream |
|---|---|---|---|---|---|
| window off | 3961 | 60 | 0 | 35.5 s | 19.42 s |
| window on | 266 | 4 | 224 | 20.6 s | **6.24 s** |

The lag falls from 14.4 s to 1.2 s, and building the session is 40 % faster
because every turn after the fourth is streamed into a small tree.

The price is that **four turns are on screen and the rest are behind the `⋯`
row**. That is already what a reader sees after any reload, so the live view and
the reloaded view now agree — but during a working session four turns is tight,
and this is the number to revisit first. It is `RENDER_CAP_TURNS`, one constant.
Raising it is linear in cost: §1's table is the price list.

§7 is what makes four turns liveable: the reader moves the window instead of
growing it.

## 6. What is deliberately absent

**No suppression after "show them".** `show_all_messages` mounts the whole
conversation, and the window trims it again at the end of the next turn. The
reader is not prompting while they read, so the re-trim lands when they have
moved on; and a window that switched off on request would be the unbounded
transcript back, which is the defect. If this proves annoying the fix is to widen
`RENDER_CAP_TURNS`, not to add a mode.

**No config key.** The bound is `RENDER_CAP_TURNS` / `RENDER_CAP_MESSAGES` on the
class, which is where the reload cap already lived. A knob is worth adding once
someone has run with the default and wants a different number, not before.

**No single-`Static` collapse of finished turns.** §3 measures it at 1.9× and
5.0 s of conversion. With the window in place the remaining tree is 266 widgets,
where 1.9× of a 6.24 s stream is not worth a second rendering path for finished
messages, or the loss of `Markdown`'s per-block selection and styling. If the cap
is ever raised far enough for the tree to matter again, this is the next thing to
build, and Textualize discussion #6414 is the upstream request for it.

**Nothing about model input.** The cap is a rendering bound and touches no
message list. The whole conversation is still loaded, still in the session log,
and still what the model is sent. If it ever changes what the model receives it
has become a silent context bug against docs/TREE-BROWSER-AS-EDITOR.md's
tree-as-truth invariant, which is why the eviction lives on the display and reads
the transcript without writing to it.

## 7. Sliding the window

**Built 2026-08-31, and it reverses §6's first entry.** That entry said there was
no scrollback into the evicted region, and gave the reason: a spacer of the
evicted height plus a re-mount on approach is more machinery than the gesture is
worth. The reversal is not a change of mind about the cost. It is that the
gesture does not need a spacer at all.

Scrolling against the top edge slides the window one turn back. Scrolling against
the bottom edge slides it one turn forward. The reader can walk the linear
history of any point in the conversation, one turn at a time, and the mounted
count never changes — because a step loads one older turn and drops one newer
one.

That is the whole idea, and it is why the earlier design was wrong to reject
scrollback. The cost is *how many* turns are mounted, not *which* ones. A window
that moves is free; only a window that grows is not.

### 7.1 A move is one step along `turn_starts`

`turn_starts` is the user messages plus 0 — the same candidate set
`render_cap_start` already picked from. A window can only start where the cap
could have put it, so `move_window` is `candidates[position ± 1]` and nothing
more.

This is why a move never asks how tall anything is. A design that scrolled by
pixels would have to measure the content it was about to unmount; a design that
steps by turns does not, and a step is always a whole turn because the list it
steps along is made of turn boundaries.

`window_end` is the forward twin of `render_cap_start`, and the two must agree at
the tail:

```
window_end(m, render_cap_start(m)) == len(m)
```

`test_window_end_agrees_with_render_cap_start_at_the_tail` asserts it across 58
transcripts. Without it, sliding forward to the end would stop one turn short and
leave a phantom `⋯ 0 later` row — and a window the reader slid back and forward
again would mount something a reload of the same conversation would not.

The message bound needs one explicit branch to hold that identity: when
`start + RENDER_CAP_MESSAGES` already reaches the end, the answer is the end. The
general branch answers "the last turn boundary within the bound", which at the
tail is the last turn's *start*, not the transcript's end.

Turns are counted from the window's first **turn**, not from `start`. A leading
system message makes those differ: `start` is 0 and the first turn opens at 1, and
counting from `start` spends one of the four turns on a message that renders
nothing.

### 7.2 Holding the reader's place

A move re-renders the whole window, so every widget is a new object and the
scroll position means nothing. What is preserved instead is a *turn*: moving back
puts the turn that was at the top back under the top edge, moving forward puts the
last turn of the old window there. Either way something the reader had just read
is still on screen, which is what makes repeated steps read as scrolling rather
than as paging.

`_render_window` records `_turn_anchors` — message index → the widget that opens
that turn — as it mounts, and `_settle_move` scrolls to the right one. The map is
valid only for the tree that just built it; the live path mounts turns without
registering them and `trim_to_cap` removes them without unregistering. That is not
a leak to fix: the map exists to hold a position across ONE re-render, and its
only reader runs directly after one.

Two Textual details decide whether this works at all, and both cost a wrong
version first:

**The new tree has no geometry when `move_window` returns.** It was mounted
inside `batch_update`, and regions are computed on the refresh that follows. So
the restore is a separate `call_after_refresh` step. Scrolling in `move_window`
itself silently did nothing.

**`scroll_to_widget` defers by default.** Its `immediate` parameter is `False`,
meaning the scroll waits for the next screen refresh — and `_settle_move` already
runs on one. Deferring twice landed the jump a frame after the content it
belonged to, and left the `_follow_tail` decision on the next line reading a
position that was about to change. `immediate=True` is required.

### 7.3 Two claims, and why both are needed

A move is decided synchronously in `watch_scroll_y` and runs a tick later. Two
things can go wrong in that gap, and they need different guards.

**`_window_moving`, claimed in the watcher.** One flick of a wheel posts several
scroll events before any scheduled call runs. A claim taken inside the coroutine
would let one gesture queue several moves; taken in the watcher, the second and
third events see it already held. It is released by `_settle_move`, not by
`move_window` — deliberately last, so the scroll the restore performs is not read
as the reader asking for another move.

**`_window_generation`, bumped by every `_render_window`.** The window can be
*rebuilt* between the decision and the move: a reload, or `snap_window_to_tail`
for a starting turn. The scheduled call carries the generation it was decided in
and declines to act on a different one. Without it a queued step back silently
undid the reload that overtook it — measured, and the reason
`test_a_reload_always_lands_on_the_tail` exists.

`_render_window` also detaches `_follow_tail` while it builds, because every
`add_message` on the way down calls `scroll_to_tail` — which would pin a window
the reader slid *back* to its own bottom edge, which is where a forward slide
would then fire.

### 7.3a The trigger is a gesture, not a position

The first version watched `scroll_y` and slid whenever the reader reached an
edge travelling towards it. That is wrong, and the test suite found it: **the
`⋯ N earlier` row became unclickable.** Reaching the row is what scrolling to the
top means, so arriving there loaded another turn and scrolled the row away —
every time, for ever. `test_clicking_the_earlier_row_mounts_the_rest` failed with
`OutOfBounds`, which is the click landing where the row no longer was.

A scroll position cannot tell "arrived at the top" from "still pushing against
it", and only the second is a request for more transcript. So the trigger moved
onto the events themselves: `_on_mouse_scroll_up` / `_on_mouse_scroll_down` for
the wheel, `action_scroll_up` / `action_scroll_down` for the keyboard, each
asking `_slide_at_edge` whether this scroll has anywhere left to go. The first
scroll takes the reader to the edge; the next one slides.

This also drops a whole class of false triggers for free. `watch_scroll_y` sees
mounting content, resizes and this class's own `scroll_end`, all of which land on
an edge and none of which is a reader asking for anything. A `MouseScrollUp` is
unambiguously a person.

### 7.4 A starting turn snaps back to the tail

The live state machine mounts into the END of the display. A window showing turn
5 of 40 would grow a live exchange directly under turn 8, in a place that means
nothing — so `begin_exchange` calls `snap_window_to_tail` first.

This does pull a reader out of history, and it is the cost of the design. It is
the cheaper of the two costs: the alternative is a live turn rendering where the
reader cannot see it, and the state machine having to tolerate streaming into a
box that is not on screen. A turn beginning is the present changing, and the tail
is what shows the present.

It does **not** disturb the ordinary read-while-writing case
(docs/TUI-STEERING.md). Scrolling up *within* the mounted window changes nothing
and snaps nothing; only a window actually slid off the tail is pulled back. The
line is: re-reading the last few turns is undisturbed, browsing history is
interrupted by the model starting to talk.

Two consequences follow, and both are asserted:

* `move_window` refuses while any lane is open. It cuts by transcript position,
  which says nothing about which lane a widget belongs to.
* `trim_to_cap` refuses while the window is slid. Its arithmetic is "count user
  boxes back from the end and cut there", which answers for a span that is not
  mounted. There is nothing to evict anyway — a slid window is already bounded.

### 7.5 What is still absent

**No dedicated key.** The arrow keys move the window at the edges, because
`action_scroll_up` / `action_scroll_down` are where they land — but there is no
binding that means "slide" on its own. Every key in the chat pane is already
spent (docs/SLASH-COMMANDS.md §3), and the scroll reaches both edges.

**No jump to an arbitrary point.** The window steps one turn at a time; there is
no "go to turn 12". The tree browser is where you navigate to a point, and
`show_all_messages` is where you get the whole thing at once.

**Still no spacer.** The scrollbar describes the mounted window, not the
conversation — so its thumb size does not shrink as the transcript grows, and
dragging it cannot reach a turn that is not loaded. The `⋯` rows are what say
there is more, in both directions, with a count.

### 7.6 What a step costs

Walking a 60-turn transcript from the tail to the top, one step at a time —
56 steps, four-turn window, tool calls and fenced code in every turn:

```
steps=56  widgets: min=89 max=90   step time: median 433 ms, max 598 ms
returned to the tail in 56 forward steps, later=0
```

**The mounted count does not move.** 89 or 90 widgets for the whole walk, the
one-widget spread being the `⋯ N later` row appearing. That is the claim of a
sliding window over a growing one, measured.

**A step costs 433 ms, and 95 % of it is building the four turns.** Tearing the
old window down is 259 ms across ten steps — 5 %. The rest is mounting ~88
widgets and parsing eight Markdown documents, which is simply what rendering a
four-turn window costs: a reload of the same four turns costs the same.

So the slide is not unusually slow; it pays the standard price, four times over,
for a step that only changes one turn at each end. **The obvious optimization is
an incremental slide** — mount the newly exposed turn at one end, remove the
turn falling off the other, keep the middle — which should cost about a quarter,
near 120 ms. It is not built. It needs positional mounting (`add_message` and
`_reload_exchange` both mount at the end today) and it needs `_turn_anchors` and
both `⋯` rows maintained rather than rebuilt, so it is a change to code the
reload path shares, and it is worth doing only if 433 ms proves to be a stutter
someone minds.

---

## 8. Who owns the scroll position

> **Built (2026-08-31).** Two defects, one owner. Reported as: *"I can only
> scroll up to the beginning of the conversation or down to the bottom; there's
> 46 messages between, but the window only ever lands at the top or bottom …
> scroll events seem extra twitchy."*

The window decides what is MOUNTED (§4, §7). This section is about where the
view sits inside it, which turned out to have three writers — the reload, the
reader, and Textual's `Collapsible` — and no rule saying which of them wins.

### 8.1 A reload landed at the top of a real transcript

`reload_messages` ends with `scroll_to_tail()`, and `test_a_reload_ends_scrolled
_to_the_newest_message` has asserted since the window landed that it works. It
does — on a transcript of plain messages. On a real one it did not: resuming a
46-message session opened it at the FIRST message.

The measurement, on a real session file, before the fix:

```
46 messages: 3 user, 22 assistant, 21 toolResult
after reload: scroll_y=0 max=557 follow_tail=True virtual_h=597
```

`scroll_to_tail` scrolls to `max_scroll_y`, which comes from `virtual_size` —
and `_render_window` mounts inside `App.batch_update`, whose whole purpose is to
hold the layout off (§4). So the scroll runs against the PREVIOUS layout's
answer, usually zero, clamps, and does nothing. `move_window` already knew this
and hands its restore to `call_after_refresh` (§7.2); `TreeDetailPane
._size_updated` already knew it too, and its comment says so. `reload_messages`
was the third caller with the same problem and no defence.

**Why the plain-message transcript hides it:** every `add_message` calls
`scroll_to_tail` on its way down, and the last of them runs late enough to find
a measured layout. A transcript with tool calls rebuilds its answers through
`_reload_exchange`, whose final height arrives with the `Collapsible` — after
the last scroll anybody performs. The regression test is therefore a transcript
WITH tool calls; the old one is kept, since it is the case that was working.

**The fix is `ChatDisplay._size_updated`** — the hook where `ScrollView` learns
its new virtual size, and so the earliest point at which the answer can be
right. While `_follow_tail`, a growth re-asserts the tail.

`_follow_tail` being stale is the second half of the same defect: it said the
reader was at the bottom while they were at the top, so the next thing to call
`scroll_to_tail` would have yanked them down. Landing where the flag claims
makes the flag true again.

### 8.2 Every collapse stole the view

`Collapsible._watch_collapsed` ends with `call_after_refresh(self
.scroll_visible)` (textual 8.2.7). Every collapse and every expansion scrolls
the container to put that box on screen — and this transcript collapses boxes
without anyone asking: `_close_exchange` folds each finished exchange behind its
summary as a turn ENDS, `_fold_reasoning` folds a thinking region when the answer
starts, and a reload folds one per rebuilt span.

Measured, with a reader parked mid-history:

```
reader parks at y=40  (follow=False)
a box further down expands:   y=40 -> 220
it collapses again:            y=220
```

That is the reported twitchiness, and it is worse than a jump: it fires on the
model's schedule, in the middle of reading, which is the one thing
docs/TUI-STEERING.md's scroll release exists to prevent. It is also what left a
freshly reloaded transcript three rows short of its newest message — the last
box to fold had the last word about the scroll position.

**The fix is `QuietCollapsible`** (`chat_widgets.py`), which the three transcript
box types now subclass: it keeps Textual's two state-keeping lines verbatim and
drops the deferred `scroll_visible`. The transcript decides where the transcript
scrolls; a box in it does not.

**Reacting to the messages instead does not work, and the attempt is worth
recording.** `Collapsed`/`Expanded` are posted from inside the watcher that
schedules the scroll, so a handler can re-assert a position afterwards — but
only if its `call_after_refresh` lands after the box's, and the two are queued on
different widgets. Measured, it lost: the reload settled at 178 of 181, every
time. Three further problems were already visible before that measurement, and
each needed a guard of its own: `Collapsible.__init__` posts an `Expanded` for
the `collapsed=False` it performs before mounting; a tool-free span's exchange is
REMOVED rather than collapsed, and `remove()` is queued, so its stale message
arrives while the box still reports `is_mounted`; and the build's own folds are
indistinguishable from a reader's. Removing the scroll removes all four
questions.

Keyboard focus is unaffected: `Screen.set_focus` brings a widget into view with
`scroll_to_center`, not `scroll_visible` (verified, 8.2.7).

### 8.3 Where a build lands

`_render_window` now marks itself with `_building` and schedules `_finish_build`
one refresh later, which is where a reload that wanted the tail finally asserts
it. Two callers, two answers, one method: `reload_messages` leaves
`_follow_tail` true and lands on the newest message, `move_window` leaves it
false and puts the reader back itself (§7.2). `_size_updated` remains as the
general rule for everything that grows afterwards.

### 8.4 What was NOT the problem

The report proposed debouncing the scroll events. Measured, they do not need it:
one wheel event scrolls 4 rows (`scroll_sensitivity_y` 2.0), a flick's worth of
events at an edge slides the window exactly one turn — `_window_moving` is taken
synchronously in the handler for that reason (§7.3) — and a slide leaves ~60
rows of headroom above the reader, so a continuous gesture does not chain
slides. Nothing here is rate-limited, and after §8.1 and §8.2 nothing needed to
be.

What DOES remain true is that a 46-message conversation of few, long turns
mounts four turns at a time (`RENDER_CAP_TURNS = 4`), so its middle is reached
by sliding rather than by scrolling. §5 already names that number as the first
one to revisit; this section did not change it.

---

## 9. The message cap was the wrong number to leave alone

> **Built (2026-08-31).** `RENDER_CAP_MESSAGES` 50 → 100. Reported as: *"there's
> no way to load the next 4 messages … scroll input only sends me to the first
> block or the last block"*, and then, from a screenshot: *"what I see is only
> one user turn. Shouldn't it be 2 user, 2 assistant?"*

§5 named `RENDER_CAP_TURNS` as the number to revisit first. That was wrong. In a
tool-heavy conversation the MESSAGE bound is the one that binds, and the turn
bound never gets a chance to matter.

### 9.1 What the reader was actually looking at

Every legal window of the reported session, 205 messages with 7 user turns:

```
start=  0  mounts 50 msgs / 1 turn    ⋯0 earlier    ⋯155 later
start=  1  mounts 49 msgs / 1 turn    ⋯0 earlier    ⋯155 later
start= 50  mounts 47 msgs / 4 turns   ⋯49 earlier   ⋯108 later
start= 81  mounts 16 msgs / 3 turns   ⋯80 earlier   ⋯108 later
start= 93  mounts  4 msgs / 2 turns   ⋯92 earlier   ⋯108 later
start= 95  mounts  2 msgs / 1 turn    ⋯94 earlier   ⋯108 later
start= 97  mounts 88 msgs / 1 turn    ⋯96 earlier   ⋯20 later   ← the screenshot
start=185  mounts 20 msgs / 1 turn    ⋯184 earlier  ⋯0 later
```

One turn there is **88 messages** — one user message, 48 tool calls with their
results, then the answer. `window_end` mounts an over-long turn whole rather than
cutting it (§7.1), so at cap 50 that window is 1.8× the bound and holds nothing
else: the four-turn allowance was unreachable.

Two consequences the reader met directly. A window can hold **one** turn where
they expected four — that is the message bound, not a bug in the turn bound. And
stepping back from the tail walks through windows of 20, 88, 2 and 4 messages,
with the `later` count jumping 20 → 108 as the big turn leaves: positions that
show almost nothing while claiming a hundred hidden below.

### 9.2 Why 100

Measured on two real sessions (205 and 283 messages) — reload the tail window,
then take one step back:

| cap | smallest window | tail widgets | reload | one step |
|---|---|---|---|---|
| 50 | 2 messages | 212 | 634 ms | 2343 ms |
| **100** | 16 messages | 212 | 699 ms | 2271 ms |
| 150 | 16 messages | 1179 | 3604 ms | 3436 ms |

100 is free; 150 is a cliff, and not a gradual one. At 150 `render_cap_start`
moves back past the 88-message turn, so the window a session OPENS in swallows
it: 5.6× the widgets and 5× the time, paid on every resume. 100 keeps the tail
window where it was and buys the neighbouring positions — the 2- and 4-message
windows become 90 and 92, and a step back from the tail lands somewhere worth
reading.

### 9.3 What this does not fix

**Steps are still turn-sized.** The fold rows count messages and the movement
counts turns, so the numbers still jump — 20 later to 108 later in one step,
because a step can cross an 88-message turn. Making a window start inside a long
turn was the alternative considered and not taken: it would give uniform,
windowful-sized steps at the price of opening a window on an answer whose
question is above the fold. Raising the cap was chosen instead, so this stands.

**A step still costs ~2.3 s on these windows**, against the 433 ms §7.6 measured
on four short turns. The difference is the content, not the mechanism: the step
is rebuilding 88 messages with 48 tool boxes. §7.6's incremental slide is the
answer if that becomes worth building.
