# Message timestamps

**Built 2026-09-06.** A conversation now records when each of its events
happened, in one clock, readable identically by the TUI, the CLI, an extension
and an RPC client, live and after a reload. It came out of
`docs/PROMPT-CACHING.md`: deciding whether a prompt-cache entry had expired
needs the gap between two LLM calls, and that gap was not recoverable from a
saved session.

## 1. What was wrong

Nothing failed to *measure* time. Three of the four clocks were already right:

| Clock | Set at | State before |
|---|---|---|
| User send | `agent_session.py:2745`, when `submit` builds the message | Accurate, epoch ms |
| Tool result | `agent_loop.py:1252`, when results are collected | Accurate, epoch ms |
| Every `AgentEvent` | `agent_loop.py:264`, `:413`, `:633` | Accurate, epoch ms |
| Assistant message | `openai.py`'s two builders | **`0`** |

Two things then destroyed the record at the persistence boundary.

**The assistant message's own field was fabricated, and differently per
provider** — `0` on `openai-completions`, epoch ms on `anthropic-messages`,
epoch *seconds* on `google-generative-ai`. A reader could not know which
convention it held without knowing which provider wrote it.

**The entry timestamp was the write time, not the event time.** A turn's
messages are persisted in one pass after `loop.run` returns
(`agent_session.py:2818` → `_persist_loop_messages`), so `append_at` stamped
`_now_iso()` once for the whole turn. Measured on a real 685-entry session: a
user message and four completions all carried `2026-08-29T04:23:57.149Z`–`.150Z`.

The result was that a CLI client watching `--mode json` had accurate
per-completion times, and the same session reloaded had none. The information
existed only while the process that produced it was still running.

## 2. `None` is unknown; `0` is legacy data

`AssistantMessage.timestamp` is `int | None`. `None` means no clock applies —
a synthetic message an extension built, a system prompt. It is never `0`.

τ did not run in 1970, so a `0` in stored data is the fabricated value and
nothing else. Every session store maps it to `None` in
`normalize_loaded_entries` (`session_log.py`), called from `Session.load` and
`JmftsSessionLog.load`. **That is the only place in τ that interprets a zero.**
No consumer tests for it, and no writer produces one.

## 3. One clock for an exchange's duration

The TUI used to time an exchange from when its widget opened, and show nothing
for a reloaded one (`chat_widgets.py:1449`: "wall-clock duration is not
persisted"). Both paths now read the agent loop's clock:

- **Live** — `TurnStream` widens `first_event_ms`/`last_event_ms` as events
  arrive and reports `elapsed_seconds` on the `lane_end` event. Per lane, so a
  fork or a bus submission is timed like any other exchange.
- **Reloaded** — `transcript.span_seconds` takes first-to-last over the span's
  message timestamps.

Both read timestamps the agent loop set, so they agree by construction rather
than by two clocks happening to be close. `None` still means unknown and the
summary still omits the duration, which is what a pre-fix session now shows.

## 4. Parity across the four consumers

The requirement is that no head has data another lacks, and that reloading or
switching heads does not change what is readable.

| Consumer | How it reads the clock |
|---|---|
| TUI | `TurnStream.elapsed_seconds` live, `span_seconds` on reload — §3 |
| CLI (`--mode json`) | `AgentEvent.timestamp` live; the message's own field in anything it persists or replays |
| Extensions | Message dicts from `prompt()`, `ctx.entries()`, and the per-completion `turn_end` hook |
| RPC | `get_messages` returns `session.messages` wholesale, and `timestamp`/`usage` are now **declared** in the capability's return schema |

The RPC row was the subtle one. The transport already carried both fields, but
`_GET_MESSAGES_RETURNS` declared only `role` and `content`, so the generated
`docs/RPC-PROTOCOL.md` never named them — a second implementation built from
that document could not know they existed, which is `docs/REMOTE-CONTROL.md` G1
failing quietly.

**Three stores, one stamp.** `event_iso` is called from every `append_at`:
`InMemorySessionLog`, the file `Session`, and `JmftsSessionLog`. A store that
stamped its own write time would make one session read differently depending on
which store wrote it, which is the same defect as §1 with a wider blast radius.
`test_timestamps_survive_write_reload_fork_and_paste` pins write → reload →
fork.

It takes the caller's `now` as a parameter rather than importing one, so each
store keeps its own module-level `_now_iso` as the single substitutable clock.
That is not decoration: `testing.scenes` patches `session_store._now_iso` with a
ticking fake because six sessions written back to back land inside one
millisecond and `list_sessions` then falls through to inode order. Importing the
core's clock silently bypassed that seam and shuffled the sidebar snapshot.

**Print mode stamps its own user message.** `headless.py` appends the user turn
itself, before `submit`, and then skips it when persisting the loop's output —
so it never went through `_persist_turn_inputs` and carried no clock. The TUI
recorded the user's send time and the CLI did not, which is the parity break
this section is about, found by running `tau -p` and reading the file back.

## 5. What this enables

Every LLM call is now bracketed by two accurate timestamps with no new field:
call 1 of a turn starts at the user message's send, call *K* starts when tool
result *K−1* was collected, and each call ends at its assistant message. So the
start-to-start gap that decides whether a 5-minute prompt-cache entry survived
is `user_ts(N+1) − last_toolresult_ts(N)`, computable from a saved session by
any of the four consumers.

`docs/PROMPT-CACHING.md` §7 describes the notice this makes possible.

## 6. What is NOT done

**The entry timestamp changed meaning**, from "when the log wrote this entry" to
"when the event happened". It is the sibling sort key in
`ConversationTree.children_of` and `tree`, which compare the ISO strings; the
format is unchanged so old and new logs still sort together, and within a turn
the order goes from all-equal (relying on a stable sort over append order) to
genuinely ascending in that same order. An entry with no event of its own — a
navigate, a compaction, a system message — still takes the write time.

**No backfill.** A session written before this reads `None` for every assistant
message and shows no durations. Nothing reconstructs them; the tool-result
timestamps in those files are real and are the only clock such a session has.

**No per-completion start time.** §5 is why one is not needed. If a future
change makes a call's start unrecoverable from its neighbours — a provider that
batches, or a loop that reorders — this is the assumption that breaks first.
