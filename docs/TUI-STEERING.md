# Steering: typing while the model is generating

Status: **built, 2026-08-29.**

Before this, a turn locked the TUI. `on_input_submitted` set
`input_widget.disabled = True` for the duration, and every renderer in
`ChatDisplay` called `scroll_end()` on every delta. So while the agent worked
you could neither say anything to it nor read what it had already said — a
ninety-second turn snapped the view back to the bottom on each token, and
scrolling up to re-read a tool result was impossible until it finished.

The core has had the delivery mechanism since docs/SUBMISSION-LIFECYCLE.md phase
4: `multitask_strategy="steer"` puts content on `_pending_steer_messages`, and
the running `AgentLoop` drains it immediately before its next call to the model
(`agent_loop.py:_deliver_steer`). Nothing in the TUI could reach it. This
document is the frontend half.

## 1. The two locks, and why they were there

**The scroll lock.** `MessageList` now keeps `_follow_tail` and every renderer
calls `scroll_to_tail()` instead of `scroll_end()`. `watch_scroll_y` — Textual's
own watcher, extended — recomputes the flag as `is_vertical_scroll_end` on every
vertical scroll from any cause. So scrolling up detaches the view, scrolling back
to the bottom re-attaches it, and there is no gesture to learn.

The flag is maintained on the READER's movement rather than sampled when content
arrives, and that is the whole subtlety. Content arriving while detached grows
`max_scroll_y` without moving `scroll_y`; a check at the moment of arrival would
answer "not at the bottom" forever after the first delta, and a check after the
mount would answer "at the bottom" every time.

Textual ships `Widget.anchor()`, which is almost exactly this. It was not used:
its release is wired into `scroll_to`'s `release_anchor` argument rather than
into the scroll position, so this class's own `scroll_end` calls would release
the anchor on the first delta.

`reload_messages` re-attaches the tail explicitly. A reload REPLACES the
transcript — resume, compact, rollback — so a scroll position taken in the old
document does not describe a place in the new one.

**The input lock.** It is gone. `is_generating` still gates Esc-to-cancel and
Ctrl-Z-to-rollback; it no longer gates the editor.

One thing that cost nothing before now costs something. `ctrl+z` is bound to
Rollback while a turn is generating, and it used to steal the key from an editor
that was disabled, so its undo was unreachable anyway. The editor works now, and
the two uses genuinely contend. Rollback still wins: the Footer says
`^z Rollback` for exactly as long as the binding is live, and a key doing what
the Footer promises beats an unadvertised undo. So there is no undo in the editor
during a turn.

## 2. Where a mid-turn line goes

`on_input_submitted` does not submit while `is_generating`. The text goes into
`Parley._pending_steer`, and `_flush_pending_steer` submits it later.

**The buffer is the app's, not the core's.** Once `submit()` has taken content,
no frontend can get it back, and the reclaim gesture in §4 is the reason to keep
it this side of that door. pi holds it the other way — `session.steer()` queues
into the session immediately and `clearQueue()` pulls it back on interrupt
(`interactive-mode.ts`, `restoreQueuedMessagesToEditor`) — which is a coherent design, and would have needed
two new methods on `AgentSession` plus a withdraw that races the loop's drain.
The buffer costs nothing and the reclaim cannot race.

`config.json`'s `steering_strategy` picks the delivery point. Both values are
`MultitaskStrategy` members, spelled as the core spells them, because the setting
IS the field the app puts on the submission:

| value | delivered | as |
|---|---|---|
| `steer` (default) | at the running turn's next tool call | `multitask_strategy="steer"` — the loop takes it before its next call to the model, which is after that turn's tool results |
| `enqueue` | when the app goes idle | an ordinary turn |

`steer` is the default because pi binds Enter to it (`interactive-mode.ts:3129`)
and because the point of typing mid-turn is to change what the agent is doing. A
message that waits for the turn to end is a follow-up, and that was already
reachable.

An unrecognised value RAISES at startup rather than falling back to the default.
The two strategies put the message in a different place at a different time, so a
typo that silently selected the other one would be invisible until a steering
message failed to land where it was aimed.

**Why a tool call is the trigger and Enter is not.** Handing a steer to the core
the moment it is typed looks simpler and strands it. A turn that is writing a
long text answer and then stops makes no further call to the model, so a queued
steer is not delivered — the core's own contract is that it "STAYS queued and is
delivered at the start of the next turn", and if the user never types again
there is no next turn. Waiting for a tool call means the buffer is only handed
over when there is provably another call to the model coming.

**The `steer` strategy delivering at the turn edge is not a fallback.** When a
turn ends without another tool call there is no "before its next call to the
model" left in it, so the message becomes its own turn. The pending widget says
which delivery point it is waiting for, and it never says the message was
delivered when it was not.

**All at once, and only all at once.** The buffer is joined with a blank line and
delivered as ONE message: a second line typed while the first waits joins it.
pi reaches the same place from the other side — it queues each message
separately and `PendingMessageQueue` mode `"all"` drains them together
(`agent.ts:142`) — and also offers `"one-at-a-time"`. τ has no such mode and no
setting for one: `AgentLoop._deliver_steer` drains the whole queue
unconditionally, so a setting promising otherwise would have nothing behind it.

**Commands are refused, not queued.** A line that resolves to a command is left
in the editor with a notice saying it runs between turns. `/compact` and `/fork`
rewrite the very context the running turn is being answered from, and steering
delivers through the same door that dispatches them. The flush submission carries
`expand_commands=False` for the residual case: the `input` hook chain runs on
this text too, and could rewrite prose into a command after the refusal check.

## 3. The pending-input widget

`PendingInput`, between the transcript and the editor. Hidden when the buffer is
empty, like `LaneStrip`, so an ordinary turn costs no rows.

It is the only place a steering message is visible between the Enter that wrote
it and the boundary that delivers it. Without it the input box would accept text
during a turn and appear to swallow it — the transcript cannot show the line yet,
because the model has not been given it yet.

It holds no state. The app owns the buffer, because the buffer has to survive the
gesture that empties the widget and refills the editor.

## 4. Reclaim

Up on an EMPTY editor takes the whole buffer back into the editor and clears it.
With a draft in the box, Up still means history, exactly as before.

Requiring an empty editor is what keeps the two apart without a mode. Pending
input is the newest thing the user wrote, so it sits one step in front of the
history Up otherwise walks; and reclaiming into a box that already had a draft
would have overwritten the draft.

**alt+up does it with a draft in the box**, putting the reclaimed text in front
of the draft. That is pi's binding for this (`app.message.dequeue`,
`keybindings.ts:108`) and pi's ordering (`restoreQueuedMessagesToEditor`): the
pending message was typed first. Bare Up is τ's addition, for the case pi's key
does not need to cover.

`ChatInput` reaches the buffer through a `reclaim_pending` callback the app
installs at mount, rather than through `self.app`, so the widget stays
constructible on its own — which is how most of its tests build it.

**Two events reclaim without being asked**, because they make a pending message
undeliverable rather than merely early:

- **Esc.** The turn the message was aimed at is being cancelled, so delivering it
  would launch a turn the user just stopped. pi does the same — `clearAllQueues()`
  and refill the editor (`interactive-mode.ts`, `restoreQueuedMessagesToEditor`).
- **A session swap** — new chat, clear, resume, model swap, all of which pass
  through `_rebind_after_session_swap`. A line written about one conversation
  must not be delivered into another.

Both put the text back in front of whatever draft is already in the editor rather
than over the top of it, the same way round as alt+up. Dropping it is the other
wrong answer: the user typed it.

## 5. Showing a delivered steering message

`AgentLoop._deliver_steer` appends the message to the running context and to the
log, and brackets it with `message_start`/`message_end`. `TurnStream` consumed
neither, so a steering message reached the model and the session file without
appearing in the transcript at all until the next reload.

`TurnStream._feed_message_start` now emits a `steer_message` render event, and
only for a message whose role is `user`. That is an exact discriminator: the
loop's other two `message_start` emitters are an assistant completion
(`_stream_response`) and a provider error, both `role: "assistant"`, and both
already rendered off the deltas and the `message_end` that follow them.

`ChatDisplay` mounts it as a step INSIDE the open exchange, in arrival order. It
is a user turn that happened inside somebody else's exchange; hoisting it to a
top-level bubble would put it above content that preceded it.

That makes an exchange's steps no longer all the model's, which `_close_exchange`
assumed in two places:

- It promotes `steps[-1]` out below the summary as the final answer. A steer that
  arrives last — the turn then ends on `max_turns` or a provider error — would be
  promoted, rendering the person's own words as the model's reply. The promotable
  step is now the last ASSISTANT step.
- It unwraps a tool-free exchange by removing it, which destroys every box in it
  and keeps only the promoted answer. An exchange holding a steer is never
  unwrapped now, whatever its tool count; the user's line would be the casualty.

## 6. What this does not do

- **No Alt+Enter.** pi binds Enter→steer and Alt+Enter→followUp per submission.
  τ has one setting for the session instead. The strategy is a field on the
  record, so a second keybinding is a one-line change in `_flush_pending_steer`.
  There is now a second reason not to make that change: on a terminal without the
  kitty keyboard protocol, Textual reports Alt+Enter as plain `enter` and the
  modifier is unrecoverable (`docs/ENTER-KEY.md` §3). The binding would work for
  some users and silently do the wrong thing for the rest.
- **No steering an image.** `Submission.images` exists and the editor has no way
  to attach one, mid-turn or otherwise.
- **The reclaim window closes at the tool call**, not at delivery. Between the
  flush and the core's drain the text is unreclaimable, and the widget stops
  showing it at the flush — which is honest about where it is (gone through the
  door) rather than about whether the model has read it.
- **Any lane's tool call triggers the flush**, not only the lane the user is
  talking to. `_on_render_event` sees a forked sub-agent's and a bus
  submission's tool calls too. The delivery is not wrong when that happens — the
  message still goes on the session's steering queue and the main loop still
  drains it before its next call to the model — but it is EARLIER than the
  widget's wording implies, and the reclaim window closes with it. Telling the
  lanes apart needs the label `RenderRouter` computes at `lane_start`, which
  this app keeps only for the strip.
