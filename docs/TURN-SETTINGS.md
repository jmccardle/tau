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
