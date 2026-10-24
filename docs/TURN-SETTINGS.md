# A request is sent with the settings in force when it starts

Built 2026-10-15. The agent loop reads model, key, reasoning and temperature
before every request rather than once per loop, and the session records a change
in the tree ahead of the request it governs. An abort now says who asked for it:
after an extension abort the queued followUps run, and after a user abort they
are dropped and announced. `ctx.set_model` is recorded under the config name
that `--continue` resumes on.

## 1. What was wrong

Three defects, each found by a failing fixture written for a model-routing
extension (`turn_end` picks the next request's model).

- **A switch made inside a loop did not reach the loop's next request.** A
  `turn_end` hook calling `ctx.set_model("cheap")` in a two-request tool loop
  produced provider calls `['m', 'm']`; wanted `['m', 'cheap']`. `AgentLoop` read
  `self._model`, `config.api_key` and `config.reasoning` fixed at construction;
  `AgentSession.set_model` changed only `self._model`, which the *next*
  `_run_one_turn` read. There was no runtime reasoning setter at all.
- **A followUp queued before `ctx.abort()` never ran.** `_end_of_prompt_drain`
  re-entered `_run_one_turn` with the cursor's `abort_signal`, already tripped,
  and the only reset was per submission. The loop broke before its first request,
  so the followUp text was written to the tree as a user message with no reply.
  A user's Ctrl-C did the same to whatever an extension had queued.
- **`ctx.set_model` was not recorded in a form `--continue` reads.**
  `_frame_config` recorded `model_spec{id, provider, context_window}`; resume reads
  the config *name* `model` (`ConversationSession.model`, `headless.select_session`,
  the serve daemon's `log.config`). Only `TauBackend.set_model`, the RPC
  `set_model` verb and a headless resume wrote `model=<name>`, so a switch made by
  an extension resumed on the old model.

The first is the root cause of a fourth reading: a model switch was the one piece
of config whose effect and record were a turn apart.

## 2. Settings are read per request

`AgentLoop` takes a `turn_settings` source, an async callable returning a
`TurnSettings(model, api_key, reasoning, temperature)`, and awaits it in
`_stream_response` immediately before `stream_simple`. The session supplies
`AgentSession._request_settings`, which reads `_turn_model` / `_turn_api_key` /
`_turn_reasoning` — the acting cursor's frame, else the session's. So anything
that changes those between two requests (a `turn_end` hook is the place) changes
the second request. Temperature follows the model, as it did per turn.

A loop built without a source — the SDK case — sends every request with its
construction `model` and `AgentLoopConfig` values, so standalone use is unchanged.

**Alternative rejected:** rebuilding the loop per request. The loop owns the
steer queue, the repeat-call detector and the turn index, and a rebuilt loop would
have to be handed all three back.

`AgentSession.set_reasoning(level)` / `ctx.set_reasoning(level)` join
`set_model`. A level is one of `EXTENDED_THINKING_LEVELS` (`off` … `xhigh`), and
anything else raises `ValueError`; the provider clamps to what the model supports,
as it always has for the constructor's `reasoning`. `None` (send no reasoning
option) stays a construction-time choice, because on a non-head cursor `None`
already means "the session's". A non-head cursor's level lives on its
`TurnFrame.reasoning`, as its model does.

The prompt cache is never busted by this: a request's settings change only when
something called `set_model` / `set_reasoning`, and an unchanged pair sends a
byte-identical request (pinned by `test_turn_settings.py`).

## 3. Config entries may sit mid-loop

Tree-as-truth says config takes effect where it is recorded (`CURSORS.md` §5).
A switch that takes effect at request two must therefore be recorded before
request two, which is now inside a loop: the path reads
`config · user · assistant · toolResult · config · assistant`.

`_request_settings` calls `_record_config(_REQUEST_KEYS)` first, comparing only
`model`, `model_spec` and `thinking` — the keys a request reads. The rest of the
frame (tools, system prompt digest, extensions, cwd) is fixed for the loop's
life, so recording a change to one of those mid-loop would claim a change the
running loop does not make; it is recorded at the next turn as before.

The write is safe under the single-writer model because `_stream_response` reads
the settings before `writer.open`: the previous assistant message is finalized and
every tool result appended, and no entry is incomplete. A turn with
`store_history=False` reads the settings and records nothing.

## 4. An abort carries a reason

`AbortSignal.abort(reason="user" | "extension")` and `AbortSignal.reason` (`None`
until aborted). The default is `"user"`, so every caller that predates the
distinction keeps meaning "someone asked this to stop". A later `"user"`
overrides an earlier `"extension"`; the reverse changes nothing, because a person
saying stop outranks an extension's routing decision. An unknown reason raises
before the signal trips.

Who sets which:

- `ctx.abort()` → `"extension"`.
- `AgentSession.abort()` → `"user"`. Its callers are the TUI's Esc
  (`TauApp` → `TauBackend.abort`), the REPL's Ctrl-C, the RPC `abort` verb, a
  head swap (`agent_session_runtime`), and a comparison's teardown.
- A `rollback` submission aborting the turn it replaces → `"user"`.
- Tool-internal signals (`extensions_builtin/background.py`) are not turn signals
  and keep the default.

### 4.1 An abort cancels the pending read (built 2026-10-18, #36 #37)

Measured by tau-code against 87f2845:
- The mock server sent one chunk, then went silent for 4 s.
- `abort` answered `"aborted"` at +1.0 s.
- `message_end` arrived only at +4.0 s, and `agent_end` said `end_reason: "done"`.

Every provider polled `is_aborted()` once per chunk, so an abort waited for the
server's next byte. That wait is unbounded when the model thinks silently or the
connection stalls.

`AbortSignal.wait()` is awaitable, and `abort()` resolves it from any thread.

Each provider now runs its blocking read through one of two helpers in
`tau_llm.abort`:
- `iterate_until_aborted` for a stream: the OpenAI SSE lines, the Anthropic
  event stream, the Google chunk stream.
- `unless_aborted` for a single call: the OpenAI buffered POST, and opening the
  Google stream.

Both helpers cancel the read when the signal trips. The provider then
finalizes `stop_reason="aborted"` with whatever had streamed. The retry
backoff waits on `wait()` instead of polling every 0.1 s.

`end_reason` was the second half of the defect. The loop set `"aborted"` only
in its turn-top check. A completion cut short mid-stream returned with no tool
calls and left through the text-only exit, which still said `"done"`. Every
exit that ends a turn now reads the signal. A cut-off read from the wire
looked like a finished answer to all three heads.

Not covered: entering the Anthropic SDK's `messages.stream()` context, which
waits for response headers, is still not raced against the signal.

## 5. After a user abort, queued followUps are dropped and announced

`_end_of_prompt_drain` checks the cursor's signal before each followUp:

- **not aborted** — run it, as before;
- **`"extension"`** — give the cursor a fresh `AbortSignal` and run it. An
  extension that aborts has decided the current loop is over, and the followUp it
  queued is how it says what should run next; this is the routing pattern the
  fixture came from;
- **`"user"`** — pop every queued followUp, write nothing, and emit
  `follow_ups_dropped` with `dropped: list[str]` (the texts), stamped with the
  cursor and submission like any turn event.

**Alternative rejected:** running followUps after a user abort too. The user's
stop would then be undone by text they never typed. Silently clearing the queue
was the other option, and it is the drop with no record that the event exists to
prevent: the text was never written anywhere, so the event is its only trace.

The event reaches all three heads: `TurnStream` turns it into a
`follow_ups_dropped` render event worded by the pure
`tau_agent_core.follow_ups.follow_ups_dropped_notice`; the TUI shows a warning
toast, the REPL a system line, `tau -p` a stderr line (in both modes), and
`--mode json`, RPC and `tau serve` carry the event itself. The wire field
`dropped` carries the full texts, an exception to `WireEvent`'s "nothing
unbounded is pushed" rule (G3): every excluded field there is pullable, and these
texts are not — they were never written to the tree. It rides the unreleased RPC
2.2 / serve 0.9 bump (`TAU-SERVE.md` §5).

### 5.1 A user abort also refuses waiting `enqueue` submissions (built 2026-10-18)

Found by tau-code against 87f2845:
- Two prompts were submitted with `multitask_strategy: "enqueue"` while a turn ran.
- An `abort` stopped the turn.
- Both prompts then ran, one after the other, and no `follow_ups_dropped` came.

So Stop stopped one turn, and the queue the user saw kept going.

The cause: an enqueue submission is not in any queue. Its `submit()` waits on
`cursor.turn_lock`, so the abort could not reach it.

What happens now:
- Each waiting enqueue registers an `EnqueuedWait` on `cursor.enqueued`.
- `AgentSession.abort()` marks every wait on the target cursor dropped, and on
  each cursor it owns. It copies each wait's text to `cursor.dropped_enqueued`.
- A dropped submission still takes the lock in FIFO order, releases it at once,
  and returns `accepted=False`. Its `rejection_reason` is
  `"dropped by a user abort while waiting for the turn"`.
- The texts are announced in the same `follow_ups_dropped` event as the dropped
  followUps: followUps first, then enqueues in submission order.
- The aborted turn's drain announces them while it still holds the lock. If no
  turn drain ran (the lock holder was not a turn, or the turn raised), the
  first refused wait announces them.
- An enqueue submitted after the abort runs normally.

An `"extension"` abort leaves waiting enqueues alone, as it leaves followUps to
run. A `rollback` submission's own abort does not drop them either. Rollback's
FIFO rule ("a rollback does not jump an already-queued enqueue") is unchanged.

**Alternative rejected:** an `abort` parameter that opts into the drop. It would
make a Stop that does not stop the default, and every head would have to pass
the flag to get the behaviour the user asked for.

## 6. The model's config name is recorded

`AgentSession` remembers the name `set_model` resolved (`_model_name`; on a
non-head cursor `TurnFrame.model_name`), and `_frame_config` emits `model` when a
name is known. `_record_config` then writes it with `model_spec`, so after
`ctx.set_model("cheap")` and the next request,
`config_at(cursor.entries(), cursor.leaf)["model"] == "cheap"` and `--continue`
resumes on it.

`model` is emitted only once a name is known. A session built from a `Model`
object knows no name, and emitting `None` there would overwrite the name a head
recorded in the session header. Core records `model` only; `backend` stays with
the coding-agent sites that know it. Those sites (`TauBackend.record_model_change`,
the RPC `set_model` verb, headless resume) are kept: each writes immediately and
writes `backend`, and the core's record lands only at the next request, so none
is redundant.

## 7. What is absent

- **A `before_request` hook.** `turn_end` already runs between two requests of
  one loop, which is the only point a per-request decision can be made without
  racing the stream; a second hook at the same point would be two names for it.
- **`ctx.restart_turn()`.** Re-issuing the request just made under new settings
  is `ctx.abort()` plus a followUp, which §5 now makes work. A restart primitive
  would also have to decide what happens to the partial assistant message.
- **An RPC verb for reasoning.** `cycle_thinking_level` stays declined. The
  `set_reasoning` verb, and `/thinking` in the TUI and the REPL, came later
  (`docs/REASONING-WIRE.md` §10).
- **A `model` name for a comparison column or a `spawn(model=Model)`.** Both are
  handed `Model` objects; their paths inherit the head's `model` name as before.
- **Dropping queued `nextTurn` messages on a user abort.** They belong to the next
  submission, which the user starts, not to the one that was stopped.

## 8. A change records its source and tells extensions (built 2026-10-24, #25)

Before this, `set_model` wrote nothing, the entry the next request wrote carried
no source, and no extension heard about it. tau-router guessed at a hand-picked
`/model` by comparing `ctx.get_model()` with the last model it had set itself, so
a resume, or another extension's `ctx.set_model`, was misattributed. Its pin
rule needs the opposite: `-m` and `/model` pin, `default_model` never does,
including `-m X` where X is also the default.

### 8.1 The vocabulary

One string vocabulary for both settings, `tau_agent_core.setting_sources`:

| Source | Means | Comes from |
|---|---|---|
| `default` | config, or the built-in default | no `-m`/`--thinking`; `create_agent_session()` with no `model`; a TUI new chat on `default_model` |
| `cli` | a flag, or the SDK constructor argument | `-m`/`--model`, `--thinking`, a `:level` suffix on `-m`; `AgentSession(model=…)` |
| `command` | a head command | `/model`, `/thinking` in the TUI and the REPL (`TauBackend.set_model` / `set_reasoning`) |
| `rpc` | an RPC or serve verb | `set_model`, `set_reasoning`; `tau serve`'s `new_session` with a `model` |
| `extension:<name>` | `ctx.set_model` / `ctx.set_reasoning` | the extension whose hook, command or tool is running |
| `resume` | restored, origin not recorded | a resume from an entry written before #25 |
| `spawn` | the model a cursor was opened with | `spawn(model=…)`, and each `compare` column |

`spawn` is the one value added to the brief's list: a sub-agent's or comparison
column's model is set when its cursor opens, by no setter. There is no `unknown`.

Whether a flag counts is decided by whether it was given, never by comparing
values, so `-m X` is `cli` when X is `default_model`
(`headless.setting_sources_for`, which every head's `resolve_model_config` call
goes through).

The setters take a required keyword: `AgentSession.set_model(name, *, source)`
and `set_reasoning(level, *, source)`. A setter refuses `resume`, because a
setter call is a change. `ctx.set_model` names its extension from
`ACTING_EXTENSION`, a context variable the runner sets around every hook handler,
the extension api sets around its notify handlers, and the session sets around
an extension command or tool. Called anywhere else (a thread with no context,
module load), it raises `RuntimeError` rather than guess.

The constructor keeps defaults, because `AgentSession(model=…)` is the
argument given explicitly: `model_source=None` is `cli`, and
`reasoning_source=None` is `cli` when `reasoning` is given and `default` when it
is not. `model_restored` / `reasoning_restored` (bools, default `False`) say a
resume restored the value; a restored setting without its source raises.
`TauBackend` passes the model config's four keys through (§8.5).
`create_agent_session(model=None)` now means `"gpt-4o"` with source
`default`. It previously defaulted to the string, which made the two cases
indistinguishable.

### 8.2 What is recorded

`model_source` and `thinking_source` are config keys. A value write carries its
source in the same entry. A source change with an unchanged value is written on
its own, so `/model X` while on X still pins after a resume. A restored value
never writes its source again.

`AgentSession.record_settings(**extra)` writes the request keys now, with
`extra` beside them. `TauBackend.record_model_change` and the RPC `set_model`
verb write through it instead of `append_config`, so the immediate entry a
`--continue` reads carries the source.

### 8.3 Reading it

`ctx.get_setting_source("model" | "thinking")`, backed by
`AgentSession.get_setting_source`, returns a `SettingSource(source, restored)`
for the acting cursor. It is readable at `session_start`, at `input`, and
wherever a ctx exists. A frame that sets its own model or level carries its own
source on `TurnFrame.model_source` / `reasoning_source`, required beside the
value.

**On resume the recorded source survives** (tau-router's follow-up request): a
resumed `/model` pick reads `SettingSource("command", restored=True)`, not
`resume`. A head reads it off the resumed config with `restored_keys(recorded,
setting)`, which gives `{"model_source": "command", "model_restored": true}`.
An entry with no source gives source `resume`, restored. A flag
given on resume replaces only its own setting (`-m Y` gives `cli`, and the
thinking level stays restored). The heads that resume are `-p`, the REPL, the
TUI's session picker and `tau serve` loading a session. A serve fork restores
the source's record too. A session `tau serve` has just created takes the
`new_session` sources rather than reading as restored.

### 8.4 The event

`turn_setting_changed`, runner-routed, with `{type, cursor_id, setting, old,
new, source}`. `old` and `new` are config names for the model, or `Model.name`
when the session knows no config name, and levels for thinking.

It is **notify-only**. It fires once the change is in the tree, so a veto
would have to un-write an entry, and the setter has already returned to its
caller. An extension that wants a different model calls `ctx.set_model`, and
that is a change of its own. The return value is ignored, and a raising handler
is surfaced and does not stop the next one, as with `entry_written`. It respects
a frame's `hooks=False`. Runner events are not mirrored on the RPC wire, so the
event list and the protocol docs are unchanged.

When it fires:
- Only a setter call is a change. The setting a session was built with is never
  announced, nor is a value restored on resume, even on its first record. An
  extension reads that setting with `ctx.get_setting_source` at
  `session_start`.
- It fires when the setter's change is recorded: at the next request, or at
  once through `record_settings` (`/model`, the RPC verb). Several calls before
  one record report the first `old` and the final value. A call that changed
  neither value nor source writes nothing and fires nothing.
- A non-head cursor without a frame records the session's change on its own
  path. The event fires on the head's record, where the change was made.

**Alternative rejected:** making it an `AgentEvent` on the notify bus. That
would reach RPC and `--mode json` for free, but `AgentEvent.source` is already
the submission's origin. A second meaning on the same field, or a renamed one,
costs more than a runner event's lack of wire presence, which no consumer has
asked for.

**Not done:** a TUI attached to `tau serve` (`--connect`) sends `/model` as the
RPC verb, so it records `rpc`, not `command`.

### 8.5 Config-path values are JSON

Every dict on the config path holds JSON values only: a head's model config
(`resolve_model_config`, `TauBackend`'s config, `tau serve`'s `new_session`
sources), the folded config (`config_at`), the `extra` keys of
`record_settings`, the tree's config entries, and RPC and serve payloads. A
model config starts as `config.json` and stays plain JSON.

So a source is a string under `model_source` / `thinking_source`, and whether
a resume restored it is a bool under `model_restored` / `thinking_restored`.
`source_keys(setting, source, restored=)` and `restored_keys(recorded,
setting)` in `tau_agent_core.setting_sources` build both keys, and check the
source against the vocabulary. The `*_restored` keys live only in a head's
model config and the session's own state. The tree never records them,
because a restore is a fact about one process opening the session, not about
the conversation.

`SettingSource` is the extension-facing view, built only where an extension
reads it: `ctx.get_setting_source` and `AgentSession.get_setting_source`. The
`turn_setting_changed` event carries the source as a plain string, because a
setter change is never restored.
