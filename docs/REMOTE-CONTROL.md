# Spec: remote control — τ as a process you drive, not a library you import

**Status:** shipped 2026-08-01 → 08-07. `--mode rpc` is wired
(`cli.py:557` → `tau_coding_agent.rpc_mode.run_rpc`); `rpc.py` became a
package (`tau-agent-core/src/tau_agent_core/rpc/`: commands, handler,
transport, dialect, capabilities, wire_events). 20 command verbs implemented,
7 formally declined with a stated rationale (`commands.py:3642-3727`), Tier B
adds 6 more (`docs/RPC-TIER-B.md`). 463 RPC-scoped tests pass, including 28
true-subprocess conformance tests. This document's design decisions below are
the record of what was built, not a proposal — see `ROADMAP.md`'s "RPC /
remote control (Tier 12)" for the compressed shipped-state summary and
evidence citations. The three deferrals stated in §1 ("Out of scope,
deliberately") are still accurate: no reverse channel, no socket/TCP
transport.

**Relationship to existing docs.** `RPC-PROTOCOL.md` documents the protocol
`rpc.py` speaks *today* — six methods, JSON-RPC 2.0, and no way to reach it from
the CLI. This document states what that surface must become to be a product, and
supersedes `RPC-PROTOCOL.md` as the design of record; that file becomes the
generated reference once §6 lands. `docs/PI-RPC-REPLACEMENT.md` is the
complementary half: what one consumer (tectum) needs. This is what τ should
offer regardless of that consumer. `NODE-ADDRESSABLE-AGENTS.md` owns the tree
invariants §7.2 depends on and is not restated here.

**Provenance.** pi facts are read from `~/Development/pi` at `6d5ede31`,
`packages/coding-agent/src/modes/rpc/` (`rpc-mode.ts` 774 lines, `rpc-types.ts`
264, `rpc-client.ts` 575, `jsonl.ts` 58) plus `core/output-guard.ts`. τ facts are
from this checkout at `9cf92f5`. Where something was not verified, it says so.

---

## 1. Scope

**In scope.** One τ process, one session, driven over ndjson on stdio by a host
that may be written in any language.

**Out of scope, deliberately.**

- **The reverse channel** (extension → host UI requests). Deferred, but the
  protocol must not foreclose it. §7.1.
- **Multiple writing processes on one conversation.** Forbidden by
  `NODE-ADDRESSABLE-AGENTS.md` decision 6, and this document does not relitigate
  it. What it does do is avoid *baking in* the assumption, so the forbidden thing
  stays cheaply reachable if the precondition is ever revisited. §7.2.
- **Sockets, TCP, multiplexed sessions.** stdio is the interface. §7.3 records
  the two places a later transport would touch, so that it stays an adjustment.

**Non-goal.** Faithful agent reconstruction from the wire. Same position as
`NODE-ADDRESSABLE-AGENTS.md` §5: the tree owns what was said, the invoker owns
who speaks next. A host that wants the same agent back supplies the same frame.

---

## 2. Design goals

**G1 — The wire is the product, not a debugging aid.** Versioned, documented,
and covered by a contract suite runnable without τ internals. A second
implementation should be possible from this document plus the generated
reference. Same idiom as `testing/session_log_contract.py`.

**G2 — The host need not be Python.** Everything else follows: JSON-RPC 2.0,
capability discovery, no Python-shaped payloads, no assumption that the host can
import anything. This is the property being sold. tectum's scan-safety ban list
(`torch · whisper · pyannote · transformers · httpx · nats · numpy ·
sounddevice · sentence_transformers`) is one consumer stating it in its own
dialect; every host in every language has the same problem.

**G3 — Nothing unbounded is ever pushed.** Push deltas and small events; pull
large state. This dissolves a bug class by construction rather than by asking
every client to raise a limit.

**G4 — Backpressure is mandatory, not advisory.** The loop stalls when the host
cannot keep up.

**G5 — The process boundary is a guarantee.** Kill always works, all the way
down the process tree, with defined exit codes. This is what a host buys by
paying for a process instead of an import.

**G6 — Provenance in, provenance out.** τ's Submission contract
(`events.py:62-77`) is the one place this surface can be better than pi rather
than equal to it.

**G7 — Fail Early applies to the wire.** Unknown method → error. Unsupported
capability → declared, not silently no-op'd. Broken pipe → die.

**G8 — Compatibility is a shim with an expiry, not a peer dialect.**

---

## 3. The blocks

```
        host process  (any language)
              │
   ═══════════╪══════════════════════════════  process boundary  ═══════
              │
  ┌───────────┴───────────┐
  │ [1] Transport         │  framing · ordering · backpressure · stdout takeover
  ├───────────────────────┤
  │ [2] Dialect           │  envelope: request / response / event / (later) ui-request
  ├───────────────────────┤
  │ [3] Command table     │  inbound verbs
  │ [4] Event stream      │  outbound telemetry
  │ [5] Reverse channel   │  DEFERRED — §7.1 reserves the shape
  ├───────────────────────┤
  │ [6] Runtime host      │  session lifecycle ABOVE AgentSession
  ├───────────────────────┤
  │ [7] Process contract  │  signals · exit codes · child reaping · shutdown order
  │ [8] Capability doc    │  introspectable, and honest about what is missing
  └───────────────────────┘
              │
        AgentSession → AgentLoop → provider
```

Blocks 1, 5, 6, 7, 8 do not exist in τ. Blocks 2, 3, 4 exist partially, in
`rpc.py`, unreachable: `cli.py:141-145` accepts `--mode text|json` and the file
contains no reference to `RPCHandler`.

---

## 4. Block specifications

### [1] Transport

pi: `jsonl.ts` is LF-only and *deliberately does not use Node readline*, because
readline splits on U+2028/U+2029, which are legal inside JSON strings.
`output-guard.ts` supplies `takeOverStdout` / `writeRawStdout` /
`waitForRawStdoutBackpressure` / `flushRawStdout`, and `rpc-mode.ts` couples the
agent loop to stdout drain:

```ts
unsubscribeBackpressure = session.agent.subscribe(async () => {
    await waitForRawStdoutBackpressure();
});
```

τ: `_read_lines` uses `sys.stdin.readline` in an executor. The writer is an
**unbounded** `asyncio.Queue` polled at 0.5 s with no drain coupling
(`rpc.py:285-310`). There is no stdout takeover, so a `print()` from any tool or
extension corrupts the protocol stream silently.

- **T1** Framing is LF-only and documented as such; `\r` tolerated on input.
  Never `splitlines()`, which splits on U+2028/U+2029 in Python too.
- **T2** stdout is claimed exclusively at mode start; `sys.stdout` is rebound to
  stderr for every non-protocol writer. Port of `takeOverStdout`.
- **T3** The outbound queue's **event stream** is bounded, and the agent loop
  awaits drain at a per-event checkpoint. This is the difference between "the
  host is slow" and "τ's RSS grows without limit". The bound is a credit count
  held against events specifically, not a cap on the queue as a whole:
  **responses are enqueued uncredited** and can always pass a stalled event
  stream, because the host that most needs `abort` is exactly the host that has
  stopped reading, and a response that queued behind the backlog it is meant to
  end would deadlock. The consequence is that T3 bounds what *τ* produces
  unprompted, not what a *host* can ask for: a host that pipelines requests
  without reading responses still grows τ's memory, one queued response per
  unread request (measured: 200,000 pipelined `get_state` requests, RSS
  38,800 kB → 182,096 kB). No request cap ships — see §10 for why that is a
  question rather than an omission.
- **T4** stderr is a documented log channel, not noise. Today tectum drains and
  discards it (`agent_pool.py:150-153`), which makes every τ-side error invisible
  to the operator; that is a reasonable thing for a host to do only if τ has
  promised nothing lives there.
- **T5** A write failure terminates the process. `rpc.py:298-309` already does
  this and explains why; promote the comment to protocol text.
- **T6** FIFO is the only ordering guarantee, and it is a guarantee.
  `rpc.py:298-310` records the bug that establishes this (twenty responses
  emerging `1,3,5,…,0,15,…` across executor threads).
- **T7** One inbound request line has a **stated maximum size**, and exceeding
  it is a *refusal*, never a death. Added by the Tier B review (finding 9),
  which measured the alternative: the reader inherited `asyncio.StreamReader`'s
  64 KiB default limit, `readline()` enforces it by RAISING, nothing caught it,
  and one 100 KB `prompt` — a pasted file, a stack trace, a diff — ended the
  process with no JSON-RPC error, nothing on stderr, and every later request on
  that connection lost with it. Three properties, and all three are the
  requirement:
  1. **The bound is generous** (`transport.MAX_REQUEST_LINE_BYTES`, 8 MiB). A
     limit a *correct* host trips over in normal use is not a limit.
  2. **The bound is enforced and survivable.** `REQUEST_TOO_LARGE` (-32003,
     `id: null` — the id was inside the bytes deliberately never parsed), a T4
     stderr line, and the offending line discarded *through its next LF* so the
     stream resynchronizes on real framing rather than parsing the tail of a
     rejected request as a fresh one. Nothing is buffered while discarding, so
     a peer that never sends an LF at all is refused ONCE and costs a flat
     bound's worth of memory however long it keeps going.
  3. **The bound is discoverable** — `get_capabilities` →
     `limits.max_request_line_bytes`, the generated reference, and
     `error.data`, all three read off the one constant. G7 restated for a
     number: a bound that exists but is never stated is the same defect one
     order of magnitude later.

  Note what T7 is *not*: it is not a cap on how many requests may be in flight.
  That is a different growth path, still open — §10 open question 4.

  T7 also has a **sibling input class**, closed at the Tier B review's
  integration: a request line that is not valid UTF-8. `printf
  '\xff\xfe\n{"jsonrpc":"2.0","id":1,"method":"get_state"}\n' | tau --mode rpc`
  killed the child out of the same reader (`UnicodeDecodeError` escaping
  `_read_stdin`, rc 1, empty stdout, nothing on stderr, and the well-formed
  request behind it lost). Two bytes. Same answer as T7 — refuse the line,
  consume it through its LF, keep serving — but reported as `PARSE_ERROR`
  rather than a code of its own: JSON text is UTF-8 (RFC 8259 §8.1), so bytes
  that are not UTF-8 are not JSON, which is the claim -32700 already makes.
  `id: null` for T7's reason; `error.data` names the byte offset.

- **T8** τ's **responses are not line-bounded, and a host must not bound
  them.** The mirror of T7, and not symmetric with it: τ refuses over-long
  input because it can, and refuses to cap output because it cannot — a
  `get_messages` result is as large as the conversation. Stated as a
  requirement because it is a live trap rather than a theoretical one: 64 KiB
  is the *default* line length of `asyncio.StreamReader` and of a good many
  other readers, and `get_capabilities` — the one verb K2 tells every host to
  send FIRST — answers with more than that. Measured at the Tier B review's
  integration: 65,258 bytes at the start of the round, over the line by the
  end of it, at which point nine of this repo's own subprocess tests failed
  because the *test driver* was framing with `readline()`. τ shipped the whole
  round 278 bytes from a cliff that takes out every default-configured
  `asyncio` host, and nothing said so anywhere.
  1. **The generated reference states it**, with the current measured size
     (`protocol_doc`'s "What a host must be prepared to RECEIVE"), computed
     rather than hand-copied so the number cannot go stale.
  2. **A host frames its own lines over chunked reads.** This is what
     `transport._read_stdin` itself does since T7; the two sides of the wire
     have the same problem and the same solution.
  3. **No bound is advertised**, deliberately. `limits` publishes what a host
     may SEND, and every value in it is read off the code that enforces it —
     a ceiling on responses that nothing enforces would be exactly the drift
     that key exists to make impossible.

  Events remain the exception G3 already carves out: nothing unbounded is ever
  *pushed*. T8 is about responses, which a host asked for.

### [2] Dialect

Keep **JSON-RPC 2.0** as the native dialect. It buys standard error objects, an
unambiguous request/notification distinction, a defined place to put a
server-originated request (§7.1), and off-the-shelf clients in every language —
which is G2 restated. pi's `{id?, type}` shape is a Node-shaped protocol that
happens to be JSON, and it emits session events **raw** (`session.subscribe(event
=> output(event))`), which conflates "the event object" with "the wire format"
and makes every internal event-shape change a wire break.

- **D1** One native dialect. One **compat shim** for pi's dialect behind
  `--rpc-dialect pi`, documented as a migration affordance with a stated expiry.
  Not a peer. Its job is to buy a differential harness during a port — same host,
  same wire, `pi` vs `tau` as the only variable.
- **D2** Responses echo the method they answered (`result.method`). JSON-RPC
  gives only `id`; a human reading a transcript should not have to maintain the
  id→method map. This is pi's `command` field and pi is right about it.
- **D3** The wire event schema is a *projection* of `AgentEvent`, not
  `AgentEvent` itself. It may lag, and adding an internal field must not change
  the wire without a version bump.

### [3] Command table

pi has 29 commands. τ has 6. tectum uses 3. Sizing this work off any one
consumer undercounts it.

**Tier A — required of any host.** τ has 4 of 7.
`prompt` · `abort` · `new_session` · `get_state` · `get_messages` ·
`get_commands` · `get_tools` · `get_capabilities`

**Tier B — corrected ground truth: `docs/RPC-TIER-B.md` §1.** This list
originally claimed all six exist "because `AgentSession` already has the
method." Half do not — read `docs/RPC-TIER-B.md` §1 before implementing any
of them; it is the ground truth every Tier B unit works from, not this
paragraph.
`set_model` (`:715`, exists — but does not persist anything; a wired verb
must append a `model_change` entry itself, RPC-TIER-B.md D-2, and must first
establish that the bound session can KEEP it, D-6/§1.1) ·
`compact` (`:2956`, exists) ·
`set_auto_compaction` (**no accessor exists** — `AgentSession
._compaction_settings` is a mutable, private `CompactionSettings`; also the
only route to a capability RPC mode cannot reach at all today, since RPC
sessions construct with `compaction_settings=CompactionSettings(enabled=
False)`) ·
`get_session_stats` (backed by `get_usage` `:783`, but a bare re-shaping of
it is redundant against `get_state`; it must ship scoped UP — context-token
headroom, effective `CompactionSettings`, last-compaction state — or not at
all, RPC-TIER-B.md D-3) ·
`get_last_assistant_text` (**no method exists** — trivially derived from
`AgentSession.messages`) ·
`set_session_name` (**no `AgentSession` method exists** — implemented on
`ExtensionContext.set_session_name`, `extension_types.py:2203`, not on
`AgentSession` itself).

`fork` (`_spawn_fork` `:2368`) — listed here in an earlier draft of this
table — is NOT a Tier B item: it already shipped as a Tier A verb in phase 3
(the `new_session`/`fork`/`switch_session` runtime-host trio, H1, backed by
`AgentSessionRuntime`). It does not belong in this list at all.

Two verbs in this tier come from neither pi nor the list above, and both were
added because a SHIPPED param had no wire-side source for its value —
`docs/RPC-TIER-B.md` §1 has no row for either:
`get_models` (finding 7 of the Tier B review — `set_model` takes a config
model NAME and nothing enumerated them) ·
`list_sessions` (finding 8, `docs/RPC-TIER-B.md` D-9 — `switch_session` takes
a session id and nothing enumerated those either, so a host could reach only
the sessions it had itself created in that process; see H1d).
Both are the same defect and the same fix: a verb whose argument a host can
only obtain by reading `~/.tau/config.json` or `~/.tau/sessions/` out of band
defeats G1, and leaving the enumeration unbuilt AND undeclined violates C1.

**Tier C — τ-justified, no pi equivalent.**

- **`submit`** — the differentiator. τ stamps `submission_id · source ·
  submitter · correlation` on every event (`events.py:62-77`); pi hardcodes
  `source: "rpc"`. A host multiplexing agents over a bus can say *"this prompt
  came from subject X on behalf of Y"* and get it back on every resulting event.
  tectum currently fakes this with a `TECTUM_BINDING_ID` environment variable.
  Lead with it; do not bolt it on after `prompt`.
- **`open_lane` / `close_lane` / `list_lanes`** — expose `BranchView`. See §7.2:
  this delivers most of the multi-agent-on-one-tree use case *today*, within one
  writing process, at zero cost to decision 6.
- **`navigate`** — move the cursor. τ's tree-as-truth model has no pi analogue
  worth copying.
- **`enable_extension` / `disable_extension` / `reload_extension`** —
  `:1106-1209`, already implemented, trivially exposed.

**Tier D — declined, with the reason stated in the capability document.**
`cycle_model` · `cycle_thinking_level` · `set_steering_mode` ·
`set_follow_up_mode` — TUI keybinding affordances leaking into a machine
protocol. A remote host enumerates and sets; it does not cycle.
`export_html` — a host can render. `bash` as an out-of-band command — τ's bash is
a tool, and a second privileged path into the same executor is a second thing to
secure.

- **C1** Every declined verb is declined *in the capability document*, with a
  one-line reason. Declining silently is how pi's `setWorkingMessage` became a
  no-op nobody can discover.
- **C2** Unknown method → JSON-RPC error `-32601`, never ignored.
- **C3** `prompt` has two completions: an immediate response acknowledging
  *acceptance* (pi's `preflightResult`), then an `agent_end` event. A rejected
  prompt errors on the response. Both pi and tectum already depend on this shape;
  it is correct and should be kept.
  **Tier B extends C3 to `compact`** (Blocker 1 of the Tier B review): its
  summarization call is bounded by nothing but the provider, and running it
  inline on the dispatch path stopped `transport._read_stdin` from *parsing*
  the next line — `abort` included — for its whole duration (measured: 20s).
  So `compact` answers with `{accepted, compaction_id}` and reports the
  outcome on a `compaction_end` notification carrying that id plus the
  originating `request_id`. It is a distinct notification method rather than
  an `event`, because every `event` carries a `WireEvent` whose `type` is a
  closed copy of `AgentEvent.type` and a compaction outcome is not an
  `AgentEvent`. Consequence, stated: the capability document has no slot for
  a notification's payload schema, so `compaction_end`'s field list lives in
  the verb's `notes` and in `commands.COMPACTION_END_PARAMS_SCHEMA`. Any
  future verb whose work is unbounded takes this same shape.
  **`abort` reaches it** (finding 5 of the Tier B review,
  `docs/RPC-TIER-B.md` D-8). It did not, and said otherwise: a measured trace
  answered `{"status": "aborted"}` at +0.00s and delivered `compaction_end
  {"performed": true}` at +20.01s, because `AgentSession.compact` consults no
  abort flag. Moving unbounded work off the dispatch path is only half an
  escape hatch if the host can then watch it but not stop it — the same
  argument §4[1] makes about `abort` itself. So `abort` now cancels the
  in-flight compaction, its response gains `compaction_id` (null when none
  was running) naming which one the SIGNAL reached, and the outcome arrives
  as `compaction_end` with `cancelled: true` and no `performed`. Nothing is
  written: the summary is generated before the entry is appended. Any future
  verb that takes C3 for unbounded work owes a host the same pair — a way to
  stop it, and a completion that says it stopped.

### [4] Event stream

Tiers as agreed.

- **E1** `message_update` carries a **delta**, never the cumulative message.
  τ's `AgentEvent` has no delta field (`events.py:80-96`) and `rpc.py:502-522`
  serializes `event.message` wholesale, so the wire is quadratic: a 32 KB reply
  becomes ~32K lines averaging 16 KB. The prefix-diff τ's TUI already runs
  (`backends.py:192-199`) moves into the projection.
- **E2** **No unbounded push.** `agent_end` announces completion and carries
  counts; the message array is *pulled* via `get_messages`. Deliberate divergence
  from pi, and it retires the whole 64 KiB-line failure class rather than
  requiring every client to raise a limit.
- **E3** Additive and versioned. Unknown fields MUST be ignored by clients, and
  that must be stated so adding fields is safe.
- **E4** Every event carries the submission provenance quad when a submission
  drove it, and `null` when none did — never a fabricated id (`events.py:62-77`
  already states this rule for the in-process case).
- **E5** Every response to a mutating command returns the resulting cursor. See
  §7.2: no host may cache "the tip".

### [6] Runtime host

pi separates `AgentSessionRuntime` — `newSession` · `fork` · `switchSession` ·
`dispose` · `setRebindSession` — from `AgentSession`, one conversation.
`rebindSession()` re-subscribes the event stream *and* re-binds extensions after
every swap.

τ has no such object, and this is the honest answer to "τ has no `new_session`".
It is not a missing method on `AgentSession`; it is a missing **layer** that the
TUI already implements ad hoc at `app.py:3648` (`action_clear_chat`: create a
catalog session carrying the same system prompt, `_bind_backend_session()`, reset
`messages`). The seam it uses already exists and is already documented as
existing for this purpose — `AgentSession.session_log` is a settable property
(`:663-667`) and `messages` is *derived*, not stored
(`:639-646`, `ConversationTree(log.entries(), log.cursor).context_for()`).

- **H1** Extract `AgentSessionRuntime` from `app.py`. One implementation serves
  the TUI, print mode, and RPC. Highest-leverage item in this document, and it is
  refactoring rather than new design.
- **H2** `new_session` returns `{cancelled}`. An extension may veto, and a host
  must be able to treat a veto as a hard failure.
- **H1a** (added by Blocker 2 of the Tier B review, `docs/RPC-TIER-B.md` D-6)
  Both sessions a host can land on are **persisted by default**: `--mode rpc`
  binds its startup session with `SessionCatalog.create`, and `new_session`
  takes `persist`, defaulting to `true`. "Fresh" was always the load-bearing
  half of RPC mode's fresh-and-unpersisted convention; unpersisted was what
  made `set_model`/`set_session_name` return cursors for entries no replay
  could see. A host that wants an unpersisted conversation asks for one
  (`new_session {"persist": false}`) and those two verbs then refuse on it.
  The startup CLI restrictions (`--session`/`--continue`/`--fork`/`--resume`/
  `--name`/`--store` rejected for `--mode rpc`) are enforced separately in
  `cli.py` and are unchanged.
- **H1b** (unit S — the regression H1a caused) Persisted, but **not into the
  user's session list**. `SessionCatalog.create` writes its header immediately
  and unconditionally, so H1a made every `--mode rpc` spawn leave a durable,
  listable, 0-message session in `~/.tau/sessions/<dashed-cwd>/` — a child that
  asks `get_capabilities` and exits included. `--continue` is exactly
  `catalog.most_recent(os.getcwd())`, so a host spawning τ per request took
  `tau -p -c` away from the human working in the same directory. So the
  **default session base for `--mode rpc` is `<tempdir>/.tau/sessions`**
  (`/tmp/.tau-<uid>/sessions` unless `$TMPDIR` says otherwise); the TUI's and
  `--print`'s stay `~/.tau/sessions`. Three things a host must know:
  - `--session-dir DIR` overrides it and is **accepted at startup under
    `--mode rpc`** (the one session flag that is). `--session-dir
    ~/.tau/sessions` is how a host states that it does want to appear in the
    user's list; that is a deliberate choice, not a default.
  - **Durability is bounded by machine uptime**, since most systems clear the
    temp dir on reboot. A cursor from `set_model`/`set_session_name` still
    names a real entry a replay can find within the session's life — which is
    what H1a was about — but "forever" is not what it means, and both verbs'
    `notes` say so on the wire. E5 is not weakened; its horizon is stated.
  - `<tmp>/.tau-<uid>` is created `0700` and a pre-existing path that is not a
    directory this uid owns is **refused loudly** — never written into, never
    silently swapped for another path. The uid is IN the name deliberately
    (round-3 finding 1): the flat `<tmp>/.tau` this first shipped as meant the
    first user to run `--mode rpc` on a box owned it and every other user was
    refused at startup, exit 2, with an error whose stated remedy the sticky
    bit on `/tmp` forbids. Qualifying the name costs nothing and retires the
    collision; a hostile squat on *your* uid's name is still refused, which is
    what the guard is actually for.

  Known gap: this separation is a file-store mechanism. A run configured with
  `session_store.backend = "jmfts"` still writes into the same document tree
  the user's own sessions live in; the equivalent fix there is a distinct
  `parent_id` for RPC runs, which nobody has claimed.
- **H1c** (finding 6 of the Tier B review, `docs/RPC-TIER-B.md` D-7) Which
  verbs refuse an unpersisted session is **one rule, not a list to memorise**:
  *the verb that APPENDS a session-log entry refuses; the verb that appends
  nothing does not ask.* `set_model`, `set_session_name` and `compact` refuse
  with `-32004 SESSION_NOT_PERSISTED`; `set_auto_compaction` — a mutator that
  carries a cursor and still appends nothing — answers, as do every read. That
  code is its own (round-3 finding 4): the refusal first shipped as `-32603`,
  which the generated reference defines as "the handler raised something it
  did not raise on purpose" — a host could not tell a considered refusal from
  a τ crash without matching English prose, and this is the most deliberate
  refusal in the tier. H1b's temp-dir default
  does not enter into it: the rule keys on `path is None`, i.e. on the
  session, never on the directory. Stated costs, both in D-7: compaction is
  unavailable on an unpersisted session, and auto-compaction still appends
  there because that append happens inside `AgentSession`, below the wire
  this rule governs.
- **H1d** (findings 8 and 7 of the Tier B review, `docs/RPC-TIER-B.md` D-9)
  A host can now **enumerate** the sessions it may switch to, and the tuple
  the three verbs above return **says whether it is addressable**. Before
  this, `switch_session` took an id nothing on the wire produced — reachable
  sessions were only the ones this process had itself created — and
  `new_session {"persist": false}` returned a tuple the schema called
  "addressable" for a session `switch_session` answered `-32602` for.
  - **`list_sessions`** returns `SessionCatalog.list(cwd)` for this
    process's cwd, newest first, plus a `scope` of `{store, cwd}`. The
    listing and `switch_session`'s resolution are the same set *by
    construction* — `resolve_ref` is built on `list(cwd)` — so there is no
    `cwd` param to widen it: a wider list would advertise ids the next call
    refuses.
  - **Each row's `ref` is what says which universe it is**, which H1b made a
    real question: an RPC child lists `<tmp>/.tau-<uid>/sessions` while the human's
    TUI lists `~/.tau/sessions`. The base directory is not its own field (no
    catalog declares one, and core owns no filesystem knowledge); the refs
    name it, and in RPC mode the list is never empty because the child's own
    startup session is in it.
  - **`session.addressable`** means exactly "`list_sessions` returns this
    id". It is the same `_DURABLE_LOCATION_ATTRS` question H1c's rule asks,
    so the two cannot drift apart, and it is reported on all three verbs.
    `store` still names the store this CONNECTION is on — not a claim that
    the session is in it.
- **H3** Define the **reset set** explicitly: log, cursor, usage, `side_usage`,
  last-compaction anchor, queued messages, deferred ops, streaming flag. Anything
  not on that list survives *on purpose* — system prompt, tools, model,
  extensions, provider client stay warm. That warmth is the entire reason a host
  pools a process instead of respawning.
- **H4** Rebind is atomic with respect to the event stream: no event from the old
  session may arrive after the `new_session` response.

### [7] Process contract

pi: SIGTERM→143, SIGHUP→129, `killTrackedDetachedChildren()`, stdin EOF →
shutdown, `flushRawStdout()` before exit except on SIGTERM, extension-requested
shutdown checked after every command.

τ: `run()`/`stop()` with careful task-shutdown ordering and a good comment
explaining a real deadlock (`rpc.py:197-236`), but no signal handling, no exit
codes, and no child reaping. `tools/bash.py` calls `process.kill()` on the shell
only (`:127`, `:171`, `:184`) — no `start_new_session`, no `killpg`.

- **P1** SIGTERM/SIGHUP handled; exit codes 143/129; stdin EOF is a clean
  shutdown.
- **P2** Bash children run in their own process group and are killed by group.
  **Without this, a host's `terminate`/`kill` leaves orphans, and G5 is a lie in
  exactly the case it exists for** — the runaway tool loop.
- **P3** Extensions can request shutdown, checked after each command rather than
  polled.
- **P4** Flush before exit, except on SIGTERM, where the host is already
  impatient. The flush is bounded by a **no-progress deadline**, not by total
  time: EOF reports only that the host closed the end it *writes* to, and a
  host that also stopped reading leaves the writer parked on a full pipe with
  a backlog behind it — τ then cannot be ended by its own documented
  clean-shutdown trigger. A host that is merely slow keeps the flush alive by
  accepting bytes; a host that is absent gets the remaining output dropped and
  a line on stderr (T4) saying so. Truncating loudly beats not exiting.

### [8] Capability document

pi has none; support is discovered by a method quietly doing nothing.

- **K1** `get_capabilities` → `{protocol_version, dialect, commands[], events[],
  ui_methods[], declined[{name, reason}]}`.
- **K2** Version is negotiated on connect. A host may refuse to run against an
  incompatible protocol rather than discovering it on first failure.
- **K3** The document is machine-readable and is the same artifact the generated
  reference is built from. §6.

---

## 5. pi consistency ledger

| Concern | pi | τ | Decision |
|---|---|---|---|
| Framing | LF-only, anti-readline | readline, undocumented | **Copy**, and document (T1) |
| stdout takeover | yes | no | **Copy** (T2) |
| Backpressure | loop awaits drain | unbounded queue | **Copy** (T3) |
| Envelope | bespoke `{id,type}` | JSON-RPC 2.0 | **Diverge** — keep JSON-RPC (G2) |
| Response echoes verb | `command` field | id only | **Copy** (D2) |
| Events on the wire | raw session event | wrapped | **Diverge** — projection (D3) |
| Streaming granularity | `text_delta` | cumulative message | **Copy pi's shape** (E1) |
| Terminal payload | full message array pushed | full array pushed | **Diverge both** — pull (E2) |
| `prompt` dual completion | preflight + end | single result | **Copy** (C3) |
| Session lifecycle layer | `AgentSessionRuntime` | absent | **Copy** (H1) |
| Provenance on events | `source:"rpc"` | full quad | **Diverge** — τ is better (E4) |
| `cycle_*` verbs | present | absent | **Decline** (Tier D) |
| Out-of-band `bash` | present | absent | **Decline** (Tier D) |
| Signals / exit codes | SIGTERM 143, SIGHUP 129 | none | **Copy** (P1) |
| Child reaping | tracked-child kill | none | **Copy, stronger** — process group (P2) |
| Capability introspection | none | none | **Diverge** — add (K1) |

---

## 6. Capability inventory: appraisal of instrumenting `AgentSession`

**The proposal.** Decorate `AgentSession` methods — `@rpc(name="compact",
tier="B")` — and derive both the command table and the capability document by
introspection, so they cannot drift from the implementation.

**What it would genuinely buy.** One source of truth; K1 becomes free and always
true; adding a verb costs one line; `inspect.signature` plus type hints could
generate a params schema; a single test asserts the table is complete.

Those are real. The objections are nonetheless decisive **in the form proposed**.

**A1 — Layer inversion.** `AgentSession` is core; the wire is transport. A
decorator puts wire metadata on core methods, which is the same category of
mistake as pi's `cycle_model`: a consumer's concern embedded in the thing being
consumed. τ has been careful about this in exactly this area — `get_model()`
returns three fields specifically to keep the extension API decoupled from the
full model schema (`:688-703`). A decorator quietly reverses that discipline.

**A2 — The mapping is not 1:1, and the interesting verbs are the non-1:1 ones.**
Walk the table. `prompt` needs a preflight callback and has two completions;
`new_session`, `fork`, and `switch_session` are **runtime-host** operations that
do not exist on `AgentSession` at all and must trigger a rebind; `get_state` is
an aggregate over eight properties; `get_messages` is a property; `submit` takes
a rich `Submission`. Roughly four of the ~20 verbs map cleanly to "call this
method with these params". A mechanism that handles the easy 20% and needs an
escape hatch for the rest is worse than no mechanism, because the hard cases —
the ones that need review — end up in the un-introspected hatch.

**A3 — Python signature stability is not wire stability.** Deriving params from
`inspect.signature` couples the wire to Python parameter names and defaults.
Renaming a kwarg for readability becomes a silent break for every host. G1 wants
the wire schema to be a deliberate, reviewed artifact; a decorator makes it an
*emergent* one, changed by edits whose authors are not thinking about the wire.

**A4 — "Capability" is mostly a statement about what does *not* work.** The
valuable content of K1 is `declined[{name, reason}]`, the unsupported UI methods,
and the protocol version. None of it is derivable from decorated methods. The one
place introspection is most wanted — C1's "declined, with a reason" — is exactly
the place it cannot reach.

**A5 — It evaporates under mypy.** τ's only static gate is mypy. A
decorator-built dispatch table is `dict[str, Callable[..., Any]]`; the types
vanish precisely at the boundary where a wrong signature costs a host a bad day.
An explicit handler module keeps them.

**A6 — Import-time registries are order-dependent.** A registry populated by
decoration requires the defining module to have been imported. With extensions
loading dynamically, "which verbs exist" becomes a function of load order. That
is a Heisenbug generator, and it is avoidable by not building it.

### Recommendation: audit, do not generate

Invert the direction. The mechanism costs less and catches more.

1. **An explicit, declarative command table** in the RPC layer — one entry per
   verb: `name`, `params_schema`, `handler`, `tier`, `since`, `notes`, or for a
   declined verb, `declined_because`. Hand-written, reviewed, and diffable. This
   *is* the capability document; K3 falls out.

2. **An introspective audit test**, not an introspective mechanism. It walks
   `AgentSession`'s and `AgentSessionRuntime`'s public methods and asserts each is
   either in the table or in an explicit `NOT_EXPOSED = {name: reason}` map. It
   fails when a new session method is added and not triaged, *and* when a table
   entry's handler disappears. This delivers the anti-drift property the decorator
   promised, in both directions, and it also produces the declined-with-reason
   content the decorator cannot — because writing the reason is how you make the
   test pass. Same idiom as `testing/session_log_contract.py`.

3. **Generate the event half.** This is where the objections do not apply.
   `AgentEvent` is already a pydantic model with a `Literal` type union
   (`events.py:80-91`), the mapping to the wire *is* 1:1 modulo the E1/E2
   projections, and pydantic emits JSON Schema directly. Generate `events[]` and
   their schemas; hand-write `commands[]`.

4. **Decorators are fine one layer down.** The objection is to decorating
   `AgentSession`, not to decorators. `@command("compact", tier="B")` on a handler
   function inside `rpc/commands.py` is a registry within the layer that owns the
   wire: no inversion, no signature coupling, no dynamic-import ambiguity. If a
   registry is wanted for ergonomics, put it there.

**One thing to keep dynamic.** `get_commands` — slash commands contributed by
extensions, prompt templates, skills — is genuinely runtime-variable and pi
already builds it by enumeration at call time. That is a different question from
"which protocol verbs exist", which is static and should stay static. Do not let
the dynamism of the former argue for dynamism in the latter.

**Cost, stated honestly.** The audit test needs a maintained `NOT_EXPOSED` map,
and someone adding a session method will have to write one line about it. That is
the tax. It is also the mechanism by which the capability document acquires its
most valuable content, so it is being paid either way — the only question is
whether it is paid deliberately or discovered by a host.

---

## 7. Forward compatibility

### 7.1 Reverse channel (deferred)

pi's `extension_ui_request` / `extension_ui_response` is UUID-correlated, with
`select · confirm · input · editor` blocking on the host, `notify · setStatus ·
setWidget · setTitle · setEditorText` fire-and-forget, and ~10 methods explicitly
no-op'd in RPC mode. `createDialogPromise` resolves to a declared default on
timeout or abort rather than hanging.

Out of scope for v1. Three cheap reservations keep it additive:

- **RC1** The spec states that a client MUST answer a server-originated JSON-RPC
  request, at minimum with error `-32601`. A v1 client that ignores it hangs the
  agent; a v1 client that errors it lets τ fail fast. One sentence now, and the
  channel becomes purely additive later.
- **RC2** Server-originated ids live in a separate namespace from client ids
  (prefix or sign), so no correlation collision is possible when the channel
  arrives.
- **RC3** v1 policy for an extension that calls a UI method: **fail fast with the
  declared default**, and record it — pi's timeout behaviour with `timeout=0`.
  Never hang. `get_capabilities().ui_methods` is `[]` in v1, which is the honest
  statement of that.

**Note the first real consumer.** tectum, used as a τ interface, occupies exactly
this role — it already has a bus and channels that reach a human, so
`confirm("proceed?")` routed out to a speak/listen loop is the reverse channel's
natural first implementation. The channel is deferred, not hypothetical, and it
should be designed against that consumer when it is built.

### 7.2 Several agents at different points on one tree

`NODE-ADDRESSABLE-AGENTS.md` decision 6 already settles this, and its statement
is not reopened here: *a conversation has exactly one writing process;
concurrency inside a conversation is lanes, concurrency across processes is
`fork(mode="export")` to a separate conversation.* Its analysis of the hazard is
the important part and is easy to get wrong — the danger is **not** entry-id
collision (`_generate_entry_id` retries against the log's id set, and the
cross-process collision window is negligible) but that `_leaf_id` is
process-local memory nothing re-reads, so two writers parent off the same node
and the conversation silently becomes a fork instead of a line.

What this means for the remote-control surface, without overscoping:

- **F1 — Ship lanes, not multi-process.** The stated use case — several agents
  at different points on a shared tree — is *already available in one process*.
  `BranchView` (`session_log.py:392`) holds its own `_leaf_id` independent of the
  log's primary cursor, `resolve_cursor` skips lane-tagged entries so a
  sub-agent's write can never become the primary tip (`:153-186`), and
  `ConversationTree` lane-scopes its descendant walks (`:603-641`). Exposing
  `open_lane` / `list_lanes` / `close_lane` over RPC (Tier C) delivers the use
  case now, at zero cost to decision 6, because it is still one writing process.
  This is the recommendation.

- **F2 — Session identity on the wire is an addressable tuple, not a file.**
  `{store, session_id, lane, cursor}`, from v1, even though v1 only ever has one
  lane and one store per process. Costs nothing now; it is the difference between
  a later multi-writer story being additive and being a protocol break.

- **F3 — No host may cache "the tip".** E5 — every mutating response returns the
  resulting cursor. Today this is merely convenient. It is also precisely the
  discipline that makes a future compare-and-append store workable, because a host
  that already re-reads the cursor after every mutation needs no change when the
  cursor stops being process-local.

- **F4 — Do not build optimistic concurrency on speculation.** The one primitive
  a real multi-writer story needs is `append_at(parent, …, expect_leaf=…)`
  failing if the lane's leaf moved — one method on the `SessionLog` Protocol
  across three stores plus the contract suite. The JMFTS store already *detects*
  the violation (`store.py:400-405`, *"a second writer touched the tree"*);
  supporting it rather than detecting it is a deliberate future decision with a
  real cost. F1–F3 are what makes that decision cheap to take later. Do not take
  it now.

### 7.3 Transports beyond stdio

stdio is the interface. A socket or TCP transport later touches exactly two
places, and only stays cheap if they are kept clean now:

- **X1** Block [1] is the only code that knows about `sys.stdin`/`sys.stdout`.
  The reader/writer pair takes a stream abstraction; nothing above block [1]
  names a file descriptor.
- **X2** Block [7]'s shutdown triggers are transport-specific — stdin EOF is a
  stdio concept, a closed socket is the analogue. Keep "the peer is gone" as one
  named event with per-transport detection, rather than `if not line: break`
  scattered through the handler.

Multiplexed sessions over one connection are a *different* change — it makes
session identity part of every message, and F2 is what would make that additive.
One process per session remains the model.

---

## 8. Decisions taken

1. **JSON-RPC 2.0 is the native dialect.** pi's dialect is a compat shim with a
   stated expiry (D1, G8).
2. **Nothing unbounded is pushed.** Deltas out, bulk state pulled (E1, E2, G3).
3. **The command table is hand-written; the event schema is generated; both are
   audited by introspection.** No decorators on `AgentSession` (§6).
4. **`AgentSessionRuntime` is extracted from the TUI**, not added as a method on
   `AgentSession` (H1).
5. **The process boundary is a guarantee**, which means process-group kill, not
   `process.kill()` on a shell (P2, G5).
6. **The reverse channel is deferred with three reservations**, not designed
   (RC1–RC3).
7. **Multi-agent-on-one-tree ships as lanes in one process** (F1). Decision 6 of
   `NODE-ADDRESSABLE-AGENTS.md` stands; F2–F3 keep the alternative cheap without
   building it.
8. **Declining is a documented act.** Every verb pi has that τ does not implement
   carries a reason in `get_capabilities` (C1, K1).

---

## 9. Test obligations

Following this repo's idiom that a contract is executable:

- **R-T1** Protocol conformance suite, driving a real τ process over pipes:
  framing, ordering, unknown-method error, dual completion on `prompt`, cursor
  returned on every mutation.
- **R-T2** The §6 audit test: every public `AgentSession` /
  `AgentSessionRuntime` method is exposed or explicitly not-exposed with a
  reason.
- **R-T3** Backpressure: a host that stops reading must stall the loop rather
  than grow τ's memory. Assert, under a non-reading peer, that the loop's own
  consumption of the provider stream actually stops — not merely that few events
  were counted, which an unbounded queue satisfies too if nothing forced it to
  fill first. Scope is the event stream (§4[1] T3): a host that pipelines
  *requests* without reading is not covered by this bound.
- **R-T4** Kill semantics: a tool that backgrounds a child must leave no orphan
  after the host's `terminate` (P2). This is the test that makes G5 true rather
  than aspirational.
- **R-T5** stdout hygiene: a tool that calls `print()` must not corrupt the
  stream (T2).
- **R-T6** Delta correctness: concatenating every `message_update` delta over a
  turn reproduces the final assistant text exactly (E1).

---

## 10. Resolved, and still open

**Resolved (2026-08-04).**

9. **`AgentSessionRuntime` lives in `tau-agent-core`.** RPC mode is not a TUI
   concern, and `session_catalog.py` is already in core, so no new dependency is
   created. `app.py` becomes a consumer of the extracted runtime rather than its
   owner. This is H1's target package.
10. **`prompt` survives, defined in terms of `submit`.** One implementation, two
    names: `prompt`'s handler constructs a default `Submission` and calls
    `submit`. Keeps the simplest possible host simple and keeps the pi-dialect
    shim (D1) implementable, without giving up G6 — provenance is always present
    on the wire, merely defaulted when the host does not supply it.

**Resolved (phase 4).**

1. **The T3 backpressure mechanism — and a correction to this question's own
   premise.** This entry previously claimed τ's bus is "explicitly
   fire-and-forget" (citing two `agent_session.py` line ranges that have since
   moved; the phrase lives in `subscribe_channel`, `route_session_event`, and
   `_emit_stamped`) and concluded from that that "a handler that blocks on a
   bounded `put()` stalls the handler task and not the loop." **That
   conclusion was wrong, verified against the actual code (phase-4 review):**

   - `EventBus.emit` (`events.py`) calls each subscribed handler and, when the
     call returns a coroutine, `await`s it before moving to the next handler
     or returning — it does not schedule-and-move-on.
   - `AgentSession._emit_stamped` (the `emit=` `AgentLoop` is constructed
     with) does `await self._events.emit(...)`.
   - `AgentLoop.run` does `await self._emit(...)` at every emission site.

   So the chain `AgentLoop.run` → `_emit_stamped` → `EventBus.emit` → `await
   handler(event)` was **already** the awaited path pi's own
   `session.agent.subscribe(async () => await waitForRawStdoutBackpressure())`
   is — one layer down, on the SAME bus. "Fire-and-forget", everywhere
   `agent_session.py` uses that phrase for this bus, is a **failure-isolation**
   contract (a handler that raises does not kill its siblings — the exception
   goes to `_surface_handler_error`, not silently dropped) — never a
   scheduling one. The ONE place scheduling is genuinely fire-and-forget is
   `AgentSession.route_session_event`'s `loop.create_task(self._events
   .emit_channel(...))` — the session-lifecycle *channel* bridge
   (`session_start`/`session_before_compact`/etc.), a completely different
   subscription from the `AgentEvent` stream `RPCHandler.subscribe`s to.

   **Consequence:** no new "pace hook"/"awaiting channel"/"gating `emit`
   itself" was needed — the three candidates this entry used to list were all
   reinventions of a mechanism that already existed. The shipped fix is
   `RPCHandler._forward_event` itself, made `async`, genuinely suspending
   (`_acquire_event_credit`) when the bound (T3) is reached. See §4[1] T3 and
   §9 R-T3 for the bound, the drain coupling, and the abort-reachability
   argument that goes with it (a stalled emit needs its own abort checkpoint,
   since `AbortSignal` is a polled flag with no async wakeup and a stalled
   `_emit` call is not at any of the checkpoints that already poll it).

**Still open.**

2. **The H3 reset set's per-item semantics.** The list is enumerated; what each
   item resets *to* is not (in particular whether the last-compaction anchor is
   cleared or re-derived from the fresh log).
3. **Does `--mode rpc` belong on `tau`, or is it a separate console script?**
   Affects nothing architecturally; affects how discoverable the surface is.
4. **Should inbound requests be capped too, and if so by what?** T3 as shipped
   bounds the event stream; responses are deliberately uncredited so `abort`
   can always pass a stalled turn (§4[1] T3). That leaves one growth path open:
   a host that pipelines requests and never reads the replies accumulates one
   queued response each (measured: 200,000 `get_state` requests, RSS
   38,800 kB → 182,096 kB). No cap ships, because the obvious ones are all
   worse than the leak. Stop reading stdin at a depth: the reader is also how
   `abort` and `terminate` arrive, so the host loses exactly the escape hatch
   T3's uncredited-response rule exists to preserve. Reject requests past a
   depth: a host cannot distinguish that rejection from a real refusal without
   new dialect. Bound the response queue: same deadlock the uncredited rule
   avoids. The honest framing is that this is self-harm by a host that is
   ignoring its own replies — unlike the event stream, where τ produces
   unprompted and a *correct* host can still be overrun. It becomes worth
   solving when a real host hits it, and the answer probably lives in the
   capability document (K1: an advertised max in-flight request count the host
   is obliged to respect) rather than in the transport.

   **Narrowed, not answered (Tier B review, finding 9).** A neighbouring
   question — how *big* may one request be — turned out not to be open at all
   but broken: the reader had an accidental 64 KiB bound that killed the
   process instead of refusing. T7 answers that half, and does it in exactly
   the shape this entry predicts for the other one: an advertised max in the
   capability document (`limits.max_request_line_bytes`) that a host is obliged
   to respect, with a defined refusal past it. What stays open here is
   unchanged and is specifically about COUNT — how many unanswered requests a
   host may have outstanding — for which none of the three rejected mechanisms
   above has become any better. When it is taken, `limits` is where it goes.
