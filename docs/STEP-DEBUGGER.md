# Stepping an agent: a suspension primitive in core, a debugger beside it

Design record (2026-10-24), nothing built. John asked for a step debugger: the
agent stops at an event, a human reads what happened, answers with an action
(step, step over, continue, abort) and optionally a verdict and a note, and the
agent goes on. It must behave the same in the Textual TUI, `tau --mode repl`,
`tau -p`, `tau --mode rpc`, `tau serve` (local and networked) and Tau Code.

The record makes three changes, in two homes:

- **τ core gets a suspension primitive** (§3): `await ctx.suspend(spec)`. An
  extension states what it needs: actions, fields, context. Every head renders
  it, any attached responder answers it, and core records the answer durably.
- **τ core gets extensible handler policies** (§4, #83): a hook handler names a
  timeout and failure policy at registration. τ ships `gating`, `advisory` and
  `human` as the first three. The API is experimental.
- **A new repository, `tau-debugger`, gets the debugger extension** (§5). It
  will live at `~/Development/tau-debugger`, MIT-licensed like τ and JMFTS, and
  is created when the build starts. It holds the break conditions, the stepping
  modes and the notes. It contains no head-specific code.

§8 lists what is open.

## 1. What is missing, head by head

### 1.1 Nothing in τ can suspend a turn for a human

`agent_loop.py:~722` states the loop "never blocks" on input. The two ways an
extension can involve a human today each fail the requirement:

- **`api.request_user_action` (lock and ask)** appends one `extension_request`
  entry and returns at once (`extension_types.py:2493`). It is durable and every
  head renders it. But a request raised mid-turn is inert, because the turn keeps
  appending behind it and a request is read only at the cursor's leaf
  (EXTENSION-LOCKS.md §4.1). It gates the *next* turn, not the current one.
- **`ctx.ui.form`** (`extension_types.py:643`) does await an answer, mid-turn.
  But it is the one blocking dialog EXTENSION-LOCKS.md left standing, for a
  narrow use: a flow collecting a missing command argument (`extension_types.py:498-503`).
  It reaches a human only when the submission set `allow_user_input`, which
  defaults to `False` (`submission.py:147`). It has a delegate only in the TUI,
  the REPL and the serve daemon.

### 1.2 Per head

| head | an extension can present | the head can send back | short of the requirement |
|---|---|---|---|
| TUI | toast, status, panel with buttons, form modal, ask row + modal (`extension_ui.py:20-59`, `:243-312`) | steer (typed text mid-turn), abort (Esc), panel button, `ctrl+e` chord, ask answer | slash commands refused mid-turn (`app.py:1055-1065`); form only under `allow_user_input` |
| REPL | styled lines, prompt-prefix status, printed panel, field-by-field form and ask (`repl.py:782`, `:2081-2172`) | steer, Ctrl+C abort, `/panel <key> <n>`, ask answer | commands refused mid-turn (`repl.py:1811-1817`); same form gate |
| `-p` text / `--mode json` | `[τ]` lines on stderr, or `{"type":"extension",…}` records; nothing pressable (`headless.py:1098-1106`) | argv prompt, SIGINT/SIGTERM abort | no input channel at all; a pending ask is never surfaced (`headless.py` never reads it); form raises `HeadlessDialogError` |
| `--mode rpc` | notify/status/panel to stderr only; no delegate, no record sink (`rpc_mode.py:123-129`) | the command table, including `abort`, `get_pending_request`, `answer_request` (`rpc/commands.py:3283-3401`) | no push of a pending ask: the host polls; no form at all |
| serve, local | `ui` events (notify, status, panel); `request` events for forms, answered by `Answer`, first wins (`daemon.py:248-283`); asks on `CursorState.request` | `hello`, `attach`, `answer`, RPC verbs at a named cursor (`protocol.py:276-346`) | the form Future lives in RAM, holds the turn lock, and returns **defaults** when no client is attached (`daemon.py:261-263`); `Attached.requests` is not read by the `--connect` client (`client.py:96-110`); panels and statuses are not in the attach snapshot |
| serve, networked | the same | the same | one optional shared token checked at `hello` (`daemon.py:732`), no TLS, no per-client role; `client_id` is a per-connection label, so an answer can name a connection but not a person |
| Tau Code (web, VS Code) | notify, status, panel buttons, forms (`request`/`request_closed`), asks with text/select/multiselect/confirm/number fields (tau-code `ui/src/request-panel.tsx:35-120`) | `answer`, `answer_request`, panel commands | panels and statuses live in a 20-event ring, not the snapshot, so a reattaching head loses them (tau-code `session.ts:151`) |

Three gaps recur across the table, and §3 closes each one:

1. **No head-agnostic wait.** The only mid-turn wait is serve's form, plus the
   in-process TUI and REPL delegates. RPC and `-p` have none.
2. **No pending state in snapshots.** An attaching client learns about durable
   asks through `cursors[].request`, but not about forms, panels or statuses
   reliably.
3. **An unanswered wait fabricates an answer.** Serve's form returns field
   defaults when nobody is attached. For a question that gates work that is a
   silent default, which Fail Early forbids.

### 1.3 What EXTENSION-LOCKS.md requires of a new primitive

EXTENSION-LOCKS.md §1 removed `confirm`, `select` and `input` for three defects:

1. three of the four emitted no record, so only the TUI knew a question was asked;
2. the answer was a return value that only a bound TUI delegate could give;
3. a waiting question held the FIFO turn lock behind a dialog nobody might be
   looking at, and it died with the process.

Its fix was durable tree state for questions that gate the *next* turn. A step
debugger gates the *current* turn, mid-flight, so it cannot use that fix
directly. A new primitive must therefore answer each defect explicitly:

| defect | this record's answer |
|---|---|
| no record outside the TUI | the suspension is an event every head receives, and part of every attach snapshot (§3.4) |
| only a TUI delegate could answer | an answer is a verb any head can send: RPC, serve, the in-process heads (§3.5) |
| holds the turn lock; dies with the process | it holds the lock on purpose (§3.6), but abort, steer, reads and answers still work; a durable entry records it, and a restart reports it instead of losing it silently (§3.7) |

## 2. Why the debugger needs core changes at all

The debugger could be written today against `ctx.ui.form`, in the TUI only.
John ruled that out: a TUI-only debugger is a fix that reaches one head
(feature-doc's second recurring fault). The suspension is a *reading* every head
must present and answer the same way. So it belongs in `tau_agent_core`, and the
heads only render it.

## 3. The primitive: `ctx.suspend`

### 3.1 The call

```python
resolution = await ctx.suspend({
    "title": "Break: repeated tool call",
    "text": "bash `pytest -q` called 3 times with the same arguments",   # body: text | list | table
    "evidence": [{"label": "last result", "text": "…"}],                 # plain data, rendered generically
    "fields": [
        {"name": "verdict", "kind": "select",
         "options": ["fine", "intervene", "intervene_earlier", "wrong"], "required": False},
        {"name": "note", "kind": "text", "required": False},
    ],
    "actions": [{"label": "step"}, {"label": "step over"},
                {"label": "continue"}, {"label": "abort"}],
    "targetId": "3fa9c1d2",            # optional: the entry this is about
})
# resolution = {"id": "…", "action": "step", "values": {"verdict": "fine", "note": "…"},
#               "answered_by": {"client_id": "c3", "client": "tau-code/0.9"}, "at_ms": …}
```

- **The spec is the Ask spec** (`validate_ask_spec`, `extension_types.py:409`),
  plus `evidence` and `targetId`. Every head that renders an ask already renders
  this. Tau Code estimates half a day for it (tau-code-arecibo, 2026-10-24),
  because its `RequestPanel` and field renderers are reused.
- **`evidence` is plain data:** a list of `{label, text}`. Heads render it as
  text. There is no span highlighting and no scroll-to-entry, in v1 or after
  (John, 2026-10-24).
- **`targetId` is named for the JMFTS store.** It is in tau-jmfts
  `_CROSS_REF_FIELDS` (`store.py:158`), so a JMFTS fork remaps it.
- **`abort` is an action like any other** to the extension. When the human
  picks it, core also trips the turn's abort signal (§3.6).

### 3.2 Where it may be called

`ctx.suspend` is permitted only while the runner is dispatching a hook whose
caller is awaiting it and has no entry open at the cursor:

| site | when | what is on disk |
|---|---|---|
| `tool_call` | before a tool runs | the assistant entry, finalized, with its calls |
| `tool_result` | after a tool ran, before its result is written | as above |
| `annotate` (#82) | before any message write, the assistant finalize included | the assistant entry is open (incomplete) |
| mutating `turn_end` | before the next sample | the whole turn so far |
| `user_turn_end` | end of the prompt | the whole prompt |
| an `ext:` channel handler | when it is reached from one of the above | as for that site |

The runner sets a context variable for the dispatch. Python `contextvars`
propagate through `await`, so an extension's own `ext:` bus handler, which a
producer calls from its hook (`await api.emit(...)`), inherits the permission.
That is how the debugger parks on a hit (§5.2).

**Refused everywhere else, with a named error.** That includes the notify
events on the `EventBus` (`message_update`, `message_end`,
`tool_execution_*`). They are emitted *while* a provider stream is open, and a
parked handler would hold the stream open (events.py:315-322). A command handler
is refused too, because it runs between turns, where `request_user_action` is
the right tool.

### 3.3 Lifecycle

1. **Raise.** Core validates the spec, checks the site and the responder rule
   (§3.8), and checks the calling handler's policy (§4.3). It mints a
   suspension id and writes the durable record (§3.7). It registers a Future
   keyed by the id, emits `suspension_open {id, cursor_id, spec}`, and awaits the
   Future raced against the turn's abort signal.
2. **Render.** Every head shows it: the TUI and the REPL natively, serve and RPC
   as an event, Tau Code from the serve event. A client that attaches or
   reattaches later gets it from the snapshot (§3.4).
3. **Answer.** Any responder sends `answer_suspension {id, action, values}`.
   Core validates `values` against the fields (the way `answer_request` does),
   so a head can keep the form up on an error.
4. **Resolve.** The first valid answer resolves the Future, finalizes the durable
   record with the resolution, and emits `suspension_closed {id, resolution}` to
   every head. A later answer to a closed id returns `{"accepted": false,
   "reason": "already resolved", "resolution": …}`. That is not an error, so a
   race between two clients is idempotent and both heads converge.
5. **Return.** `ctx.suspend` returns the resolution to the extension.

### 3.4 Snapshot and events

- **Events:** `suspension_open` and `suspension_closed` are `AgentEvent` types,
  so they reach every head through the paths events already take: the TUI and
  REPL in-process, `--mode json` records, the RPC event stream, and serve.
- **Snapshot:** serve's `Attached` gains `suspensions: [SuspensionState]`, read by
  both the `--connect` client and Tau Code. The RPC head gains
  `get_pending_suspensions`, for a host that connects mid-wait.
- **The protocol change is additive.** It still needs a lockstep τ and tau-code
  release, because `hello` refuses a different `PROTOCOL_VERSION` (tau-code
  `generated.ts` is generated from `tau serve --schema`).

While this is being changed, serve's `Attached` should also carry the live
panels and statuses that tau-code found missing. That gap is real, but it is not
this record's to close, so it is tracked separately (§7).

### 3.5 The answer verb, per head

| head | how a human answers |
|---|---|
| TUI | a modal built from the spec, the same widgets as `ExtensionAskScreen`; it opens by itself, and does not close on Esc without an action |
| REPL | field by field, then a numbered action list, as `_collect_answer` does for asks |
| `--mode rpc` | `answer_suspension` in the command table, unguarded by the turn lock, like `answer_request` |
| serve (local, networked), Tau Code | `answer_suspension` as an RPC verb at a session, from any attached client |
| `-p` | none; see §3.8 |

### 3.6 Locks, and what keeps working

The suspended turn **keeps `Cursor.turn_lock`**. Releasing it would end the
single-writer guarantee for that cursor while the turn is still in flight, and
a second turn would write into the middle of the first.

While a cursor is suspended:

| still works | how |
|---|---|
| answering | `answer_suspension` takes no turn lock |
| abort | trips the turn's signal; the suspension resolves with `action: "abort"`, and the loop takes its existing abort path |
| steer | queued as today; delivered at the next `_deliver_steer`, after the suspension resolves |
| reads, attach, `get_pending_suspensions` | unguarded today |
| other cursors' turns | each cursor has its own lock (`cursor.py:109-110`) |
| `rollback` strategy | it sets abort and then waits for the lock; abort resolves the suspension, so the lock is released |

Mutating RPC verbs and session swaps fail after their existing 5 s wait with
`TURN_STILL_RUNNING`, as they do for any long turn. That is the intended refusal.

**One core fix comes with this.** Today, after a `tool_call` hook returns, the
tool runs even if the signal tripped during the hook: sequential mode checks the
signal before the hooks, and the parallel prepare loop does not check it at all
(`agent_loop.py:1280`, `:1344-1347`). An abort chosen at a `tool_call`
suspension must not run the tool. So the loop checks the signal after the
`tool_call` hooks and before execution, and routes to `_aborted_batch`.

### 3.7 Durability: recorded, and reported after a restart

The Future lives in memory. A restart cannot resume a parked coroutine, and v1
does not pretend to. Instead:

- **A durable record.** On raise, core writes a `suspension` entry as a side
  child of the cursor's leaf. It does not move the cursor, so the turn's next
  entry still hangs from the leaf and the path the model reads is unchanged. The
  entry is written with `status: "incomplete"` and holds `{spec, site,
  targetId}`. On resolve, core finalizes it with the resolution, by the
  incomplete → finalize path every store already has.
- **The answer is the verdict record.** A finalized `suspension` entry holds
  the action, the field values (the verdict and the note) and who answered. The
  debugger's dataset is these entries. They mirror to JMFTS like any other
  entry.
- **A restart reports, and never continues silently.** On load, an incomplete
  `suspension` entry means a turn was suspended when the process stopped.
  - The session raises a session warning (#60's warnings store), naming the
    suspension and its site.
  - The turn's pending tool calls are in the state any interrupted turn leaves
    (`admission_reason` names them), and recovery is the existing one: continue
    from an earlier entry.
  - Resuming at a boundary is deferred (§7).

Three changes make the side entry safe:

- `default_leaf` skips the `suspension` kind, as it skips `navigate`
  (`session_log.py:170`). Otherwise a reopened session would continue from a
  resolved suspension.
- `entries_to_messages` ignores it. It is not on any path the model reads,
  because it is never an ancestor of a later entry.
- The tree views (the TUI tree browser, `get_tree`) render it as a mark on its
  parent, not as a branch.

**Why not a namespaced kind** (`system:kind`)? `default_leaf` already skips
those, but its docstring reserves them for documents another system put in the
tree, such as the JMFTS store's `jmfts:document` (`session_log.py:158-161`).
`suspension` is τ's own, so it is a bare word with its own skip.

**Why not `ext` on the target entry?** `ext` is write-once, written in the
entry's one write (ENTRY-ANNOTATIONS.md §4). A suspension at `tool_call`
happens after the assistant entry is final. A side entry needs no rewrite.

### 3.8 Responders, and heads with none

A suspension is raised only if something can answer it. Core keeps a responder
count per session:

- the TUI and the REPL count as one responder while they are bound;
- an RPC host counts once it declares the `suspensions` capability in the
  handshake;
- each serve client counts once its `hello` declares the capability.

| situation | behaviour |
|---|---|
| `-p`, text or `--mode json` | there is no responder. `ctx.suspend` raises `NoResponderError`. The debugger arms only after checking `ctx.can_suspend()`, so `/step on` under `-p` fails at arm time, naming the alternatives: run under `tau serve` and attach a client, or use `--mode rpc` |
| serve with zero attached responders | the suspension **waits**. It does not default. `list_sessions` shows the cursor as suspended, and the first client to attach receives it in its snapshot |
| `ui.form` under `allow_user_input=False` | unchanged: the headless policy applies (§3.9) |

A `-p` control channel (stdin JSON or a socket) is deferred (§7). `tau serve`
plus an attached client covers the scripted case today.

### 3.9 Relation to `request_user_action` and `ctx.ui.form`

- **`request_user_action` stays,** for a different job: a durable gate on the
  next turn that survives a restart. A suspension gates the current turn. The
  two share the Ask spec and the head renderers.
- **`ctx.ui.form` is reimplemented on `ctx.suspend`.** A form becomes a
  suspension with the actions `submit` and `cancel`. That removes `ServeUI.form`'s
  private Future and its `request`/`Answer` path, and with it the defaults that
  serve fabricates when nobody is attached. Forms then work in RPC too. The
  headless policy (`--ui-defaults form=defaults`, or `HeadlessDialogError`) is
  kept for heads with no responder, because a flow collecting a command argument
  may legitimately take defaults. A step-debugger break may not.
- The serve verbs `answer` and the `request`/`request_closed` events are
  superseded by `answer_suspension` and `suspension_open`/`suspension_closed`.
  They are removed in the same release. τ is below 1.0, and tau-code is the only
  client (tau-code-arecibo).

### 3.10 Who may answer over networked serve

Any client that passed `hello` may answer, which is the same rule as every other
write verb today. Per-client roles are not added here: τ's threat model is
scrappy, and tokens and locks are opt-in. The resolution records who answered
(`client_id` and the `hello.client` label), so the dataset can attribute
verdicts to a connection. Naming a person needs a client-supplied display name,
which is deferred (§7).

## 4. Handler policies (#83): an open mechanism

John wants this extensible, and he will not fix a set until he has tinkered.
So the policy is a registered object, not an enum. **The API is experimental**:
its names and defaults may change below 1.0.

### 4.1 Shape

```python
api.register_policy("gating", HandlerPolicy(
    timeout_s=10.0,
    on_timeout="fail",      # fail | proceed
    on_error="fail",        # fail | proceed
    surface="error",        # error | warning
))
api.on("tool_call", handler, policy="gating")
```

- **`timeout_s`:** `None` means no timeout.
- **`fail`** means what failure means at that hook: a `tool_call` blocks the
  call (it already fails closed on an exception, `tool_pipeline.py:297`); an
  `annotate` writes the error ext and aborts the turn (ENTRY-ANNOTATIONS.md
  §4.3); `turn_end` and `tool_result` end the turn with the error.
- **`proceed`** continues as if the handler had returned nothing.
- **`surface`** chooses between a turn error and a session warning
  (`ctx.set_warning`).
- A timed-out handler is cancelled (`asyncio.wait_for`), so the work it was
  doing stops.

### 4.2 Built-ins

| name | `timeout_s` | `on_timeout` | `on_error` | `surface` | for |
|---|---|---|---|---|---|
| `gating` | 10 | fail | fail | error | a decider or check whose answer gates the step |
| `advisory` | 2 | proceed | proceed | warning | a recorder or monitor whose absence must not stop work |
| `human` | none | — | fail | error | a handler that suspends for a human (§3) |

The numbers are a first guess for John to tune; they are not measured. A
handler that names no policy behaves exactly as today: no timeout, and errors as
each hook handles them now. So landing this changes no existing extension.

### 4.3 How policies and suspension meet

`ctx.suspend` refuses, with a named error, inside a handler whose policy has a
timeout. A human cannot be timed out without a fabricated answer, so the
mismatch is caught at the call, not after 10 seconds. The debugger registers its
handlers with `policy="human"`.

## 5. The debugger extension (`tau-debugger`)

### 5.1 Home and dependencies

- **Repository:** `~/Development/tau-debugger`, MIT. It is created when the
  build starts.
- **It depends on** τ ≥ the release that ships §3 and §4, and on #82 (`annotate`).
- **It contains no head code.** Everything it shows goes through `ctx.suspend`,
  `ctx.ui.set_status` and its commands.

### 5.2 Break conditions over the bus

Producers announce and emit on their own `ext:` channels (`extension_types.py:57`).
The debugger only consumes them:

- `ext:<producer>:debug_conditions`, emitted at `session_start`: `[{name,
  describe}]`. This feeds the break-set picker.
- `ext:<producer>:debug_hit`, emitted when a condition fires: `{condition,
  evidence, targetId?}`.

Emission is awaited (`events.py:19-25`), and the producer emits from inside its
own hook. So the debugger's handler for `debug_hit` runs at a permitted site
(§3.2) and can suspend there, holding the producer's hook and with it the loop.
A producer with no debugger loaded pays one emit with no subscribers.

The `all` channel does not receive `ext:` channels (`events.py:483-495`), so the
debugger subscribes to each producer by name. The list comes from its config.

### 5.3 Built-in deterministic conditions

These need no decider. Each is observable at a permitted site:

| condition | site |
|---|---|
| every step (each completion, each tool call) | `annotate`, `tool_call` |
| a tool, or a named tool | `tool_call` |
| a tool error (`is_error`; a nonzero bash exit since #88) | `tool_result` |
| a risky bash command, `git commit`/`push`/`reset` | `tool_call`, regex over `command` |
| a write or edit to a test file, or outside an allowlist | `tool_call`, the `path` argument |
| an edit diff that removes asserts | `tool_result`, the edit tool's `details.diff` |
| the same call repeated N times; the same error text repeated | `tool_call` / `tool_result`, debugger state |
| oversized tool output | `tool_result`, in characters |
| a text-only completion; an assistant claim regex ("all tests pass") | `annotate`, assistant |
| N completions with no file edits | debugger state |
| a context-size threshold | `annotate`, assistant `usage` |

Conditions from other producers: todo created, changed and completed come from
`examples/38_todo.py`, which gains three emits. Decider questions (`q:<id>`)
come from the shadow extension. A model change comes from tau-router.

Two conditions are deliberately not in v1:

- **compaction and provider retries.** They are visible only as notify events
  (`side_completion_start`, `provider_retry`), where suspending is refused (§3.2).
  They can still be shown in the status line.
- **delegate/`spawn_branch`.** It has no event; a delegate tool is caught as a
  named tool.

### 5.4 Stepping semantics

- **step:** suspend at the next permitted site with any condition, then return
  to step mode.
- **step over** (continue): run until a condition in the selected break set
  fires.
- **continue:** run without stopping until the human arms stepping again.
- **abort:** trip the turn's signal (§3.6).
- The break set is edited in the suspension's own fields (a multiselect), and
  with `/break add|rm|list` between turns.

### 5.5 Where the mode lives

The mode and the break set are written as a `customEntry`
(`tau_debugger.config`) by the arming command, between turns. Because they are
in the tree:

- a fork carries the mode with it;
- navigating back before the arming entry disarms;
- a restart keeps it.

This follows the per-cursor-config-in-the-tree direction (CURSORS.md). The
debugger reads its config at the cursor when a turn starts.

### 5.6 Notes and verdicts

- **At a suspension:** the note and verdict are the resolution's field values,
  stored durably in the finalized `suspension` entry (§3.7). The debugger writes
  nothing else.
- **At an `annotate`-site break:** the debugger *also* returns the resolution
  from its `annotate` handler, so it lands in `ext["tau_debugger"]` on the very
  entry the human looked at (ENTRY-ANNOTATIONS.md §2). That makes "verdict per
  entry" a single read.
- **Between turns:** `/note [targetId] <text>` appends a `tau_debugger.note`
  `customEntry`.
- **Voice:** no τ head captures audio, and Tau Code has no microphone access. An
  OS-level whisper hotkey that types into whichever client has focus fills any
  note field, in every head. Nothing in τ or tau-debugger changes for it.

### 5.7 Cost when not armed

The debugger registers its handlers at load, and each returns at once when the
config at the cursor says unarmed. The cost is one dictionary read per hook
call, plus `entry_blocks` on each written message for its `annotate` handler.
Conformance (§6) measures a turn with the debugger loaded and unarmed against a
turn without it.

## 6. Conformance

### 6.1 One scripted session, stepped from every head

The scenario uses a faux provider (the test helpers' stub). One prompt yields a
completion that calls `bash` twice, then one that answers in text. The debugger
is armed in `every` mode, and the script answers each suspension in order: step,
step with a note, step over, continue.

Every head must produce the same tree: the same entries, the same four finalized
`suspension` entries with the same resolutions, and the same `ext` on the
annotated entries.

| head | driver |
|---|---|
| TUI | Textual `pilot`, pressing the modal's actions |
| REPL | a pty or stdin driver answering the field prompts |
| `--mode rpc` | a JSON driver that reads `suspension_open` and sends `answer_suspension` |
| serve, local | the τ client library over the unix socket |
| serve, networked | the same, over loopback TCP with `serve.token` set |
| Tau Code | tau-code's own e2e suite against a pinned `tau serve` (in that repository) |
| `-p` | arming fails with `NoResponderError`, and no turn runs |

### 6.2 The edge cases

- two clients answer at once: one wins, the other gets `already resolved`, and
  both heads close the suspension;
- a client disconnects while a suspension is open and reattaches: it sees the
  suspension in `Attached.suspensions`;
- serve with no client attached: the suspension waits, and the first client to
  attach answers it;
- abort during a `tool_call` suspension: the tool does not run, and the batch
  gets aborted results;
- kill the daemon during a suspension and reload: the incomplete `suspension`
  entry raises a session warning, the reopened leaf is not the side entry, and
  no turn continues;
- a `gating`-policy handler that calls `ctx.suspend` gets the named error;
- a suspension from a notify-event handler gets the named error.

A headless harness (no Textual) drives the RPC and serve rows, so the matrix
runs in the container matrix with the rest of the suite.

## 7. Scope

**v1:**

- §3 in τ core: `ctx.suspend`, `ctx.can_suspend`, the events, the snapshot
  field, `answer_suspension`, `get_pending_suspensions`, and the `suspension`
  entry with its default-leaf, path and tree-view rules;
- `ctx.ui.form` reimplemented on it; serve's `answer` and `request` removed;
- the `tool_call` abort check (§3.6);
- §4: `register_policy`, `policy=` on `api.on`, the three built-ins,
  experimental;
- head rendering in the TUI, REPL, RPC, serve and Tau Code;
- `tau-debugger` with §5.3's conditions, the bus convention, the stepping modes
  and notes;
- three emits in `examples/38_todo.py`;
- the §6 matrix.

**Deferred, each with its reason:**

- **a `-p` control channel:** serve plus an attached client covers scripted use.
- **resuming a suspension after a restart:** v1 reports it (§3.7); resuming
  mid-turn needs a parked coroutine to be reconstructed.
- **notes mid-turn while not suspended:** an append at the cursor while an
  entry is open breaks `finalize` (`cursor.py:293-315`). Notes go in a
  suspension, or between turns.
- **per-client roles and display names over networked serve** (§3.10).
- **breaking on notify events** (compaction, retries, deltas): suspending there
  holds the provider stream open.
- **panels and statuses in serve's `Attached`:** a real gap tau-code found, but
  not this record's. It gets its own issue.
- **evidence-span highlighting:** cut by John, not deferred.

## 8. Open

1. **The policy numbers** (§4.2): 10 s for `gating` and 2 s for `advisory` are
   guesses. John tunes them.
2. **The side entry** (§3.7): the reviewers should check that a `suspension`
   entry as a non-moving side child is safe for every walker: `default_leaf`,
   `ConversationTree`, `tree_surgery.admission_reason`, the JMFTS mirror and the
   tree views. If one of them cannot be taught the kind cheaply, the fallback
   is to keep the record only in memory and lose the restart report. That is
   weaker, and it would need John's agreement.
