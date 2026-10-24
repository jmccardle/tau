# Stepping an agent: a suspension primitive in core, a debugger beside it

Design record (2026-10-24), nothing built; John approved the plan the same day.

John asked for a step debugger. The agent stops at an event. A human reads what
happened and answers with an action (step, step over, continue, abort), and
optionally a verdict and a note. Then the agent goes on. It must behave the same
in the Textual TUI, `tau --mode repl`, `tau -p`, `tau --mode rpc`, `tau serve`
(local and networked) and Tau Code.

The record makes three changes, in two homes:

- **τ core gets a suspension primitive** (§3): `await api.suspend(spec)`. An
  extension states what it needs: actions, fields and context. Every head
  renders it. A live responder answers it in-process, or a later process answers
  it from the tree. Either way the turn resumes from the tree by one rule (§3.7).
- **τ core gets extensible handler policies** (§4, #83). A hook handler names a
  policy at registration. A policy is a protocol, not a set of enum fields. τ
  ships `gating`, `advisory` and `human`. The API is experimental.
- **A new repository, `tau-debugger`, gets the debugger extension** (§5). It
  will live at `~/Development/tau-debugger`, MIT-licensed like τ and JMFTS, and
  is created when the build starts. It contains no head-specific code.

Nothing is open (§8). The record was revised after two review rounds and an
independent architecture review (§9 maps each finding to its section).

## 1. What is missing, head by head

### 1.1 Nothing in τ can suspend a turn for a human

`agent_loop.py:~722` says the loop "never blocks" on input. The two ways an
extension can involve a human today each fall short:

- **`api.request_user_action` (lock and ask)** appends one `extension_request`
  entry and returns at once (`extension_types.py:2493`). It is durable, and every
  head renders it. But a request raised mid-turn is inert, because the turn keeps
  appending behind it and a request is read only at the cursor's leaf
  (EXTENSION-LOCKS.md §4.1). It gates the *next* turn, not the current one.
- **`ctx.ui.form`** (`extension_types.py:643`) does await an answer mid-turn.
  But it is the one blocking dialog EXTENSION-LOCKS.md left standing, for a
  narrow use: a flow collecting a missing command argument
  (`extension_types.py:498-503`). It reaches a human only when the submission set
  `allow_user_input` (default `False`, `submission.py:147`). It has a delegate
  only in the TUI, the REPL and the serve daemon.

### 1.2 Per head

| head | an extension can present | the head can send back | short of the requirement |
|---|---|---|---|
| TUI | toast, status, panel with buttons, form modal, ask row and modal (`extension_ui.py:20-59`, `:243-312`) | steer, abort (Esc), panel button, `ctrl+e` chord, ask answer | slash commands refused mid-turn (`app.py:1055-1065`); form only under `allow_user_input` |
| TUI under `--connect` | the same, from serve events (`serve/remote.py:872-891`) | `answer` for forms, `answer_request` for asks | panel actions go through `submit` with `reject`, so they are refused mid-turn (`remote.py:1004-1029`); `Attached.requests` is never read (`client.py:96-110`) |
| REPL | styled lines, prompt-prefix status, printed panel, field-by-field form and ask (`repl.py:782`, `:2081-2172`) | steer, Ctrl+C abort, `/panel <key> <n>`, ask answer | commands refused mid-turn (`repl.py:1811-1817`); same form gate |
| `-p` text / `--mode json` | `[τ]` lines on stderr, or `{"type":"extension",…}` records; nothing pressable (`headless.py:1098-1106`) | argv prompt; SIGINT/SIGTERM abort | no input channel; a pending ask is never surfaced; form raises `HeadlessDialogError` |
| `--mode rpc` | notify, status and panel go to stderr only; no delegate, no record sink (`rpc_mode.py:123-129`) | the command table, including `abort`, `get_pending_request`, `answer_request` (`rpc/commands.py:3283-3401`) | no push of a pending ask; stdin is read **serially** (`rpc/transport.py:263`), so a request that waits blocks every later line, an answer included |
| serve, local | `ui` events; `request` events for forms, answered by `Answer` with the first answer winning (`daemon.py:248-283`); asks on `CursorState.request` | `hello`, `attach`, `answer`, RPC verbs at a named cursor (`protocol.py:276-346`) | the form Future lives in RAM, holds the turn lock, and returns **defaults** when no client is attached (`daemon.py:261-263`); panels and statuses are not in the attach snapshot |
| serve, networked | the same | the same | one optional shared token checked at `hello` (`daemon.py:732`); no TLS; no per-client role; `client_id` names a connection, not a person |
| Tau Code (web, VS Code) | notify, status, panel buttons, forms, asks with text/select/multiselect/confirm/number fields (tau-code `ui/src/request-panel.tsx:35-120`) | `answer`, `answer_request`, panel commands | panels and statuses live in a 20-event ring, not the snapshot (tau-code `session.ts:151`) |

Three gaps recur, and §3 closes each one:

1. **No head-agnostic wait.** The only mid-turn wait is serve's form, plus the
   in-process TUI and REPL delegates.
2. **No pending state in snapshots.** Asks arrive through `cursors[].request`.
   Forms, panels and statuses do not arrive reliably.
3. **An unanswered wait fabricates an answer.** Serve's form returns field
   defaults when nobody is attached. For a question that gates work, that is a
   silent default, which Fail Early forbids.

### 1.3 What EXTENSION-LOCKS.md requires of a new primitive

EXTENSION-LOCKS.md §1 removed `confirm`, `select` and `input` for three defects:

1. three of the four emitted no record, so only the TUI knew a question was asked;
2. the answer was a return value that only a bound TUI delegate could give;
3. a waiting question held the FIFO turn lock behind a dialog nobody might be
   looking at, and it died with the process.

Its fix was durable tree state for questions that gate the *next* turn. A step
debugger gates the *current* turn. The new primitive answers each defect
directly:

| defect | this record's answer |
|---|---|
| no record outside the TUI | a suspension is an event every head receives, and part of every attach snapshot (§3.4) |
| only a TUI delegate could answer | an answer is a verb any head can send, and a later `-p` process can send it (§3.5) |
| holds the turn lock; dies with the process | it holds the lock on purpose, while abort, steer, reads and answers keep working (§3.6). It is a durable entry, and a process that dies or exits leaves a turn that resumes from the tree (§3.7) |

## 2. Why the debugger needs core changes

The debugger could be written today against `ctx.ui.form`, in the TUI only.
John ruled that out: a TUI-only debugger is a fix that reaches one head
(feature-doc's second recurring fault). A suspension is a *reading* every head
must present and answer the same way. So it belongs in `tau_agent_core`, and the
heads only render it.

## 3. The primitive: `api.suspend`

### 3.1 The call

```python
resolution = await api.suspend({
    "title": "Break: repeated tool call",
    "text": "bash `pytest -q` called 3 times with the same arguments",   # body: text | list | table
    "evidence": [{"label": "last result", "text": "…"}],                 # plain data
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

- **It is on `api`, resolved through the dispatch context.** A handler on an
  `ext:` bus channel receives no `ctx` (`events.py:483-495`), and the runner's
  `ctx` is session-wide (`runner.py:200`, `:281`). So the call is `api.suspend`,
  which finds its site, cursor and policy deadlines in the dispatch-scoped
  context (§3.2). `ctx.suspend` is an alias where a `ctx` exists.
- **The spec is the Ask spec** (`validate_ask_spec`, `extension_types.py:409`),
  plus `evidence` and `targetId`. Every head that renders an ask already renders
  this. Tau Code estimates half a day for it, reusing `RequestPanel` and its
  field renderers.
- **`evidence` is plain data:** a list of `{label, text}`, rendered as text. There
  is no span highlighting and no scroll-to-entry, in v1 or after (John,
  2026-10-24).
- **`targetId` is named for the JMFTS store.** It is in tau-jmfts
  `_CROSS_REF_FIELDS` (`store.py:158`), so a JMFTS fork remaps it.
- **`abort` is an action like any other** to the extension. When the human picks
  it, core also trips the turn's abort signal (§3.6).

### 3.2 Where it may be called: never while an entry is held unwritten

**The rule:** `api.suspend` is permitted only at a point where everything the
turn has produced so far is already in the tree. A suspension must never hold
an entry that exists only in memory. So a process that dies, or deliberately
exits (§3.8), loses nothing. The turn then resumes from the tree (§3.7).

That rule rejects three hooks that run *before* their entry's write: `input`
(the prompt), `tool_result` (the tool's output) and `annotate` (#82, any message).
A suspension in any of them would park an unwritten entry. The permitted sites
are:

| site | when | the tree holds |
|---|---|---|
| `tool_call`, top-level calls only | before a tool runs | the assistant entry with its calls, and the results of earlier calls in the batch |
| `entry_written` (new) | immediately after any message entry is durably written: user, assistant, toolResult, custom | everything up to and including that entry |
| mutating `turn_end` | before the next sample | the whole turn so far |
| `user_turn_end` | end of the prompt | the whole prompt |
| a command handler | between turns; `ctx.ui.form`'s case (§3.10) | the path at the cursor; in-process only (§3.7) |
| an `ext:` channel handler | when reached from one of the above | as for that site |

**A nested tool call is not a site.** Code mode's inner calls go through
`prepare_tool_call` with `parent_tool_call_id` set (`tool_pipeline.py:532`, from
`codemode/extension.py`), so the `tool_call` hooks run for each of them. A
suspension there would sit inside an executing outer tool: the outer call's
result is not in the tree, and the code-mode program is mid-run in a child
process. That breaks the rule above, no continuation could resume it, and a
`-p` exit would unwind through code mode's `except BaseException`
(`extension.py:380`) and lose the program (tau-search, round 5). So a `tool_call`
with `parent_tool_call_id` refuses `api.suspend`, and its hits are carried, like
any non-site hit, to the outer call's result `entry_written`.

**`entry_written` is a new awaited hook.** It receives `{kind, entry_id, message,
blocks}` after the store write returns. It cannot change the entry. It is the
per-entry stepping point, and it replaces stepping at `annotate`.

- For an assistant message it runs after the finalize and before tool
  execution. The provider stream has ended by then.
- **It fires once a whole write group is on disk** (independent review, round 2,
  N2 and N1). Some entries are written together in a loop, and a suspension at
  entry *i* of a group would hold *i+1…* in memory, which breaks the rule. So
  `entry_written` is dispatched for each entry of a group, in order, only after
  the group's last write returns. The groups are:
  - the prompt batch: seed, pre-user nodes, the user entry, queued and
    post-user nodes (`agent_session.py:4178-4183`);
  - one steer drain, which empties its queue before writing (`agent_loop.py:736-757`);
  - the nodes one `turn_end` dispatch injects (`agent_loop.py:810-814`), and
    likewise `user_turn_end`'s;
  - **a parallel tool batch.** Results are flushed in order inside `gather`
    (`_flush_prefix`, `agent_loop.py:~1390-1424`). When result *k* is written,
    calls *k+1…n* may already have finished, with their results waiting
    unwritten in memory. So in parallel mode, results' `entry_written` waits
    until every result of the batch is written. No tool in the batch can then
    be finished and unrecorded while a human reads. In sequential mode each
    result is its own group, because the next tool has not started;
  - every other write is a group of one.
- A break that a producer detects at a non-permitted site is carried to the
  next permitted one. Examples: tau-router's model change at `input`, or a
  condition read in `tool_result`. `api.can_suspend_here()` tells the handler
  which case it is in. The debugger queues the hit and suspends at the next
  site, normally that entry's `entry_written`.

**The permission is a dispatch-scoped token.** The runner places a mutable
token in a context variable for one dispatch, and closes it when that dispatch
returns.

- `contextvars` propagate through `await`. So an `ext:` bus handler, which a
  producer calls from its own hook (`await api.emit(...)`), sees the open token.
  That is how the debugger parks on a hit (§5.2).
- A task spawned from a hook (`asyncio.create_task` copies the context) holds
  the same token. The token is closed once the hook returns, so a later
  `api.suspend` from that task is refused.
- An `EventBus` notify dispatch resets the variable to `None` before it calls its
  handlers. A notify event emitted while a permitted hook runs therefore does not
  inherit the permission.

**Refused everywhere else, with a named error.** That includes every notify event
(`message_update`, `message_end`, `tool_execution_*`, `provider_retry`).
Several are emitted while a provider stream is open, and a parked handler would
hold the stream open (`events.py:315-322`).

### 3.2.1 Concurrent suspensions on one cursor

In a parallel tool batch the `tool_call` hooks run in the sequential prepare loop
(`agent_loop.py:1371-1374`), before any tool starts. Results' `entry_written`
waits for the whole batch (§3.2), so in a batch the human reviews results after
every tool has finished. Two suspensions can still be raised close together on
one cursor, for example from two producers' `ext:` handlers in one dispatch.

- **Suspensions on one cursor are serialized.** A second `api.suspend` on the
  same cursor waits FIFO until the open one resolves. Heads show it as queued.
- **In a parallel batch, a step at a result reviews; it does not pace
  execution.** Every tool in the batch has run by then.
- **To step execution strictly, break at `tool_call`.** All of a batch's
  `tool_call` suspensions happen before any of its tools starts. Core does not
  force sequential execution, because that would change behaviour whether or not
  anyone is stepping.
- **An abort resolves every queued suspension** on that cursor with
  `action: "abort"`, not only the open one. A queued suspension must not open
  afterwards and ask a human about a turn that has already ended.

### 3.2.2 `TurnSuspended` must reach the loop

Under a deferred responder (§3.8), `api.suspend` raises `TurnSuspended` to
unwind the turn. Handlers between the suspension and the loop catch exceptions,
and each could swallow or rewrite it (independent review, round 2, N6). So:

- **`TurnSuspended` is a `BaseException`.** No `except Exception` catches it.
- **The rule for every `except BaseException`:** re-raise `TurnSuspended`
  untouched, before any other handling. Two sites map it rather than re-raise:
  - `agent_loop.py:417` (and `:604` in `run_continue`) emits `agent_end` with
    `end_reason: "suspended"` and writes nothing else;
  - `submit_threadsafe` (`agent_session.py:3084`), which puts any exception on a
    cross-thread caller's future, resolves the future as accepted with
    `end_reason: "suspended"`, not with a raw `TurnSuspended`.
- **A lint test enforces the rule.** It walks the AST of the five `src` trees
  and fails on any `except BaseException` that does not re-raise
  `TurnSuspended` first or appear on the mapped list. The rule cannot rot when
  a new catch lands. There are 11 such catches today (tau-search, round 5),
  including `agent_session.py:1535`, `codemode/extension.py:380`,
  `_cpython_child.py:155`, `extension_auth.py:259`, `background.py:373`,
  `serve/remote.py:148` and `session_store.py:629`.
- **`except Exception` sites need nothing,** and `asyncio.gather` in the
  parallel batch is not reached, because results' `entry_written` runs after
  the batch (§3.2).

Each mapped site and a sample of re-raising sites are conformance cases (§6.2).
In milestone 1 nothing raises `TurnSuspended`, but the type, the rule and the
lint land then, because the bus path is in-process.

### 3.3 Lifecycle, in-process

1. **Raise.** Core validates the spec and checks four things: the site (§3.2),
   the responder rule (§3.8), the turn's precondition (§3.9), and the calling
   dispatch's policies (§4.3). It mints a suspension id and opens the durable
   side entry (§3.7). It pauses every enclosing policy deadline (§4.3),
   registers a Future, and emits `suspension_open {id, cursor_id, spec,
   queued}`. It waits for its turn in the cursor's FIFO, then awaits the Future
   raced against the turn's abort signal.
2. **Render.** Every head shows it: natively in the TUI and the REPL, and as an
   event over RPC, serve and Tau Code. A client that attaches later gets it from
   the snapshot (§3.4).
3. **Answer.** Any responder sends `answer_suspension {id, action, values}`.
   Core validates `values` against the fields, as `answer_request` does, so a head
   can keep the form up on an error.
4. **Resolve.** The first valid answer resolves the Future, finalizes the side
   entry with the resolution, and emits `suspension_closed {id, resolution}`. A
   later answer to a closed id returns `{"accepted": false, "reason": "already
   resolved", "resolution": …}`, which is not an error. Two racing clients
   therefore converge.
5. **Return.** Deadlines resume, and `api.suspend` returns the resolution.

### 3.4 Snapshot and events

- **Events:** `suspension_open` and `suspension_closed` are `AgentEvent` types,
  so they reach every head: in-process to the TUI and the REPL, as records under
  `--mode json`, on the RPC event stream, and over serve.
- **Snapshot:** serve's `Attached` gains `suspensions: [SuspensionState]`, read
  by the `--connect` client and by Tau Code. RPC gains `get_pending_suspensions`.
- **Side entries on the wire.** Serve announces a side entry as an entry with
  `side: true`, and replicas apply `is_side_entry` (§3.7.2). Otherwise serve's
  `entry_open` (`daemon.py:102`) would leave every replica holding an incomplete
  entry that looks like a branch.
- **The protocol change is additive,** but it needs a lockstep τ and tau-code
  release, because `hello` refuses a different `PROTOCOL_VERSION`.

### 3.5 The answer verb, per head

| head | how a human answers |
|---|---|
| TUI | a modal built from the spec, the same widgets as `ExtensionAskScreen`; it opens by itself, and Esc does not close it without an action |
| TUI under `--connect` | the same modal, fed by `suspension_open` and `Attached.suspensions`, answering with `answer_suspension`. It declares the capability in `hello`. `RemoteBackend._answer_form` (`remote.py:879-891`) is migrated in the same release |
| REPL | field by field, then a numbered action list, as `_collect_answer` does for asks. Ctrl+C at a suspension is `abort`, not "dismiss" |
| `--mode rpc` | `answer_suspension`, `abort` and `get_pending_suspensions` run in a **control lane** that the serial reader dispatches without waiting for the request in flight (§3.6) |
| serve, local and networked; Tau Code | `answer_suspension` as an RPC verb at a session, from any attached client |
| `-p` | `tau -p --answer <action> [--field name=value …]` in a later process (§3.8) |

### 3.6 Locks, and what keeps working

A suspended turn **keeps `Cursor.turn_lock`**. Releasing it would end the
single-writer guarantee for that cursor while its turn is still in flight.

| still works | how |
|---|---|
| answering | `answer_suspension` takes no turn lock |
| abort | trips the turn's signal; the open and all queued suspensions resolve with `action: "abort"`; the loop takes its abort path (below) |
| steer | queued as today; delivered at the next `_deliver_steer`, after the suspension resolves |
| reads, attach, `get_pending_suspensions` | unguarded today |
| other cursors' turns | each cursor has its own lock (`cursor.py:109-110`) |
| `rollback` strategy | it sets abort, which resolves the suspension and releases the lock |

Mutating RPC verbs and session swaps fail after their existing 5 s wait with
`TURN_STILL_RUNNING`, as they do for any long turn.

**RPC's serial reader gets a control lane.** `rpc/transport.py:263` awaits each
line before reading the next. So a command handler waiting in a form, or any
request that awaits a suspension, would block the host's own
`answer_suspension`. That is a deadlock.

The lane opens **only while a request in flight is waiting on an open
suspension**. While it is open, the reader dispatches four verbs without
waiting: `answer_suspension`, `abort`, `get_pending_suspensions` and
`get_state`. At every other moment the reader is strictly serial, exactly as
today. So no ordering a host relies on can change. Tectum's cases are the
reason (tectum-arecibo, round 3):

- A steer `prompt` written before an `abort` still takes effect before the
  abort.
- A `get_state` written after a `prompt` still sees `is_streaming`.

Responses carry their request id, as they already do.

**One core fix comes with this.** Today the tool runs even if the signal tripped
during a `tool_call` hook. In sequential mode the loop checks the signal at
`agent_loop.py:1280`, runs the hooks at `:1293` and executes at `:1335`, with no
check in between. The parallel prepare loop (`:1371-1374`) never checks. An abort
chosen at a `tool_call` suspension must not run the tool. So the loop checks the
signal after the `tool_call` hooks and before execution, in both modes, and
routes to `_aborted_batch`.

**Abort writes nothing new beyond the aborted results.** Every permitted site has
its entries already written (§3.2). An abort at `tool_call` gives that call and
the batch's later calls `ABORTED_TOOL_RESULT`. An abort at any other site ends
the turn on the existing abort path. No `IncompleteEntryError` and no unanswered
call can arise from a suspension.

### 3.7 Durability and resume: one mechanism

The tree is the truth. A suspension is an entry, an answer closes it, and the
loop resumes from the leaf. Three situations use the same code:

- an in-process answer: the Future is live, and the loop simply continues;
- a restart after a crash, in serve or the TUI: the side entry is still open,
  so the suspension is pending again;
- a `-p` process that exited on purpose at a suspension (§3.8).

#### 3.7.1 The side entry

- On raise, core writes a `suspension` entry as a side child of the cursor's leaf,
  with `status: "incomplete"` and `{spec, site, extension, targetId, call,
  continuation}`. At a `tool_call` site, `call` is `{id, effective_args}`: the
  call's id and its arguments after every `tool_call` handler that ran before
  the suspending one. `continuation` is §3.7.3's resume program.
- `extension` keeps forms and debugger breaks apart in the dataset.
- On resolve, core finalizes the entry with the resolution: action, values, and
  who answered.
- **The finalized entry is the verdict record.** The debugger's dataset is these
  entries. They mirror to JMFTS like any other entry.
- **Side writes are new cursor API.** `Cursor.finalize` moves the leaf onto the
  entry it finalizes (`cursor.py:311-313`), and `Cursor.append` moves it onto the
  new entry. Neither is usable here. So the cursor gains
  `open_side(kind, **payload)` and `finalize_side(entry_id, **payload)`. They call
  `SessionLog.append_at(leaf, …)` and `SessionLog.finalize`, and touch neither
  `_leaf` nor `_open`.

#### 3.7.2 One predicate for every walker

`is_side_entry(entry)` in `session_log` is exported from the public API.
`ctx.leaf_id()` is added beside it, so an extension never infers the leaf from
"the last entry" (memory_reflex does today, at `memory_reflex.py:709-713`).

Every walker that counts or follows children calls the predicate:

| walker | effect |
|---|---|
| `default_leaf` (`session_log.py:170`) | skips side entries, as it skips `navigate` |
| `ConversationTree` children and branch counting | leaves them out, so fork pickers and branch indicators do not show a branch per suspension |
| the TUI tree browser, `get_tree` | show a side entry as a mark on its parent |
| HTML export (`export.py:640-690`) | does not count it as a tip, so `max(children, key=newest)` cannot pick it |
| `complete_message_id("in_session")` | does not offer it |
| the JMFTS store's tree derivation; serve replicas (§3.4) | the same rule |

`entries_to_messages` needs no change, because a side entry is never an ancestor
of a later entry. A second side kind later costs one line in the predicate.

**Forks copy finalized side children, and refuse an open one.** A fork at a
point copies the path (`session_store.py:461`, `daemon.py:1157`). It also copies
the **finalized** side children of each copied entry, so verdicts and mode
changes on a shared prefix survive. #84 does the same for tau-jmfts's `at=`
fork. An **open** suspension is never copied. A fork whose point has an open
suspension side child is refused, naming it, because both sessions could
otherwise resume the same calls (independent review, round 2, N8).
`daemon._fork` checks only the path today (`daemon.py:1158`).

#### 3.7.3 Resuming from the tree

A pending suspension with no live Future is one whose process died or exited.
Answering it starts a resume. A resume is **written down at raise time, never
inferred from the leaf.** The first design inferred it from the leaf's entry
kind, which the loop does not support. A custom node is followed by a sample
in one case, by a `break` in another (a `turn_end` injection on a text-only turn,
`agent_loop.py:315-326`), and by nothing at all in a third (`user_turn_end`, an
idle `send_message`) (independent review, round 2, N3).

**The continuation.** At raise, core records in the side entry exactly where the
loop stands:

```json
{"phase": "dispatch:entry_written", "group": ["e1", "e2"], "index": 0, "handler": 2,
 "turn_index": 3, "pending_calls": ["call_7", "call_8"],
 "repeat": {"prev_batch": "…", "count": 1}, "pending_abort": null,
 "then": "tools"}
```

| field | holds |
|---|---|
| `phase` | the dispatch that suspended: `dispatch:tool_call`, `dispatch:entry_written`, `dispatch:turn_end`, `dispatch:user_turn_end` |
| `group`, `index`, `handler` | for `entry_written`: the write group, the entry within it, and how far the dispatch had got |
| `pending_calls` | the batch's calls with no result in the tree, in order |
| `turn_index` | the loop's own counter, which the tree cannot rebuild, because steer and follow-up user entries look alike there (N9) |
| `repeat` | `prev_batch` and `repeat_count` (`agent_loop.py:~385-397`), the repeat-tool-call limit's state (N9) |
| `pending_abort` | a turn abort already decided but not yet taken, such as an `annotate` failure (ENTRY-ANNOTATIONS.md §4.3) (N5) |
| `then` | what follows the dispatch: `tools`, `sample`, `break`, `end_prompt` or `nothing` |

**Resume runs the continuation:**

1. **Claim** the suspension (below), and finalize its side entry.
2. **Re-dispatch the suspended phase in full,** with `event["resumed"]`. The
   replay rule (below) gives the suspending handler its stored answer, so the
   dispatch reaches every handler, including those after the suspending one
   (N4). `entry_written` continues through the rest of its group.
3. **Honour `pending_abort`** before anything else runs. If an `annotate`
   failure had already doomed the turn, its pending calls get aborted results,
   and the turn ends in error, as it would have without the break. A break
   never lets a vetoed call run (N5). `entry_written` therefore runs before that
   abort takes effect, in-process too.
4. **Then `then`:**
   - `tools`: run `pending_calls` through the normal tool path. Their
     `tool_call` hooks run again with `event["resumed"]`. Each call's effective
     arguments are compared with the stored `effective_args`. A mismatch refuses
     the resume, naming the call, because a non-deterministic patch would
     otherwise run arguments the human never saw. The human may then answer
     `abort`. In parallel mode, `pending_calls` includes calls *1…k−1*, which
     were prepared but whose hook outcomes existed only in memory.
   - `sample`, `break`, `end_prompt` or `nothing`: exactly what the loop would
     have done next.
5. On `abort` at any phase, every call in `pending_calls` gets an aborted result.

| site | `then`, by case |
|---|---|
| `tool_call` | `tools` |
| `entry_written`, user or prompt batch | `sample` |
| `entry_written`, assistant with calls | `tools` |
| `entry_written`, assistant text only | `break`, then `turn_end` and the end of the prompt |
| `entry_written`, a result | `tools` while calls remain, otherwise `turn_end` then `sample` |
| `entry_written`, a node from `turn_end` | `sample`, or `break` on a text-only turn (`agent_loop.py:315-326`) |
| `entry_written`, a node from `user_turn_end` or an idle `send_message` | `nothing` |
| `turn_end` | `sample` or `end_prompt`, as the turn stood |
| `user_turn_end` | `end_prompt` |
| a command handler (form) | not resumable: see below |

**A form is not resumable across processes.** A pending form whose process died
is closed with `action: "abandoned"` and a session warning, and the user runs
the command again. A command is a flow in a coroutine, and its state is not in
the tree. Under `-p`, a deferred responder does not count for forms, so a form
there takes the headless policy (§3.10) and is never suspended.

**A resume is a submission.** It goes through admission like any turn, with
`submission_start` and `submission_end` (the `--connect` client waits on the
latter, `remote.py:855-858`). Its provenance names the suspension, and the
answering head's client. Its `allow_user_input` is the answering head's own:
true for a TUI or REPL, and for `-p` the deferred-responder rule of §3.8.

**Resume fires `turn_resume`, not `session_start`.** A TUI answering a `-p`
suspension already fired `session_start` when it loaded the session
(`agent_session.py:1761-1792`). Firing it again would run setup twice, such as
nats_bus's subscription or a logger's file (N7). So `session_start` stays
process-start only, and a resume dispatches `turn_resume {suspension_id,
continuation}`.

**`turn_resume` is an awaited, mutating hook, not a lifecycle notice.** It runs
with `(event, ctx)` after the claim and before the first resumed step, under
each handler's policy like any other hook. A failure is not swallowed, as a
lifecycle event's would be (`_emit_lifecycle_one`). `ctx.set_model` and
`set_reasoning` are permitted, and a change applies to the first resumed
request. The reason is tau-router (round 5): a `-p` exit 3 can sit for days,
and in that time the provider's budget may be spent, the model held (429) or
the key revoked. Two resume paths go straight to a sample (a user entry's or a
node's `entry_written`) without passing `input` or `turn_end`, where the router
routes. So the router re-checks its rung at `turn_resume`, and steps down or
halts with its usual message, before any request is spent. An extension that
keeps state rebuilds it from the path at `session_start` (a fresh process) and
adjusts at `turn_resume`.

**A pending suspension blocks new prompts at its cursor, on every head.** After
a crash or a `-p` exit 3, the leaf can be an assistant entry with unanswered
calls. A new prompt there would send an unanswered tool call to the provider,
which rejects it. So `submit` at a cursor whose leaf has an open `suspension`
side child is refused, naming the suspension, in the TUI, the REPL, RPC, serve
and `-p` alike (tau-search, round 3). This is the same refusal a lock gives
(`agent_session.py:3592-3600`). It ends when the suspension is answered. To
abandon a suspension, answer it with `abort`: every pending call then gets an
aborted result, and the tree is valid again.

**A claim is atomic, across processes too.** In-process, first-answer-wins
rests on the Future. Across processes it rests on a lock:

- **One lock per session, held while a process hosts it.** A process that opens
  a file-store session for writing takes an exclusive `fcntl.flock`, without
  blocking, and holds it until it closes the session. That covers every hosting
  head: the TUI, the REPL, RPC, `-p` and serve. A session swap in a warm process
  (`new_session`, `switch_session`, a runtime swap) closes the old session, and
  so releases its lock. Without that, each Tectum reset (`new_session`,
  `tau_node.py:712`) would leak one held descriptor (tectum-arecibo, round 5). A second process may still open
  the session read-only, and every submit or answer from it is refused, naming
  the holder.
  - So a TUI holding a session that a `-p --answer` tries to resume refuses the
    `-p`, and the TUI's replica never goes stale behind its back. The answer
    belongs in the TUI.
  - The earlier per-turn lock left that gap (independent review, round 2).
- **The lock is on a sidecar file,** `<session>.lock`, not on the log. A store
  path that ever replaced the log file (write-temp-and-rename, a compaction
  rewrite) would leave a lock on the log stranded on the old inode (tau-router,
  round 4).
  - The sidecar outlives the process. That is harmless, because a `flock` dies
    with its holder, and an existing sidecar never means "held".
  - Delete, rename, move, fork and export handle it with the log. The catalog
    ignores it, since it globs `*.jsonl` (`session_store.py:734`).
  - Holding the lock while hosting changes visible behaviour: two TUIs on one
    session become one writer and one read-only replica. The release notes say
    so (tau-search, round 5).
- **One descriptor per session per process.** `flock` locks belong to the open
  file description. A second `open()` and `flock` on the same file in the same
  process fails, exactly as in another process. So the process keeps one
  registry of lock descriptors, keyed by the sidecar's resolved identity
  (`st_dev`, `st_ino`, not the path string, so symlinks and relative paths
  collapse). Every `Session` object and every cursor shares that entry, with a
  count (tau-search and tau-router, round 4). Two cursors in one process, such
  as memory_reflex's `spawn_branch` lane overlapping the main voice turn
  (tectum-arecibo, round 4), never refuse each other. Inside one process, turns
  keep using `Cursor.turn_lock` and the in-process rules, as today.
  - The descriptor is opened `O_CLOEXEC` (Python's default, PEP 446), so a tool's
    subprocess never inherits it. One test asserts this.
- **The claim.** The answering process, which holds the lock, re-reads the log.
  If the side entry is still incomplete, it finalizes it and owns the resume.
  Otherwise it gets `already resolved`, exactly as in-process. Today nothing
  locks the file store across processes (`session_store.py`, `session_log.py`
  and `headless.py` have no `flock`, `fcntl` or `O_EXCL`), and
  `finalized_entry`'s refusal (`session_log.py:118`) sees only lines already
  loaded. Without the lock, two `-p --answer` runs would both finalize, and both
  would run the pending calls, so tools would execute twice.
- **Where the lock cannot be taken, τ fails loudly.** `fcntl.flock` is POSIX and
  advisory, and it is unreliable on some network filesystems. If the lock call
  fails, or is unsupported on the platform, opening the session for writing is
  refused with that reason. It is never skipped silently. The platform matrix
  states which platforms the lock covers.
- **A session that `tau serve` hosts.** The daemon holds the lock while it hosts
  the session, so a `-p --answer` against it is refused and names the daemon's
  socket: answer through serve. The daemon stays the single writer.
- **JMFTS store.** It offers no cross-process lock, and John ruled out schema
  changes. So in v1 a JMFTS-backed session is resumed only through a single
  writer: serve, or the one process that holds it. A `-p --answer` from a
  second process is refused with that reason.

**A re-dispatch replays the answer; it never asks twice.** A resume re-runs
the hooks of the site that suspended, and those hooks may emit the same hit
again. Take tau-router, which emits `debug_hit` from `turn_end` after a model
change. The debugger's `ext:` handler cannot see `event["resumed"]`. Without a
rule it would open a second suspension for the same change, and under `-p`
that is an exit-3 loop (tau-router, round 3). So core makes the replay
idempotent:

- The resume puts the resumed suspension's `{id, site, extension, resolution}`
  into the dispatch context, which `ext:` handlers inherit like the §3.2 token.
- During that re-dispatch, the first `api.suspend` from the same extension at
  the same site returns the stored resolution. It opens no new suspension and
  writes no new entry. At a `tool_call` site the match also includes the call:
  the key is `(extension, site, call)`, with the call id the side entry stores
  (§3.7.1). Otherwise a parallel resume, which re-dispatches calls *1…k−1*
  before call *k*, would hand call *k*'s answer to call 1 (tau-router, round 4).
  Calls *1…k−1* suspend normally if they hit. At an `entry_written` site the key
  also includes the entry. Other sites keep `(extension, site)`.
- Any later `api.suspend` in the same dispatch opens a new suspension as usual,
  because it is a genuinely new break.
- `api.resumed()` exposes the same record to any handler that wants to know.
  No producer or debugger has to remember anything to be correct.

**Why `turn_end` and `user_turn_end` dispatch again in full.** Messages returned
by `turn_end` handlers are appended only after every handler returns. So after a
crash none of that dispatch's returns are in the tree. Dispatching again is
therefore correct, but a handler with side effects outside the tree sees the
event twice. `event["resumed"]` lets it tell.

**What is lost across processes, and how each piece is rebuilt:**

| in-memory state | across a process boundary |
|---|---|
| model, reasoning, turn settings | rebuilt from the `config` entries on the path (CURSORS.md) |
| `turn_index`; the repeat-call limit's `prev_batch` and `repeat_count` | stored in the continuation (N9) |
| a decided but untaken abort | stored as `pending_abort` (N5) |
| how far a dispatch and a write group had got | stored as `phase`, `group`, `index`, `handler` (N3, N4) |
| system prompt, tools | rebuilt from config, as on any reload |
| abort signal, retry state, policy deadlines | fresh |
| answered calls in the batch | not re-run; their results are in the tree |
| steer, follow-up and `nextTurn` queues | **lost**, never in the tree. After a crash the resume warning says so. A deliberate `-p` exit has none, because `-p` has no steer channel |
| debugger hits carried from a non-site and not yet shown | **lost** on a crash; nothing had been asked yet. On a deliberate `-p` exit there are none: every pending hit is combined into the suspension that triggers the exit (§5.2) |
| extensions' in-memory state | the extension's job: rebuilt from the path at `session_start` in a fresh process, adjusted at `turn_resume` |
| the provider's prompt cache | cold, unless the resume falls inside the provider's cache TTL. That costs money, not correctness |

### 3.8 Responders, live and deferred

A suspension is raised only if something can answer it.

**A live responder** answers in-process. It counts only while
`user_input_permitted()` is true for the submission that drives the turn
(`extension_types.py:575-590`, default `False`, `submission.py:147`). A turn
submitted by a timer, a bus message or an extension's `api.submit` has no live
responder unless its submitter set `allow_user_input`. The live responders are:

- the TUI and the REPL, while bound;
- an RPC host, once it calls the new verb `declare_capabilities
  {suspensions: true}`, because RPC has no handshake today;
- a serve client whose `hello` declares the capability. `hello` is per
  connection, so the count is the attached connections that declared it.

**A deferred responder** answers in a later process. `-p` is one, for its own
prompt only. When a suspension is raised under `-p`:

1. Core writes the open side entry, as usual.
2. `api.suspend` raises `TurnSuspended`. It unwinds the loop without writing
   aborted results or a `turn_error`, so the tree stays exactly at the
   suspension point. `agent_end` carries `end_reason: "suspended"`.
3. `-p` prints the suspension and exits with a **distinct exit code, 3**, which
   means "suspended". It is neither success (0) nor error (1). Text mode prints
   the title, evidence, actions, session id, suspension id, and the command that
   answers it. `--mode json` prints one `{"type": "suspension_open", …}` record.
   (3 is proposed: the build checks that no current `-p` path exits 3.)
4. A later process answers and resumes:
   `tau -p --session <id> --answer step [--field verdict=fine] [--field note=…]`.
   It resumes by §3.7.3 and runs until the next break, which exits 3 again, or
   until the turn completes.
5. `--answer` is required while a suspension is pending. Without it, `-p`
   prints the pending suspension again and exits 3, so a script loop sees one
   code for "a human is needed". It never continues silently.
6. Any other head can answer the same suspension. A `-p` session suspended at
   exit 3 can be answered from the TUI, from `tau serve` with an attached client,
   or from Tau Code. The answering head resumes the turn. That holds for an
   *orphaned* suspension. A *live* one is held by its own process, under the
   §3.7.3 lock, so another process's `-p --answer` is refused, naming the
   holder. A live suspension is answered in the head that is waiting, or through
   serve if serve hosts it (tau-router, round 4).

**Where nothing can answer,** `api.suspend` raises `NoResponderError`:

| situation | behaviour |
|---|---|
| `--mode rpc`, host did not declare `suspensions` | `NoResponderError`. This is right for Tectum's voice nodes: nobody sits on that channel, and a wait would hang the voice turn until its outer timeout (tectum-arecibo) |
| a live head, submission without `allow_user_input` | `NoResponderError` |
| serve with zero attached responders | the suspension **waits** and does not default. `list_sessions` shows the cursor as suspended, a session warning says it is waiting for a client, and the first capable client to attach gets it in its snapshot. Under serve, `can_suspend()` is therefore always true |
| `ui.form` with no responder | the headless policy applies, unchanged (§3.10) |

### 3.9 Refusing a turn that cannot be stepped

An armed debugger whose config came back from the tree must not discover
mid-turn that nobody can answer. If it did, the failure would differ by site: a
blocked call at `tool_call` (`runner.py:289-313`), an error elsewhere. So the
check happens before admission:

- The `input` hook gains a return form, `{"refuse": "<reason>"}`. `submit` then
  returns `accepted=False` with that reason, and no entry is written.
- The debugger returns it when stepping is armed at the cursor and
  `api.can_suspend()` is false. The reason names the alternatives: attach a
  client under serve, declare the capability, or set `allow_user_input`.
- Its config can choose `unsteppable: run` instead. The turn then runs
  unstepped, with a session warning saying so. That is an explicit choice, not
  a silent skip.
- **`store_history` off is unsteppable too.** With nothing persisted,
  `entry_written` never fires, and stepping would silently stop. So an armed
  debugger refuses such a turn at `input` in the same way.

### 3.10 Relation to `request_user_action` and `ctx.ui.form`

- **`request_user_action` stays,** for a different job: a durable gate on the
  next turn. It shares the Ask spec and the head renderers.
- **`ctx.ui.form` is rebuilt on `api.suspend`,** with the actions `submit` and
  `cancel`. That removes `ServeUI.form`'s private Future and its `request`/`Answer`
  path, and with it the defaults serve fabricates when nobody is attached. Forms
  then work in RPC too.
  - **The serve verb `answer` and the `request`/`request_closed` events are
    removed** in the same release, in favour of `answer_suspension` and
    `suspension_open`/`suspension_closed`. Two clients use them today: Tau Code
    and the TUI under `--connect` (`remote.py:879-891`). Both are migrated in
    that release. The tests that pin the old path change with it:
    `test_serve.py:274-283`, `test_serve_schema.py:300`, `:401`, and
    `test_headless_submission_source.py:260`.
- **A form usually suspends outside a turn,** from a command handler collecting
  an argument. There it takes no lock of its own: it holds `_drive_turn`'s lock
  for a command typed as a prompt, and nothing for a panel button or a chord
  (`run_extension_command`, `agent_session.py:5459-5491`). It offers no `abort`,
  and `cancel` resolves it with `None`. The headless policy
  (`--ui-defaults form=defaults`, or `HeadlessDialogError`) is kept for heads with
  no responder. A flow collecting a command argument may take defaults; a
  step-debugger break may not.

### 3.11 Who may answer over networked serve

Any client that passed `hello` may answer, the same rule as every other write
verb today. Per-client roles are not added: τ's threat model is scrappy, and
tokens and locks are opt-in. The resolution records `client_id` and the
`hello.client` label, so verdicts can be attributed to a connection.

## 4. Handler policies (#83): an open mechanism

John wants this extensible, and he will not fix a set until he has tinkered.
**The API is experimental**: names and defaults may change below 1.0.

### 4.1 A policy is a protocol

```python
class HandlerPolicy(Protocol):
    async def run(self, call: Callable[[], Awaitable[Any]], site: PolicySite) -> Outcome: ...

# Outcome = Returned(value) | Failed(error) | Proceeded(record)
# PolicySite: hook name, extension name, deadline(seconds) -> PausableDeadline, warn(text)

api.register_policy("gating", Bounded(timeout_s=30, on_timeout="fail", on_error="fail"))
api.on("tool_call", handler, policy="gating")
```

- **A policy decides how a handler is run and what its failure becomes.**
  Retry, fall back to another decider, or proceed-and-record are each a new
  class, with no core change.
- **The hook site interprets the outcome:**
  - **`Failed`** means what failure means at that hook. A `tool_call` blocks
    the call (it already fails closed, `tool_pipeline.py:297`). `annotate`
    writes the error ext and aborts the turn (ENTRY-ANNOTATIONS.md §4.3).
    `turn_end`, `tool_result` and `entry_written` end the turn with the error.
  - **`Proceeded`** continues as if the handler returned nothing, and its
    `record` is kept where the site has a place for it. At `annotate` that is
    `ext[name] = {"error": …, "proceeded": true}`, so a proceeded failure is never
    read as a clean pass in the dataset. Elsewhere it becomes a session warning.
- **`ext:` channel handlers run under policies too.** A `Failed` outcome raises
  `HandlerPolicyError` into the emitter's `await api.emit(...)`. A `Proceeded`
  outcome returns normally and raises its warning. A handler with no policy keeps
  today's failure isolation. Its exception is surfaced, and `emit` does not
  raise.
- **Enforcement is a cancel scope.** Each policed handler runs in its own task.
  A watchdog cancels that task when its `PausableDeadline` expires, and the
  dispatch token (§3.2) closes when the task returns. A deadline resumed after a
  pause, already past its time, fires immediately.

### 4.2 Built-ins

| name | class | timeout | on timeout | on error | for |
|---|---|---|---|---|---|
| `gating` | `Bounded` | 30 s | `Failed` | `Failed` | a decider or check whose answer gates the step |
| `advisory` | `Bounded` | 15 s | `Proceeded` | `Proceeded` | a recorder or monitor whose absence must not stop work |
| `human` | `Unbounded` | none | — | `Failed` | a handler that suspends for a human (§3) |

John set these numbers (2026-10-24). They should become faster eventually, but
an error because a model is slow is a punishing design, so they start generous.
A handler that names no policy behaves exactly as today. Landing this changes no
existing extension.

As one example of a non-built-in, tau-router proposes that a failed decider pull
should fall back to base routing and set a warning. That is tau-router's
proposed behaviour, not a ruling.

### 4.3 How policies and suspension meet

A suspension happens inside nested dispatches. The debugger's `debug_hit`
handler runs inside the producer's hook, and the producer's handler may be
`gating`. If the producer's 30 s clock kept running, it would cancel the
suspension while the human was still reading. Under `Proceeded`, the turn would
then continue as if nothing had happened: a fabricated "continue".

**The rule: every enclosing deadline pauses from the moment `api.suspend` is
called, until it returns.** That includes time spent queued behind another
suspension on the cursor (§3.2.1). So a timeout measures a handler's own work,
never a human's reading time and never the queue.

- An inner timeout is capped by the outer one, for every wait except a
  suspension.
- A producer may register its hook under any policy and still emit `debug_hit`.
- The debugger registers its handlers with `policy="human"`.

The alternative was to refuse `api.suspend` under any enclosing timeout
(tau-router, round 1). That forbids a `gating` decider from ever producing a
break, and decider questions are §5.3's main external producer.

## 5. The debugger extension (`tau-debugger`)

### 5.1 Home and dependencies

- **Repository:** `~/Development/tau-debugger`, MIT, created when the build
  starts.
- **It depends on** the τ release that ships §3 and §4.
- **It contains no head code.** Everything it shows goes through `api.suspend`,
  `ctx.ui.set_status` and its commands.

### 5.2 Break conditions over the bus

Producers announce conditions and emit hits on their own `ext:` channels
(`extension_types.py:57`). The debugger only consumes them.

- **Conditions are pulled.** The debugger emits `ext:tau_debugger:need_conditions`
  when a turn starts with stepping armed, and on `/break list`. Each producer
  answers on `ext:<producer>:debug_conditions` = `[{name, describe}]`, which
  feeds the break-set picker.
  - A push at `session_start` would miss `/new`, fork and resume, where
    `session_start` does not fire in 0.14, and it would depend on load order.
  - This is the same request-and-reply idiom as tau-router's decider pull.
- **A hit** is emitted on `ext:<producer>:debug_hit` = `{condition, evidence,
  targetId?}`.
- **Pending hits are combined.** Hits carried from non-sites, and any further
  hits for the same site, are combined into **one** suspension at the next
  permitted site, whose evidence lists them all. A second hit for the same site
  is never dropped. Because the combined spec is in the suspension's side entry,
  a `-p` exit persists all of them.
- **The producer contract:** emit `debug_hit` from a hook, never from a notify
  event or a task the hook spawned. Any policy is allowed, because its clock
  pauses during the suspension (§4.3). At a permitted site (§3.2) the debugger
  suspends inside the emit. At any other hook it queues the hit for the next
  permitted site.

The `all` channel does not receive `ext:` channels (`events.py:483-495`). So the
debugger subscribes to each producer by name, from its config.

### 5.3 Built-in deterministic conditions

These need no decider:

| condition | read at | suspends at |
|---|---|---|
| every step: each entry written, and each tool call | `entry_written`, `tool_call` | the same |
| a tool, or a named tool | `tool_call` | `tool_call` |
| a tool error (`is_error`; any nonzero bash exit since #88) | `entry_written`, toolResult | the same |
| a risky bash command; `git commit`/`push`/`reset` | `tool_call`, regex over `command` | `tool_call` |
| a write or edit to a test file, or outside an allowlist | `tool_call`, the `path` argument | `tool_call` |
| an edit diff that removes asserts | `entry_written`, toolResult `details.diff` | the same |
| the same call repeated N times; the same error text repeated | `tool_call` / `entry_written`, from the path | the same |
| oversized tool output | `entry_written`, toolResult, in characters | the same |
| a text-only completion; an assistant claim regex ("all tests pass") | `entry_written`, assistant | the same |
| N completions with no file edits; a context-size threshold | `entry_written`, assistant (`usage`) | the same |

Conditions from other producers:

- **todo** created, changed and completed come from `examples/38_todo.py`,
  which gains three emits.
- **decider questions** (`q:<id>`) come from the shadow extension.
- **a model change** comes from tau-router, as one condition, `model_change`,
  with `source` as a filter field. It emits after `set_model` succeeds, from
  `input` and `turn_end`, with evidence `{from, to, why, source: route |
  decider_lease | step_down}`. A hit at `input` is carried to the user entry's
  `entry_written`. A human `/model` is not a router hit. Refusal holds (429/401)
  appear only on notify events, so they are status-only.

Not in v1: compaction and provider retries, which are visible only as notify
events, and shown in the status line instead; and `spawn_branch`, which has no
event. A delegate tool is caught as a named tool.

### 5.4 Stepping semantics

- **step:** suspend at the next permitted site, then stay in step mode.
- **step over:** run until a condition in the selected break set fires.
- **continue:** run without stopping, until stepping is armed again.
- **abort:** trip the turn's signal (§3.6).

The break set is edited in the suspension's own fields (a multiselect), and with
`/break add|rm|list` between turns.

### 5.5 Where the mode lives

The debugger's effective config at a point in the tree is a fold over the path:

- each `tau_debugger.config` customEntry on the path, written by the arming
  command between turns;
- then each resolution of a `tau_debugger` suspension hanging off an entry on
  the path, read through `is_side_entry` children. A choice made at a
  suspension, such as "continue" or an edit to the break set, applies from that
  point on.

Because both live in the tree, and forks copy side children (§3.7.2):

- a fork carries the mode;
- navigating back before the arming entry disarms;
- a restart, or the next `-p` invocation, honours the same conditions without
  being told again.

The debugger also rebuilds its counters from the path (repeated calls, edits
since the last completion). So nothing it decides with depends on surviving
memory.

### 5.6 Notes and verdicts

- **At a suspension:** the note and the verdict are the resolution's values,
  stored in the finalized `suspension` entry (§3.7.1). Its `targetId` names the
  entry looked at.
- **Between turns:** `/note [targetId] <text>` appends a `tau_debugger.note`
  customEntry.
- **Voice:** no τ head captures audio, and Tau Code has no microphone access. An
  OS-level whisper hotkey that types into whichever client has focus fills any
  note field, in every head, and `--field note=…` under `-p`. Nothing in τ or
  tau-debugger changes for it.

### 5.7 Cost when not armed

Each handler returns at once when the config fold says unarmed. The fold is
cached per leaf. Conformance (§6) measures a turn with the debugger loaded and
unarmed against a turn without it.

## 6. Conformance

### 6.1 One scripted session, stepped from every head

The session runs on a faux provider. One prompt yields a completion that calls
`bash` twice, then a text answer. The debugger is armed in `every` mode. The
script answers in order: step, step with a note, step over, continue.

Every head must produce the same tree: the same entries, the same finalized
`suspension` entries with the same resolutions, and the same leaf.

| head | driver |
|---|---|
| TUI | Textual `pilot`, pressing the modal's actions |
| TUI under `--connect` | `pilot`, against a local `tau serve` |
| REPL | a pty driver answering the field prompts |
| `--mode rpc` | a JSON driver: `declare_capabilities`, then reading `suspension_open` and sending `answer_suspension` |
| serve, local | the τ client library over the unix socket |
| serve, networked | the same, over loopback TCP with `serve.token` set |
| Tau Code | tau-code's own e2e suite against a pinned `tau serve` (in that repository) |
| `-p` text and json | a loop: run, expect exit 3 and a printed suspension, answer with `--answer`, repeat, until exit 0 |

### 6.2 The edge cases

- two clients answer at once: one wins, the other gets `already resolved`, and
  both heads close the suspension;
- a client disconnects and reattaches during a suspension: it sees it in
  `Attached.suspensions`;
- serve with no client attached: the suspension waits, and the first client to
  attach answers it;
- abort at a `tool_call` suspension: that call and the later calls get aborted
  results, and no tool runs;
- abort with a second suspension queued: both resolve as `abort`, and the queued
  one never opens;
- two result `entry_written` suspensions in one parallel batch: the second is
  queued until the first resolves;
- a `gating` producer emits `debug_hit` and the debugger suspends: the
  suspension outlasts 30 s, and the producer's clock resumes after the answer
  and does not fail the turn;
- a second hit queued behind the first under a `gating` producer: it does not
  time out in the queue;
- a task spawned from a hook calls `api.suspend` after the hook returned:
  refused;
- a notify event emitted during a permitted hook calls `api.suspend`: refused;
- `api.suspend` at `input`, `tool_result` or `annotate`: refused, and the
  debugger carries the hit to the next `entry_written`;
- kill the daemon at each §3.7.3 site, reload, and answer: the turn resumes as
  the table says, and the tree matches an uninterrupted run with the same
  answers. The `tool_call` case runs in both sequential and parallel mode, and
  in parallel mode calls *1…k−1* are dispatched again;
- a session with an open suspension, reopened in the TUI, the REPL, RPC and
  serve: a new prompt is refused, naming the suspension; answering it with
  `abort` makes the tree valid and prompts work again;
- two `tau -p --session X --answer …` processes race on one orphaned
  suspension: one claims it and resumes, the other gets `already resolved`, and
  every pending tool runs exactly once;
- a TUI submits while a `-p --answer` resume is mid-batch: refused by the lock,
  not interleaved;
- on the file store, a `spawn_branch` lane turn overlaps a main-cursor turn in
  one process: neither is refused. A second process submitting meanwhile is
  refused, naming the holder;
- a parallel batch where call *k* suspended, then a resume with the debugger in
  `every` mode: call 1 suspends with a new suspension, and only call *k*
  receives the stored resolution;
- `-p --answer` against a session that `tau serve` hosts: the claim fails and
  names the daemon;
- `-p` exits 3 at each site; `--answer` resumes it; `-p` without `--answer`
  refuses and names the suspension;
- a `-p` suspension answered from the TUI instead: it resumes in the TUI;
- a turn submitted by `api.submit` without `allow_user_input`, while armed:
  refused at `input` with the reason, or run unstepped with a warning under
  `unsteppable: run`;
- an RPC host that never declared the capability: `NoResponderError`;
- an RPC host suspended inside a command's form: its `answer_suspension` is read
  and answered (control lane), with no deadlock;
- RPC with no suspension open: a steer `prompt` and an `abort` written back to
  back start no turn after `agent_end`. A `get_state` written right after a
  `prompt` shows `is_streaming`. The lane stays closed;
- a producer-relayed hit at `turn_end` and at `tool_call`, then a crash or a
  `-p` exit 3, then a resume: the re-dispatch emits the hit again, and the
  debugger's `api.suspend` returns the stored resolution. No second suspension
  is opened, and `-p` does not exit 3 again for it;
- a mode change at a suspension, then a fork from the prefix: the fork keeps the
  mode;
- a fork at a point with an open suspension: refused, naming it; a fork at a
  point with finalized suspensions copies them;
- a write group (prompt batch, steer drain, `turn_end` nodes): a break on its
  first entry suspends only after the whole group is on disk;
- a parallel batch with a break on result *k*: every result is written before
  the suspension opens; a crash or `-p` exit then a resume runs no tool twice;
- `TurnSuspended` at each §3.2.2 site: it reaches the loop, `agent_end` says
  `suspended`, and no `turn_error`, block or swallowed error appears;
- resume of an `entry_written` break: later handlers of that dispatch and later
  entries of the group still see `entry_written`;
- an `annotate` failure on an assistant entry with calls, a break at its
  `entry_written`, then a resume: the calls get aborted results and do not run;
- a `turn_end` node on a text-only turn, a break at its `entry_written`, then a
  resume: the turn ends; it does not sample;
- a tool_call resume whose re-dispatched handler patches arguments differently:
  refused, naming the call;
- a TUI answering a `-p` suspension: `turn_resume` fires, and `session_start`
  does not fire a second time;
- `max_turns` and the repeat-call limit across a `-p` exit: the counts carry
  over from the continuation;
- a TUI holding a session while `-p --answer` targets it: the `-p` is refused,
  naming the TUI; a read-only second open works and refuses submits;
- two `Session` objects over one file in one process (a fork source and a
  reopen): they share one lock descriptor and do not refuse each other; the
  descriptor is not inherited by a tool's subprocess;
- `-p` without `--answer` while a suspension is pending: exits 3 again, printing
  it;
- armed stepping with `store_history` off: refused at `input`;
- two hits for one site: one suspension, listing both;
- after `new_session` in a warm process on the file store, a second process can
  open the previous session for writing;
- a code-mode program calls `web_search` with stepping in `every` mode: no
  suspension inside the program; the hit is carried to the outer call's result
  `entry_written`;
- the `except BaseException` lint fails on a new catch that does not re-raise
  `TurnSuspended`; `submit_threadsafe` resolves a suspended turn as
  `end_reason: "suspended"`;
- suspend under `-p`, exhaust the router's budget, then resume: tau-router steps
  down or refuses at `turn_resume`, before the first resumed request;
- a form from a panel button between turns: no turn lock, no `abort` action; a
  crash leaves it `abandoned` with a warning.

A headless harness, with no Textual, drives the RPC, serve and `-p` rows, so the
matrix runs in the container matrix with the rest of the suite.

## 7. Scope and effort

**v1, in two milestones.**

*Milestone 1, in-process (about 5 days in τ):*

- `api.suspend` and the alias `ctx.suspend`, `can_suspend`, `can_suspend_here`, `api.resumed`,
  the dispatch token, per-cursor FIFO, events, `Attached.suspensions`,
  `answer_suspension`, `get_pending_suspensions`, `declare_capabilities`, and
  the RPC control lane;
- the `entry_written` hook, the `input` hook's `refuse` form, and the
  `tool_call` abort check;
- `Cursor.open_side`/`finalize_side`, `is_side_entry` with every walker in
  §3.7.2, `ctx.leaf_id()`, and forks copying side children;
- §4: `HandlerPolicy`, `register_policy`, `policy=`, pausable deadlines, and the
  three built-ins;
- `ctx.ui.form` rebuilt on suspend; serve's `answer` and `request` removed; the
  `--connect` TUI migrated;
- rendering in the TUI, the REPL, RPC and serve. Tau Code is about half a day,
  in that repository;
- `tau-debugger` (about 1.5 days): conditions, the bus idiom, stepping modes,
  the config fold, notes.

Milestone 1 also takes, because they change its contracts: `entry_written`
per write group, with a parallel batch as one group (§3.2); the `TurnSuspended`
type and its site table (§3.2.2); forks copying only finalized side entries and
refusing an open one (§3.7.2); and the session lock held while hosting, on a
sidecar, with one descriptor per process (§3.7.3).

*Milestone 2, across processes (about 3 days):*

- resume from the tree by the recorded continuation (§3.7.3): re-dispatch in
  full, `pending_abort`, effective-argument check, `turn_resume`, the resume
  as a submission, and the atomic claim;
- the refusal of new prompts over a pending suspension;
- the `-p` deferred responder: `TurnSuspended`, exit 3, the printed suspension,
  `--answer` and `--field`, and refusal without `--answer`.

Milestone 2 is v1, not v1.1. It is the same mechanism as crash recovery, which
v1 needs anyway, and §3.2's site rule was chosen to make it possible. The §6
matrix (about 1.5 days) closes v1.

**Deferred, each with its reason:**

- **notes mid-turn while not suspended.** Storage is not the obstacle: a `note`
  side entry via `open_side` is safe mid-turn. What is missing is an input path
  in every head while a turn runs. Typed text is a steer in the TUI and the REPL,
  and commands are refused mid-turn (`app.py:1055-1065`, `repl.py:1811-1817`,
  `remote.py:1005-1030`).
- **per-client roles and display names over networked serve** (§3.11).
- **breaking on notify events** (compaction, retries, deltas): suspending there
  holds a stream open.
- **resuming a form across processes** (§3.7.3): a command's flow lives in a
  coroutine.
- **panels and statuses in serve's `Attached`:** a real gap that tau-code found,
  but not this record's. It gets its own issue.
- **evidence-span highlighting:** cut by John, not deferred.

## 8. Open

Nothing. John approved the plan and set the policy numbers (§4.2) on 2026-10-24.
He also set `-p` stop-and-resume as the target (§3.8). Its effort puts it in v1
as milestone 2 (§7).

## 9. Review trail

| finding | resolved in |
|---|---|
| round 1, tau-router and tau-search: enclosing timeouts cancel a suspension | §4.3 pausable deadlines |
| round 1, tau-search: contextvar leaks to spawned tasks and notify events | §3.2 dispatch token |
| round 1: `Cursor.finalize` moves the leaf | §3.7.1 `open_side`/`finalize_side` |
| round 1: parallel `tool_result` suspensions | §3.2.1 |
| round 1: per-walker skips | §3.7.2 one predicate |
| round 2, tau-search: abort leaves queued suspensions | §3.2.1, §3.6 |
| round 2, tau-router: pause from the call, including the queue; name the enforcement; an expired deadline fires on resume | §4.3, §4.1 |
| round 2, tectum: read the leaf without "last entry" | §3.7.2 `ctx.leaf_id()`, exported predicate |
| independent B1: RPC serial reader deadlocks | §3.6 control lane |
| independent M1: `allow_user_input` ignored | §3.8 live responder rule |
| independent M2: mode changes inside a suspension do not persist | §5.5 config fold, §3.7.2 forks copy side children |
| independent M3: `--connect` TUI uses `answer`/`request` | §1.2, §3.5, §3.10 migration and tests |
| independent M4: config restored from the tree skips the arm check | §3.9 refusal before admission |
| independent M5: policy vocabulary is closed; proceed at `annotate` writes nothing | §4.1 protocol and `Proceeded(record)` |
| independent M6: queued suspensions do not pause their deadline; cancel scope unstated | §4.3, §4.1 |
| independent m1–m6 | §3.1 `api.suspend`; §3.7.2 walkers and wire; §3.7.1 `extension`; §3.7.2 forks; §3.8 responder registration; §3.7.3 resume replaces the dangling-call warning |
| John: `-p` must step, as stop and resume | §3.8, §3.7.3, §7 milestone 2 |
| independent nits: REPL Ctrl+C; prompt-cache TTL | §3.5; §3.7.3 |
| design change in this revision: no suspension may hold an unwritten entry, so `input`, `tool_result` and `annotate` are no longer sites, and `entry_written` is added | §3.2 |
| round 3, tau-router: a relayed hit replays on resume and suspends twice | §3.7.3 idempotent replay, `api.resumed()` |
| round 3, tectum: the control lane could reorder `abort` and `get_state` against a steer | §3.6 the lane opens only while a suspension is open |
| round 3, tau-search: a parallel `tool_call` resume misses calls prepared only in memory | §3.7.3 resume from the first call with no result |
| round 3, tau-search: nothing stops a new prompt over a pending suspension | §3.7.3 refused on every head until answered |
| round 3, tau-search: two processes can both claim an orphaned suspension | §3.7.3 atomic claim: `flock` and a re-read; serve holds the lock; JMFTS resumes only through a single writer |
| round 4, tau-router: a parallel resume could hand call *k*'s answer to call 1 | §3.7.3 replay key `(extension, site, call)` at `tool_call` |
| round 4, tau-search: releasing the claim lock at finalize opens a window for another writer | §3.7.3 lock held to the end of the resumed turn; every file-store writer takes it |
| round 4, tectum: a per-turn lock refuses a second cursor in the same process | §3.7.3 the lock is per process per session file, counted across cursors |
| round 4, tau-router and tau-search: "any head can answer" vs a live suspension; `flock` on network filesystems | §3.8 point 6; §3.7.3 fail loudly where the lock cannot be taken |
| round 4 build notes, tau-search and tau-router: one descriptor keyed by `(st_dev, st_ino)`; a replaced log strands the lock | §3.7.3 lock registry; sidecar lock file |
| independent round 2, N1: a parallel batch runs tools twice on resume | §3.2 a parallel batch is one write group |
| independent round 2, N2: `entry_written` holds the rest of a write group | §3.2 dispatch after the whole group |
| independent round 2, N3: the resume point inferred from the entry kind | §3.7.3 explicit continuation |
| independent round 2, N4: resume skips the rest of a dispatch | §3.7.3 re-dispatch in full |
| independent round 2, N5: resume runs calls a failed decider should have blocked | §3.7.3 `pending_abort` |
| independent round 2, N6: `TurnSuspended` swallowed or rewritten | §3.2.2 `BaseException` and the site table |
| independent round 2, N7: `session_start` fired twice in a live process | §3.7.3 `turn_resume` |
| independent round 2, N8: a fork copies open suspensions | §3.7.2 finalized only; refuse a fork at an open one |
| independent round 2, N9: state the tree cannot rebuild | §3.7.3 `turn_index` and `repeat` in the continuation |
| round 5, tau-router: a resume can spend on a rung the router would refuse | §3.7.3 `turn_resume` is an awaited mutating hook before the first resumed request |
| round 5, tectum: a session swap must release the old session's lock | §3.7.3 |
| round 5, tau-search: nested code-mode calls fire `tool_call` mid-program | §3.2 a nested call is not a site; its hits carry to the outer result |
| round 5, tau-search: the `except BaseException` table is incomplete and would rot | §3.2.2 a rule, two mapped sites, and a lint test |
| round 5, tau-search: the sidecar's lifecycle | §3.7.3 |
| independent round 2 minors | resume as a submission; forms under `-p` take the headless policy; pending hits combined (§5.2); effective-argument check; `store_history` off refused (§3.9); the session lock held while hosting; `-p` without `--answer` exits 3 |
