# RPC Tier B — implementation spec

Companion to `docs/REMOTE-CONTROL.md` §3 ("Tier B — parity, cheap because
`AgentSession` already has the method"). **That sentence is wrong for half the
tier**, and this document is the corrected ground truth every Tier B unit works
from. Read it before touching code; it is the shared premise that keeps six
parallel units from answering the same questions six different ways.

Phases 2–4 shipped blocks [1]–[4] and [6]–[8]. Tier B adds six verbs to block
[3]. Nothing here touches the transport, the dialect, the event stream or the
runtime host.

---

## 1. Ground truth

What actually exists today, verified against the code rather than §3's summary:

| Verb | Backing implementation | Status |
|---|---|---|
| `set_model` | `AgentSession.set_model(name)` (`agent_session.py:785`) | Exists. Swaps `self._model`, returns `get_model()`. **Does not persist anything.** |
| `compact` | `AgentSession.compact(custom_instructions)` | Exists. Returns `CompactionResult \| None`. |
| `get_session_stats` | `AgentSession.get_usage()` | Exists, but `get_state` already returns usage + `message_count` + cursor. Redundant as specified — see D-3. |
| `set_auto_compaction` | — | **No accessor.** `AgentSession._compaction_settings` is a mutable `CompactionSettings(enabled, reserve_tokens, keep_recent_tokens)`. |
| `set_session_name` | `ExtensionContext.set_session_name` / `.get_session_name` (`extension_types.py:2203`) | **Already implemented, on the extension API — not on `AgentSession`.** |
| `get_last_assistant_text` | — | No method. Trivially derived from `AgentSession.messages`. |

### 1.1 Two boundaries that constrain the design

**The `SessionLog` Protocol deliberately omits the appenders.**
`session_log.py:38-48` states it outright: `append_model_change` /
`append_thinking_change` / `append_session_info` are *deliberately absent*
because "`AgentSession` never calls them (the TUI/headless call those on the
concrete `Session` directly), so keeping them off the Protocol avoids an
unused-method contract (Fail-Early)."

Consequence for D-2 and B5: any code that appends one of these entries must
first establish the bound log actually has the method, and **raise if it does
not** — an `InMemorySessionLog` has nowhere durable to put it. Do not add these
methods to the `SessionLog` Protocol. Do not silently skip the append.

`ExtensionContext.set_session_name` is the reference implementation of exactly
this pattern, including the raise. Copy its shape, and its docstring's reasoning
about why a silent no-op was the bug it fixed.

**Corrected by Blocker 2 of the Tier B review — method presence was the wrong
axis.** The paragraph above states the guard as "the bound log must have this
appender, else raise", and that premise produced a bug of exactly the shape it
was written to prevent. The property a durability-promising verb needs is not
*can I call an appender* but **will the entry outlive this process**. Those come
apart precisely where it matters: every real `ConversationSession` has every
appender, persisted or not, so `require_log_appender`'s `hasattr` passes on an
unpersisted session whose `_persist_header`/`_persist_entry` are
`return`-on-`None` no-ops (`session_store.py:585,593`) — which was the session
every RPC run started on. `set_model` and `set_session_name` returned cursors
for entries no replay could ever see. The case the old guard *did* cover,
`InMemorySessionLog`, is unreachable in RPC mode.

So the durability-promising verbs take `commands.require_durable_session` as
well: the bound log must declare a storage location (`_DURABLE_LOCATION_ATTRS`
— the file store's `path`, the JMFTS store's `root_doc_id`) and that location
must be set. Unknown declarations refuse; they are never assumed durable.
`require_log_appender` stays, aimed at what it can actually answer (the SDK's
`InMemorySessionLog`), and is documented as insufficient alone.

Two things this correction does NOT change. Ephemeral sessions still swallow
these appends at the STORE layer — `SessionCatalog`'s contract suite has an
ephemeral session accept `append_session_info("renamed")` and stay unlisted,
which is correct: durability is a property of the store a caller chose, not
something an appender should second-guess. And `create_ephemeral` still means
"no disk, ever" (`test_catalog_create_ephemeral_never_touches_disk`); the fix
was to stop STARTING RPC mode on one (`rpc_mode.py` now calls `create`), never
to redefine ephemerality. The bug was only ever the RPC layer promising what
the session it chose could not keep.

**Auto-compaction is hard-disabled in RPC mode.**
`rpc_mode.py` → `backends.create_backend` → `TauBackend`, which constructs its
session with `compaction_settings=CompactionSettings(enabled=False)`
(`backends.py:885`). So `set_auto_compaction` is not a parity checkbox: it is
the only route to a capability a host currently cannot reach at all.

### 1.2 `CompactionPolicy` is not in scope

`compaction_policy.CompactionPolicy` is the SIM_SPEC_v2 measurement harness
(§16.8 / H5). Its own docstring: "This module changes nothing about how τ
compacts." It is constructed in exactly one place, `sdk.py:865`, and
`rpc_mode.py` does not go through `sdk.py`. **No RPC session ever carries a
policy.** Do not add a policy guard to any Tier B verb — see D-4.

---

## 2. Settled decisions

These are decided. Do not relitigate them inside a unit; if a unit finds a
decision unworkable, stop and report rather than improvising a fifth answer.

### D-1 — Mutating verbs take the turn-safety guard

Phase 3 established the pattern for a verb that mutates session state while a
turn may be in flight: bounded `await session.turn_lock.acquire()`, and on
timeout `RPCError(TURN_STILL_RUNNING, ...)` rather than proceeding. See
`agent_session_runtime.py:473` and `commands.py:1199-1210`.

Every mutating Tier B verb uses it: `set_model`, `compact`,
`set_auto_compaction`, `set_session_name`. B0 extracts it into one shared
helper; the four units call the helper and never hand-roll the wait.

`get_session_stats` and `get_last_assistant_text` are reads and take no guard.

**Amended by Blocker 1 of the Tier B review, for `compact` only:** that verb
now takes the guard *inside its background task* and holds it across the
whole `AgentSession.compact()` call (see D-5). The refusal still reaches the
caller on that call's own response — the D-1 contract is unchanged — but the
work it protects no longer runs on the dispatch path.

Finding 3 of the same review (three pipelined mutators delayed `abort` by
15s, one `DEFAULT_SWAP_TIMEOUT_S` each, because the serial reader parses
nothing while a dispatched line is in flight) is **bounded and still open**.
It is not fixed here: the other three mutators return a value a host expects
synchronously and have no second completion to move it to, so the C3 shape
does not generalize to them without redesigning their result contracts. Do
not "fix" it by shrinking the timeout.

### D-2 — `set_model` persists a `model_change` entry and returns a cursor

Today a host switches models over the wire, the turn runs on the new model, and
a later replay of the session shows no record that it happened. The RPC verb
closes that: after `session.set_model(name)` succeeds, append a `model_change`
entry and return the resulting cursor (E5).

The append happens **in the RPC verb**, not inside `AgentSession.set_model` —
that method is also on the TUI's path and widening it is out of this phase's
scope. Guard per §1.1: if the bound log has no `append_model_change`, raise; do
not skip — **and, since Blocker 2, if the bound session is not durable, raise
before touching anything** (`require_durable_session`). A cursor is a promise;
this verb does not make one it cannot keep.

Record the resulting inconsistency in the verb's `notes`: a TUI model switch
still does not persist. That is a known gap, stated, not hidden.

### D-3 — `get_session_stats` is scoped up, or it does not ship

Against `get_state` the pi-parity shape is redundant. It ships as the verb a
host reads to decide **whether and when to compact**:

- context-token estimate (`estimate_context_tokens(session.messages)`)
- the model's `context_window`, and remaining headroom
- effective `CompactionSettings` — `enabled`, `reserve_tokens`,
  `keep_recent_tokens`
- last-compaction state, if the session exposes one
- `get_usage()` (cumulative token usage), for cost

It must be genuinely more than a re-shaping of `get_state`. In RPC mode it is
also how a host discovers that auto-compaction is off (§1.1).

### D-4 — `set_auto_compaction` is a plain setter

Idempotent. Takes the desired state, returns the effective state. **No policy
guard** — see §1.2; the policy it would protect belongs to an experiment driver
on a code path RPC never takes, and its real protection is the declaration
recorded in `manifest.json`, not a setter.

One thing the verb must document and B4 must test: `_maybe_auto_compact` emits
its own `agent_start`/`agent_end` pair (`agent_session.py:3286-3290`) through
`self._events.emit` directly, **not** `_emit_stamped`. So those two events carry
no submission provenance. A host correlating events to `submission_id` will see
an orphan pair. The `agent_end` does carry a `cursor` (the handler stamps every
`agent_end` at enqueue), so a host that obeys F3 — never cache the tip — stays
correct across a compaction it did not ask for.

### D-5 — `compact` is a dual completion (Blocker 1, Tier B review)

Added after B2 shipped, and it **supersedes B2's "returns the
`CompactionResult` summary + cursor" row below**. Summarization is an
unbounded provider call; `transport._read_stdin` awaits each dispatched line
to completion before parsing the next, so running it inline made every other
verb — `abort` included — unanswerable for its duration (measured: 20s on a
gated fake provider; `get_state` and `abort` were not slow, they were
unparsed). That is the exact availability property phase 3 treated as a
blocker and phase 4 spent three fixes defending.

So `compact` follows C3, the shape `submit`/`prompt` already use
(`commands._submit_and_acknowledge`, `RPCHandler.track_background_task`):

- **Response** — `{accepted: true, compaction_id}`, enqueued the instant the
  D-1 guard is acquired, synchronously, for the ordering reason
  `_submit_and_acknowledge`'s docstring measures.
- **Completion** — a `compaction_end` **notification** carrying
  `compaction_id` + `request_id` (correlation), `is_error`/`error`,
  `performed`, the full `CompactionResult`, and the resulting cursor (E5).
  A distinct method rather than an `event`: `WireEvent.type` is a closed copy
  of `AgentEvent.type`, and a compaction outcome is not an `AgentEvent`.
- **Two at once are impossible** — a second `compact` while one is in flight
  is refused *immediately* with `TURN_STILL_RUNNING` (D-1's vocabulary),
  without waiting out the `turn_lock` the first one holds for as long as the
  provider takes. `RPCHandler.compaction_in_flight` is the flag; the serial
  reader is what makes the synchronous check unraceable.
- **Shutdown** — the background task is registered with
  `track_background_task`, so `run()`'s teardown reaps it exactly as it reaps
  a background turn. A cancelled compaction says so on stderr (T4) and emits
  no notification: nothing was generated and nothing was written, and an
  `is_error` notification for it would be indistinguishable on the wire from
  a real `CompactionError`.
  **Narrowed by finding 5 (see D-8): "a cancelled compaction emits no
  `compaction_end`" now means a compaction cancelled BY SHUTDOWN.** A
  compaction cancelled by a host's own `abort` reports `cancelled: true` on
  the wire — the indistinguishability argument above was about an
  unsolicited outcome sharing `is_error`'s spelling, and a dedicated
  `cancelled` field for a cancellation the host asked for shares nothing
  with either.

**Amended by finding 3 of the Tier B review — "reaped" had a hole, and the
bullet above named the wrong reason for it.** `run()`'s `finally` reaped the
background tasks *after* draining and exiting the writer, and
`_cancel_background_tasks`'s phase 1 waits `_BACKGROUND_TASK_GRACE_S` without
cancelling. A compaction finishing inside that window therefore called
`put_nowait` on a queue nobody would ever dequeue again. Measured, matrix over
provider delay and stdin-close time: `rc=0`, empty stderr, no `compaction_end`
— and a `compaction` entry durably in the session log, so the next process to
resume that session found a compaction it was never told about. Neighbouring
timings delivered correctly, so it was a race, not a constant. That is the
class this repo refuses to ship, and D-5's own claim that the only silent
outcome is cancellation was simply false.

Four changes, ordered by preference — deliver if at all possible, report out of
band only when it is not:

- **`run()` reaps background tasks BEFORE draining the writer** (handler.py,
  after the reader ends), so everything they enqueue while winding down is
  still ahead of `_flush_stdout_with_deadline` and goes out under P4. This
  costs nothing: the same grace was already being paid, just later.
  `_shutting_down` moves with it, so Blocker 1 (phase 4)'s "flag first, then
  reap" ordering is intact. The `finally`'s reap stays as the catch-all for
  the paths that skip this one (writer-ended-first, any exception on the way).
  **The grace period is not shortened or removed** — it is the mechanism that
  lets a credit-starved turn wind down by itself.
- **`_write_stdout` stops throwing away a queued item on its way out**
  (transport.py — the one file outside this finding's own list that had to
  change, because the defect's second half lives there and nowhere else).
  Its `except asyncio.TimeoutError: if not self._running: break` contradicted
  its own loop condition, which writes while "`_running`, OR the queue is
  non-empty". The timeout proves the queue was empty for the last 0.5s, not
  that it is empty now — and a `put_nowait` landing in the same event-loop
  iteration as the timer loses that race: the getter it wakes is cancelled,
  the item stays queued, and the handler runs anyway. Reaping before the
  drain is what made it reachable, and measured: at a completion offset of
  exactly 0.5s into the reap, three runs out of three lost both the
  compaction's `agent_end` and its `compaction_end`. The break now also
  requires the queue to be empty, i.e. it agrees with the `while` above it.
  Verified by sweep afterwards — offsets 0.10/0.25/0.50/0.60/0.75/0.90 all
  deliver, 1.10/2.00 are past the grace and take the cancellation path.
- **A completion that genuinely cannot be delivered goes to stderr (T4),
  never nowhere.** `RPCHandler.output_is_deliverable` is False once the
  writer task has finished — broken pipe (T6), or SIGTERM cancelling it
  outright (P1) — and `_complete` prints the whole payload rather than
  enqueueing it. `True` when no writer was ever started (every white-box
  test, any embedded caller): "not yet started" and "already gone" are
  different answers and the predicate must not conflate them.
- **SIGTERM says what it discarded.** P1's "SIGTERM skips the flush" is
  unchanged; what changes is that cancelling the writer with items still
  queued now prints a T4 line naming the count. Every other truncation route
  already announced itself (the flush deadline prints, a broken pipe
  propagates out of `run()`); this was the one with no report at all.

So `compact` now reports its outcome exactly one of three ways and never none
of them: `compaction_end` on the wire, the full outcome on stderr, or the
cancellation line on stderr.

Known cost, accepted: the capability document publishes a `params_schema`
and a `result_schema` per verb and generates its event half from
`AgentEvent`, so it has nowhere to publish a notification's payload.
`compaction_end`'s field list therefore lives in the verb's `notes` and in
`commands.COMPACTION_END_PARAMS_SCHEMA` (import-time-checked by
`_assert_supported_schema`, like every table schema) rather than on the wire.
Widening the capability document to carry notification schemas is a real,
unclaimed gap.

### D-6 — RPC mode starts on a PERSISTED session (Blocker 2, Tier B review)

`rpc_mode.run_rpc` binds the startup session with `session_catalog.create`,
not `create_ephemeral`, and `new_session` takes a real `persist` param
defaulting to `true` (it hardcoded `false`). Both are the same defect: a verb
promising durability on a session that has none.

- **Fresh, not unpersisted.** This module's old convention read "every run
  starts a fresh, unpersisted conversation". Freshness is the load-bearing
  half — a host must not inherit somebody else's transcript. `create` is
  equally fresh; it additionally has a location, so the session is listable,
  resumable, and able to KEEP what D-2's append writes.
- **Startup CLI restrictions are untouched.** `--session`/`--continue`/
  `--fork`/`--resume`/`--name`/`--store` are still rejected for `--mode rpc`
  in `cli.py`. That enforcement is separate and stays exactly as it is.
- **The wire default lives at the wire.** `AgentSessionRuntime.new_session`
  keeps `persist` required-not-defaulted ("the caller states what it wants
  rather than inheriting a guess" — it also serves the TUI). The RPC handler
  states the default and passes an explicit value down; at the wire a default
  is a published part of the contract, not a guess.
- **Unpersisted is still reachable, and now honest.** `new_session
  {"persist": false}` gives an in-memory conversation; `set_model` and
  `set_session_name` then REFUSE on it (§1.1 as corrected) rather than
  returning a cursor for a write that lands nowhere.
- **`create_ephemeral` is untouched.** Making "ephemeral" mean a file under
  `/tmp` was considered and rejected: it is a `SessionCatalog` Protocol
  method with three implementations, a contract suite asserting it never
  touches disk, and it is what `tau -p --no-session` calls.

**Amended: `--mode rpc` honors `--no-session` too.** Blocker 2 changed the
DEFAULT from ephemeral to persisted; the sentence above ("it is what `tau -p
--no-session` calls") was accurate and was also the whole problem — `-p`
honored the flag and `--mode rpc` did not, while `cli.py` accepted it in both.
A host that asked for an unpersisted run got a persisted one with no
diagnostic, which is the accepted-and-ignored shape Fail-Early exists to
refuse. `rpc_mode.run_rpc` now selects `create_ephemeral` when the flag is
set, on the same seam `headless.run_print` uses. Consequences, all of them
pre-existing machinery rather than new:

- D-7's refusals reach the STARTUP session. A host can now meet `-32004
  SESSION_NOT_PERSISTED` on its very first request, where before that state
  required `new_session {"persist": false}`.
- `get_state` gained `addressable` (protocol `1.3`, additive) so a host can
  ASK rather than discover the refusal by tripping it. It is the same
  predicate `new_session`/`fork`/`switch_session` publish, applied to whatever
  session the connection is on.
- `--store jmfts --no-session` is covered by the same line, because
  `JmftsSessionCatalog.create_ephemeral` returns a real in-memory session
  rather than writing to the document server. "No durable write" means both
  stores, not just the one with files. It also means the server need not be
  UP: the §3.1 startup health check is scoped to runs that will write, so a
  `--no-session` RPC process starts against an unreachable store (Tectum's
  prototyping report — the same defect landed first in `--print`). The four
  store-backed verbs stay reachable and fail on their own request instead.
- Durability stays reachable without a respawn: the catalog is live either
  way, so `new_session {"persist": true}` moves the connection onto a
  persisted session at any time.

~~Known gap, stated: every RPC run now leaves a session file behind, where
before it left none. That is the same cost every TUI and headless run already
pays, and it is the price of the verbs meaning what they say.~~

**Amended by unit S — that "known gap" was a regression, and its cost was not
the one this paragraph priced.** `create` writes the session header
immediately and unconditionally, so the file every RPC run left behind landed
in `~/.tau/sessions/<dashed-cwd>/`: durable, listable, 0-message, one per
spawn, including a child that asks `get_capabilities` and exits. It is *not*
the same cost a TUI or headless run pays, because those runs are a human's
own work and an RPC spawn is not. `--continue` is exactly
`catalog.most_recent(os.getcwd())` (`headless.py:363`), so any host spawning τ
per request silently took `tau -p -c` away from the human working in the same
directory, and filled the TUI picker with nameless rows.

The fix separates the STORAGE LOCATION per mode — not the listing, and not by
retreating to `create_ephemeral` (which would re-break every promise this
decision exists to keep):

- **`--mode rpc`'s default session base is `<tempdir>/.tau-<uid>/sessions`**
  (`session_store.rpc_default_session_base`; `/tmp/.tau-<uid>/sessions` unless
  `$TMPDIR` says otherwise). The TUI's and `--print`'s stay `~/.tau/sessions`.
- **`--session-dir DIR` (now implemented, pi `args.ts:112`) overrides it**, in
  both directions, and is deliberately NOT added to `cli.py`'s `--mode rpc`
  rejection set: `--session-dir ~/.tau/sessions` is exactly how a host states
  that it does want to appear in the user's list. `--continue`/`--session`/
  `--fork`/`--resume`/`--name`/`--store` stay rejected there, unchanged.
- **Durability is now bounded by machine uptime**, since most systems clear
  the temp dir on reboot. That is acceptable — a cursor still names a real
  entry a replay can find within the session's life — but it is stated where
  a host reads it: in `set_model`'s and `set_session_name`'s `notes`, and in
  docs/REMOTE-CONTROL.md H1b. The whole point of this decision was to stop the
  wire promising a durability it does not deliver; a quiet lie would be the
  same defect wearing a different hat.
- **`<tmp>/.tau-<uid>` is created `0700`, and a pre-existing path that is not
  a directory this uid owns is refused loudly** (`_ensure_private_dir`, `lstat`
  so a symlink is the non-directory it is); τ never writes into a suspicious
  one and never silently picks another path.
- **The uid is IN the name, and that took a second pass to get right**
  (round-3 finding 1). This first shipped as a flat `<tmp>/.tau`, which on a
  default distro is `/tmp/.tau` — and `/tmp` is `drwxrwxrwt`. The first user
  to run `--mode rpc` created it `0700`, and the ownership guard above then
  correctly refused it for every other user on the box: exit 2 before serving
  a request, `--mode rpc` unavailable to all but one uid until reboot, with an
  error telling them to remove a sticky-bit entry they do not own. The
  security property was right and the availability consequence was never
  asked about. Qualifying the name retires the collision and weakens no
  refusal — a hostile squat on the target uid's own name still aborts the run.

Known gap, restated honestly: the separation is a FILE-store mechanism. A run
configured with `session_store.backend = "jmfts"` still writes into the same
document tree the user's own sessions live in — the per-mode default is
skipped there rather than misapplied (a document store has no directory), and
an explicit `--session-dir` on that backend raises. The JMFTS-side equivalent
would be a distinct `session_store.parent_id` for RPC runs; unclaimed.

### D-7 — the verb that APPENDS refuses an unpersisted session (finding 6)

Blocker 2 gave `set_model` and `set_session_name` `require_durable_session`.
It did not say what made them special, so the next verb guessed. Finding 6
measured the result on ONE `new_session {"persist": false}` session:

| verb | answer |
|---|---|
| `set_model` | `-32603` "this session is unpersisted (path is None …)" |
| `set_session_name` | `-32603` "this session is unpersisted (path is None …)" |

(Those two codes are what finding 6 MEASURED. Round-3 finding 4 then moved
the refusal off `-32603` — see the end of this decision.)
| `set_auto_compaction` | `{"enabled": true, "cursor": "614c4017"}` |
| `compact` | `compaction_end {"performed": false, "cursor": "614c4017"}` |

Three answers, no stated rule, and nothing a host could look up. The rule, and
it is mechanical:

1. **A verb that APPENDS a session-log entry calls `require_durable_session`
   first and refuses an unpersisted session outright.** `set_model`
   (`model_change`), `set_session_name` (`session_info`), `compact`
   (`compaction`). Nothing is mutated before the check, so the refusal is
   total, and for `compact` it runs before the single-flight slot is taken
   and before the provider is paid.
2. **A verb that appends NOTHING never asks the question.** The tier's reads,
   and `set_auto_compaction` — a mutator, D-1-guarded, E5-cursor-carrying,
   whose entire product is an in-memory `CompactionSettings` field. Its
   `cursor` is the live tip reported as a read, not a claim that the call
   wrote something.
3. **It is the SESSION's durability, never the DIRECTORY.** D-6/unit S moved
   `--mode rpc`'s default base to `<tmp>/.tau-<uid>/sessions`; that changes how
   long a persisted session lasts (stated on the wire in the two setters'
   `notes`) and changes nothing here. The rule keys on `path is None`.

Why `compact` gets rule 1 rather than an exemption for "it also does something
in memory": `set_model` also does something in memory — it swaps the live
model — and Blocker 2 settled that it refuses anyway. Giving `compact` the
opposite answer would have been a fourth derivation, not a principle.

**The code the refusal uses: `-32004 SESSION_NOT_PERSISTED`** (round-3 finding
4; it shipped as `-32603 INTERNAL_ERROR` and was corrected). The original
argument was a good one — "the `dialect` error set is a published, closed
vocabulary, and one unusual refusal does not earn an addition to it" — and it
lost to two things. First, `dialect`'s OWN doctrine, written on this same
branch for `REQUEST_TOO_LARGE`: the -32000..-32099 band is exactly where "a
structured, EXPECTED outcome the protocol has a considered answer for" belongs,
as against "the handler raised something nobody planned for". Two units on one
branch had answered one question opposite ways without either citing the other.
Second, and decisive: the generated reference defines `-32603` as *"the handler
raised something it did not raise on purpose"*, and this refusal is the most
deliberate thing in the tier — guarded, documented in three places, checked as
`compact`'s first statement, host-actionable. A host was being asked to tell a
considered refusal from a τ crash by matching English prose.

What did NOT move: a log **missing the appender method entirely**
(`require_log_appender`, §1.1) is still `RuntimeError -> INTERNAL_ERROR`. That
is a store wired wrong, not a session the host can move off, and the two
deserve different codes for the same reason this decision exists.

Two costs, stated rather than hidden:

- **Compaction is unavailable on an unpersisted session**, where it used to
  work. A host asking for both is asking for two contradictory things
  (nothing survives this process / rewrite the log I am keeping); the honest
  fix is the one the refusal message already names, move onto a persisted
  session.
- **`set_auto_compaction(enabled=true)` on an unpersisted session arms a
  mechanism that appends anyway**, from inside
  `AgentSession._maybe_auto_compact` — a path with no RPC verb on it and so
  nothing for rule 1 to guard. Same class as the `AgentSession.compact()`
  does not take `turn_lock` gap D-1 already records: this tier guards the
  wire, not `AgentSession`'s internals.

Out of scope deliberately: `new_session`/`fork`/`switch_session`. They do not
append to the bound log, they replace it, and `persist` is `new_session`'s own
published parameter — a host states durability there rather than discovering
it.

Written once in `rpc/commands.py`'s "DURABILITY in Tier B" block, cited from
every Tier B verb's `notes`, and pinned by
`test_rpc_tier_b_scaffolding.py::test_d7_is_answered_one_way_across_tier_b`
(which reads the shipped handler sources, both directions) and
`::test_every_tier_b_verb_tells_a_host_where_the_durability_rule_lives`.

### D-8 — `abort` reaches an in-flight compaction (finding 5)

`AgentSession.compact` is `emit(agent_start)` → `_perform_compaction` →
`finally: emit(agent_end)`, consulting no abort flag anywhere, and
`AgentSession.abort()` has no handle on the RPC layer's background task. A
measured trace: `response id=4 at t=+0.00s {'result': {'status': 'aborted'}}`,
then `compaction_end at t=+20.01s … 'performed': True`. Neither verb's `notes`
said abort did not reach a compaction.

Of the two honest resolutions, this is the one taken: **`abort` cancels it.**
Telling the truth about not stopping it was the alternative, and it was
rejected because it leaves a host unable to stop the one operation in this
tier bounded by nothing but the provider — the same availability argument
§4[1] makes about `abort` in the first place, and the reason D-5 moved
compaction off the dispatch path at all. A host that can watch it but not
stop it has half an escape hatch.

- `RPCHandler.bind_compaction_aborter` / `abort_compaction` /
  `release_compaction` hold the handle; `commands._handle_compact` binds it
  **from inside `_acknowledge`**, not at task creation. Before that instant
  the compaction is still on D-1's `turn_lock` and the dispatch coroutine is
  still awaiting its own `acknowledged` future — cancelling there would
  resolve that future by cancelling it, wedging the `compact` request instead
  of answering it. It is also the instant the host first learns the
  compaction exists.
- `abort` stays a **signal**. Its response gains `compaction_id` (null when
  none was in flight) — which compaction the signal reached, never whether it
  stopped. That is `compaction_end`'s to say, the same signal-vs-outcome
  split that keeps `cursor` off this response (see `ABORT_RESULT_SCHEMA`).
- The completion carries **`cancelled: true`, no `performed`, and the
  unchanged cursor**. `performed` is absent for the reason it is absent under
  `is_error`: a compaction stopped part-way neither performed nor found
  nothing to do. `cancelled` is `required` and present (`false`) on every
  other outcome — absence is not this tier's way of saying anything (E5
  rule 3).
- **Nothing was written**, and that is a property of the pipeline rather than
  a hope: `_perform_compaction` generates the summary before appending the
  entry, so a cancellation inside the provider call leaves the log as it was.
  Pinned against a real `AgentSession.compact` in
  `test_rpc_tier_b_compact.py::test_an_aborted_compaction_writes_nothing`,
  and against a real child's session store in the conformance test's
  `last_compaction is null` assertion.
- **D-5's shutdown arm is unchanged** — see the amendment on that decision.
  Nobody asked for a reap; a host asked for an abort.
- Still out of reach, stated: an **auto-compaction** (`_maybe_auto_compact`)
  runs inside `AgentSession` with no RPC task to cancel. Same boundary D-7
  rule 2's second cost describes, from the other side.

No `PROTOCOL_VERSION` bump: `1.2` is Tier B's own unreleased version and both
wire changes here (a field on `abort`'s result, a field on the
`compaction_end` notification) are additive under the MINOR rule
`capabilities.py` states, inside the tier that version already names.

### D-9 — `list_sessions`, and "addressable" stops being a claim (findings 8 & 7)

Two findings, one property, so one decision.

**Finding 8.** `switch_session` takes `{session_id: <exact id or unique
prefix>}` and **nothing on the wire produced one**. A host could reach only a
session it had created in this process (`new_session`/`fork`) or whose id it
had recorded from an earlier run's `get_state`; a session made by the TUI, by
`tau -p`, or by a previous RPC child was unreachable. It was also absent from
the `declined` table, so it was a **C1 violation** ("every declined verb is
declined *in the capability document*, with a one-line reason") rather than a
deferral — `grep` for `list_sessions` across this document, REMOTE-CONTROL.md
and the `rpc` package returned nothing at all.

This is `get_models` one round later and one param over, and it ships the same
way: a Tier B read, `since="tier-b"`, `NO_PARAMS_SCHEMA`, its own alphabetical
marker region, an empty EXPOSED/NOT_EXPOSED pair (it reads `runtime._catalog`
and `runtime._cwd`, both private, so no public member becomes newly
reachable), and a `notes` block that states its gaps rather than hiding them.

- **`sessions`** — `SessionCatalog.list(cwd)` for **this process's cwd**,
  newest-modified first, projected to `{session_id, ref, name, title,
  message_count, created, modified, parent, error}`.
- **The listing and `switch_session` are the same set by construction**, not
  by agreement: `resolve_ref` is *built on* `list(cwd)`. So an id this verb
  returns is an id that verb accepts, and one it omits is one that verb
  refuses with `-32602`. That is why there is no `cwd` param — a wider list
  would advertise ids the next call rejects.
- **`ref` is what names the universe.** Unit S (D-6) split the default
  session base per mode, so a host and the human at the terminal are normally
  looking at different lists; each row's store handle (the file store's
  absolute path) is what tells `<tmp>/.tau-<uid>/sessions` from `~/.tau/sessions`.
  Stated limit: the BASE directory is not its own field, because no
  `SessionCatalog` declares one (the file store's is private and `None` means
  the default) and core owns no filesystem knowledge — so an EMPTY listing
  names no location. In RPC mode it is never empty: the child's own startup
  session is a row (D-6), and `get_state`'s `session_id` finds it.
- **`title`, not `first_message`/`last_message`.** Those are whole message
  texts, unbounded; two per row would put an arbitrary multiple of the
  transcript on the wire on every call. `SessionInfo.display_title()` is the
  same data bounded, and it is also the store's answer for a row whose
  entries could not be read — which stays LISTED, with `error` set, rather
  than vanishing.
- **It flags no row as "current"** (get_models' precedent for the active
  model): `get_state` already answers that, exactly rather than by guess.

**Finding 7.** `new_session {"persist": false}` returned
`{"store": "file", "session_id": "5543562f…", "lane": "primary", "cursor":
"614c4017"}` under a schema describing "F2's addressable tuple" — while
`switch_session` on that id answered `-32602 no session matches '5543562f…'`
and `store: "file"` named a file that was never written. Pre-existing in
shape; what made the unqualified description wrong NOW is that the previous
round turned `persist` into a documented, selectable mode.

The session tuple therefore carries **`addressable`**, and the definition is
finding 8's verb: *addressable means `list_sessions` returns this id*. It is
computed by the same `_DURABLE_LOCATION_ATTRS` probe D-7's
`require_durable_session` raises on (`commands.session_log_is_addressable` —
one helper, so "addressable" cannot drift into a second notion of
durability), and it is reported on all three lifecycle verbs, not only the
one whose param made it necessary. `store` keeps its meaning and gains an
honest one: the store THIS CONNECTION's catalog is on, never a claim that
this session is in it.

Not done, and deliberately: `store` is not blanked to `null` for an
unpersisted session (it is real information about the connection, and
removing a field is the breaking change adding one is not), and no verb was
declined — the enumeration shipped instead, which is what C1 asks for when
the reason for absence is "nobody wrote it yet".

No `PROTOCOL_VERSION` bump, for D-8's reason: a new verb and a new result
field are additive under the MINOR rule `capabilities.py` states, inside the
`1.2` this tier already names.

---

## 3. Units

### B0 — scaffolding (serial, lands first)

Everything the six parallel units build on. No verbs.

1. **Marker regions.** Pre-commit empty, alphabetically ordered regions so six
   worktrees can write the same files without conflicting:
   - `rpc/commands.py` — `### begin tier-b:<verb>` / `### end tier-b:<verb>`
     for all six verbs, in the table-definition area.
   - `tests/test_rpc_capability_audit.py` — one region per verb inside `EXPOSED`
     and inside `NOT_EXPOSED`.
   Regions are contiguous and a unit writes **only inside its own**.
2. **The D-1 helper.** One function in the RPC layer that performs the bounded
   `turn_lock` acquire and raises `TURN_STILL_RUNNING`, with the four mutating
   verbs as its stated callers. Extract phase 3's logic; do not duplicate it.
3. **The append guard.** One helper implementing §1.1's "the bound log must have
   this appender, else raise", used by B1 (`append_model_change`) and B5
   (`append_session_info`). **Insufficient on its own — see §1.1's Blocker 2
   correction**: `require_durable_session` is the guard those two verbs
   actually rely on, and this one now only rules out a log that cannot take
   the entry at all.
4. **`docs/REMOTE-CONTROL.md` §3 correction.** The Tier B list claims six
   methods exist on `AgentSession`. Three do not. Correct it, and note that
   `fork` already shipped in phase 3.

B0 does **not** add methods to `AgentSession`, and does not touch the
`SessionLog` Protocol.

### B1–B6 — one verb each (parallel)

Every unit delivers, entirely inside its own marker regions plus one new test
file:

- a params schema and a result schema, in the module's existing style
- the handler, using B0's helpers where D-1/§1.1 require them
- `notes` that state what the verb does, what it refuses, and any known gap
- the `EXPOSED` / `NOT_EXPOSED` audit move
- `tau-agent-core/tests/test_rpc_tier_b_<verb>.py` — a new file, so no unit
  contends on a test file with any other

| Unit | Verb | Notes |
|---|---|---|
| B1 | `set_model` | D-2. Mutating (D-1). Returns model + cursor. |
| B2 | `compact` | Mutating (D-1) — it rewrites the tree. ~~Returns the `CompactionResult` summary + cursor (E5).~~ **Superseded by D-5:** returns an acknowledgement; the summary + cursor ride the `compaction_end` notification. |
| B3 | `get_session_stats` | D-3. Read-only. Compute in the RPC layer from public surface; do not add an `AgentSession` method. |
| B4 | `set_auto_compaction` | D-4. Mutating (D-1). Must test that enabling it actually causes a compaction to fire, and that the orphan event pair arrives with a cursor. |
| B5 | `set_session_name` | Reuse `ExtensionContext.set_session_name`'s logic and its raise; §1.1. Mutating (D-1). Returns cursor. Also expose the read (`get_session_name`) in the same unit. |
| B6 | `get_last_assistant_text` | Read-only, derived from `session.messages`. Smallest unit. |

### B7 — integration (serial, after merge)

- Regenerate `docs/RPC-PROTOCOL.md` via
  `scripts/generate_rpc_protocol_doc.py`; the drift test is the proof.
- One subprocess conformance test in `tau-coding-agent/tests/` driving the new
  verbs end to end against a real `tau --mode rpc` child.
- Full gate: `pytest`, `mypy` over all four `src` trees in one invocation,
  `ruff check` + `ruff format --check` over the same four.

---

## 4. Contention map

| File | Units | Protocol |
|---|---|---|
| `rpc/commands.py` | all six | marker regions (B0) |
| `tests/test_rpc_capability_audit.py` | all six | marker regions (B0) |
| `tests/test_rpc_tier_b_<verb>.py` | one each | new file per unit |
| `rpc/capabilities.py` | none | generated by walking the table — never hand-edit |
| `docs/RPC-PROTOCOL.md` | all six | **generated.** Conflicts are resolved by regenerating in B7, never by merging either side |

`docs/RPC-PROTOCOL.md` deserves emphasis: every unit changes it, so every unit
conflicts on it, and a merge that splices two generated versions together is
wrong even when it applies cleanly. Take neither side; re-run the generator; let
the drift test decide.

---

## 5. Working in a worktree

The editable installs write **absolute paths into the main tree**:

```
venv/lib/python3.11/site-packages/__editable__.tau_agent_core-0.0.0.pth
  → /home/john/Development/agent-harness-py/tau-agent-core/src
```

A worktree that runs `pytest` without correcting for this tests **its own tests
against the main tree's source** — green results that mean nothing. `PYTHONPATH`
takes precedence over `.pth`-injected paths (verified: index 1 vs index 6 in
`sys.path`), so every command in a worktree runs as:

```bash
ln -s /home/john/Development/agent-harness-py/venv <WT>/venv   # .githooks/pre-commit needs its tools
export PYTHONPATH=<WT>/tau-llm/src:<WT>/tau-agent-core/src:<WT>/tau-coding-agent/src:<WT>/tau-jmfts/src
cd <WT>
./venv/bin/python -m pytest ... ; ./venv/bin/ruff check ... ; ./venv/bin/mypy ...
```

Confirm before writing code: `python -c "import tau_agent_core; print(tau_agent_core.__file__)"`
must print a path **inside the worktree**.

`core.hooksPath` is repo-local config, shared with worktrees, so
`.githooks/pre-commit` runs on every commit — hence the `venv` symlink. Never
`--no-verify`.

---

## 6. House rules

- **Fail Early.** No fallbacks, no placeholders, no silent skips. A missing
  appender raises; it does not quietly do nothing. This tier exists partly
  because `ExtensionContext.set_session_name` was once a silent no-op that only
  a `MagicMock` made look like it worked.
- **Every test must be able to fail.** Demonstrate it — mutate the
  implementation, watch the test go red, restore. A test whose docstring names a
  mutation that cannot be expressed is a defect (phase-4 finding 6).
- **Cite the requirement.** Comments and `notes` reference the labels
  (D-1…D-4, E5, F3, C1, R-T2) rather than restating them.
- **`mypy` is the only static check**, and it must run over all four `src` trees
  in one invocation — per-package runs report false errors.
