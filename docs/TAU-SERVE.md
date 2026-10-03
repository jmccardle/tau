# τ serves its trees: the 0.12.0 plan

Plan and cost record (2026-10-02). M0–M3 and M5 built 2026-10-03; M4 and M6
not written here. This record continues
`docs/CURSORS.md` and replaces two of the records its §11 named: durable writes
(§4 here) and the web head (§6–§7 here). It plans one development pass, which
ends when the scenario in §2 runs.

## 1. What is wrong

- **A turn is written to the log only after it finishes.** `_run_one_turn` writes
  the user message and every loop message after `loop.run` returns, or in its
  `except` (`agent_session.py:3322-3339`). The only exception is a mid-turn
  compaction (`loop_written`, `agent_session.py:3824-3835`). If the process is
  killed during a turn, nothing is left of it: the user's prompt, every finished
  tool result and the partial answer are all gone.
- **A stream that ends without a `DoneEvent` gets a made-up ending.**
  `agent_loop.py:935-947` builds an `AssistantMessage` with `stop_reason="stop"`
  and an empty `Usage()`. The tree then records a normal completion that never
  happened.
- **The agent and the screen share one machine.** The TUI is in-process only.
  `app.py` reaches the live session through `getattr(self.current_backend,
  "agent_session", None)` at seven sites, and through `self._cursor` 26 times.
  The only remote option is tau-code's hub. It relays one `tau --mode rpc`
  process, and therefore one conversation, to several browsers
  (`tau-code/packages/server/src/hub.ts`, its class docstring), using 991 lines
  of TS in `server/` and `runner/`.
- **Two concurrent top-level turns on separate cursors were thought untested.**
  Built note (M0): that was wrong. `test_cursor_turns.py` already ran two user
  turns at once on two cursors. What no test covered was §8's shape: two
  cursors at one leaf, each under its own model, on the file store.
  `test_compare_cursors.py` now does, and it passed with no core change.
- **Protocol counts are written by hand and drift.** Defect 5 of the 2026-10-02
  comparison: ROADMAP and tau-code's ARCHITECTURE.md give RPC verb counts that no
  longer match protocol 1.8.

## 2. Done means this runs

1. Start `tau serve` on a build box. Its terminal prints a line for each cursor
   and turn as it happens.
2. Attach a TUI to it with `tau --connect buildbox`, from another machine.
3. Attach a browser and VS Code (tau-code) to the same session.
4. From one leaf, open two cursors with different models (the demo in §8). Both
   stream at once, and every attached client shows both streams.
5. Run `kill -9` on the daemon in the middle of the turn, and restart it.
6. Every client reconnects and shows the same tree. The interrupted entries say
   they are incomplete. The user prompt and the tool results that finished before
   the kill are still there.

The work in §9 is ordered so that a daemon stack with fewer features can be
examined at each milestone, before the whole of this scenario runs.

## 3. The daemon is the single writer

`docs/HEADS-AND-MULTIPLEXER.md` §5.1 already settled this: attached heads submit
through `AgentSession.submit` and never touch the log, so N heads are N
submission sources, not N writers. That holds unchanged here.

**No cross-process lock.** Two τ processes that open one session file each keep
their own copy of the tree in memory. They append to the file in `"a"` mode, one
line per entry (`session_store.py:548-549`). Each entry's `parentId` is its own
writer's leaf, so the file ends up holding two branches of one tree, and both
show when it is reopened. That is misuse, not corruption ("Well that's one way to
use your free will"), and τ adds no machinery against it. The daemon is how you
get one writer.

**A daemon owns sessions and working directories.** It lists every session in its
store and loads any of them, whatever the cwd. `list_sessions(cwd=None)` already
walks every dashed-cwd directory (`session_store.py:634-655`). When a client
creates a session it names the cwd, which is a path on the daemon's machine. If
that path does not exist there, the create fails.

**Under `--connect`, the client has no store.** The daemon reads its own
`~/.tau/config.json` and builds its catalog, models, API keys and extensions from
it. So `--store`, `--session-dir`, `--model` defaults and extension flags do not
apply on the client, and passing one with `--connect` is an error, not something
silently ignored.

## 4. Durable writes

### 4.1 An entry is opened, then finalized

When a message starts, its entry is appended with `"status": "incomplete"`. When
it ends, a line with the **same id** and no `status` replaces it, because a
loader keeps the last line for each id.

The alternative was a separate `finalize` entry that refers to the incomplete
one. It loses because every reader of the tree would then have to join the two
entries: `ConversationTree`, `context_for`, all three `SessionLog`
implementations, and the client replica of §5. With "the last line for an id
wins", only the loaders change. The cost is that an id is no longer written only
once, so the `SessionLog` contract suite gains that rule as a test.

Built note (M1): `SessionLog.finalize(entry_id, payload)` is the new protocol
member. The file store appends the second line and `keep_last_per_id` folds on
load. The JMFTS store PATCHes the document in place, keeping its `seq`, so
`load`'s order check still holds. The contract suite's "durable writes" section
checks all three.

### 4.2 What is written when

| Event | Written |
|---|---|
| Turn admitted | The user message (and queued inputs), finalized at once |
| `message_start` (assistant) | The entry, opened, with no content |
| `DoneEvent` | The entry, finalized, with its content and usage |
| A tool result is collected | The tool-result entry, once every earlier call in its batch has one |
| A steer is delivered, a `turn_end` hook injects | That message |

Built note (M1), two divergences from the plan:

- **Tool results are not opened.** The finalized assistant entry already holds
  every call's name and arguments, so an assistant message whose calls have no
  results is the record that tools were running. A parallel batch writes its
  results in call order, each as soon as it and every earlier result exist.
- **The cursor stays on the parent of an open entry** and moves onto it when it
  is finalized. So a context read while a message streams, by a tool, a
  sub-agent or a head, still folds, and the leaf never names an incomplete
  entry.

Deltas are not written. A crash therefore loses the partial text of the message
that was streaming, but keeps the fact that it was streaming, and everything
finished before it. Writing a checkpoint every N deltas could be added later. It
is absent here (§11).

This replaces the end-of-turn write in `_run_one_turn`. The `_TurnPersistence`
latch (`inputs_written`, `loop_written`) existed only because writing happened
late, so it is gone. The loop writes through a `TurnWriter`
(`agent_loop.py`), which `AgentSession` implements over the turn's cursor.

### 4.3 An incomplete entry on reopen

- The tree, the transcript and the tree browser show it, marked as interrupted.
- `context_for` raises if the path to a leaf contains an incomplete entry. An
  assistant message with no ending, or a tool call with no result, is invalid
  input to every provider. Making one up is the defect that §1 removes.
- `default_leaf` does not stop on an incomplete entry. It goes back to the
  nearest finalized ancestor, so reopening a session leaves the user able to
  retry, and the interrupted branch stays visible beside the retry.

### 4.4 No made-up endings

`agent_loop.py:935-947` is deleted. A stream that ends without a `DoneEvent`
raises. The open entry stays incomplete, and the head shows the error. This is
what "Fail Early" means here.

## 5. The stream protocol replicates the log

A client under `--connect` keeps a **replica** of each attached session's
entries. The daemon sends:

- **Entry events.** `entry_open`, `entry_final` and `entry_append`, each
  carrying the full entry. These are the §4 writes, sent as they happen. A client
  applies them with the same "last line for an id wins" rule, so its replica is
  exactly what the daemon's file holds.
- **Ephemeral events**, which change no state: text and reasoning deltas, cursor
  opened, cursor closed, turn started and ended (each with `cursor_id`),
  extension UI requests, and status.
- **A sequence number on every event, per session.** A client that reconnects
  sends the last number it saw, and the daemon replays entry events from the log
  after that point. A client too far behind gets a snapshot instead.

Because the replica holds the whole tree, `ConversationTree(entries, leaf)`, the
tree browser and transcript reload run unchanged on the client. They are pure
reads over entries (`CURSORS.md` §3). The alternative was a request for each
read, as RPC's `get_tree` and `get_messages` work. It loses because the TUI reads
the tree synchronously from 26 `self._cursor` sites, and each of those would
become a network round trip.

**Backpressure gets an answer.** `HEADS-AND-MULTIPLEXER.md` §5.2 item 2 left open
whether one slow reader stalls the agent for everyone. With replay, the daemon
**drops** a client whose queue goes over a bound. The client reconnects and
catches up from its sequence number. The agent never stalls for a reader, and a
reader never misses an entry. Deltas that were dropped are not replayed, and the
final entry carries the full text anyway.

**The schema is generated.** Event and command types are defined once, as
dataclasses in `tau_agent_core`. A script generates JSON Schema from them, and
tau-code's `protocol` package generates its TS types from that schema, in the
way `scripts/generate_rpc_protocol_doc.py` already works for RPC. Every count in
the docs is generated, which closes defect 5 for the new protocol. The RPC
counts are corrected by hand in the same pass.

Built note (M2):

- The protocol is `tau_coding_agent/serve/protocol.py`: one dataclass per
  request and record. `docs/SERVE-PROTOCOL.md` and
  `docs/serve-protocol.schema.json` are generated from it
  (`scripts/generate_serve_protocol.py`), and `test_serve.py` fails when either
  is stale. Its "Counts" line is the only place the numbers are written.
- **Replay comes from memory, not the log.** Each loaded session keeps its last
  50,000 events (`REPLAY_BOUND`). A client whose `since` is older, or whose
  `epoch` is from another daemon run, gets a snapshot. A daemon restart is
  always a new epoch, so after one every client takes a snapshot.
- A client is dropped when 20,000 frames are queued for it (`QUEUE_BOUND`),
  with close code 4000, and resumes by `since`.
- Writes are observed by wrapping the log instance's own `append_at` and
  `finalize` (`daemon.watch_writes`), so every writer is seen and the store's
  class is unchanged for `fork`'s `isinstance` checks.
- The client applies an `attach` answer inside its reader, before the next
  frame, so no event that follows the answer can miss the replica.

**`cursor` means a cursor.** The new protocol names entry ids `leaf` and
`entry_id` from its first version. The RPC rename (ROADMAP, "RPC says `cursor`
where it means an entry id") follows it before the release.

Built note (protocol 0.2), 2026-10-03. tau-code's move from RPC to `tau serve`
listed what the serve protocol lacked that RPC has. `PROTOCOL_VERSION` is `0.2`,
so a 0.1 client is refused at `hello`. What changed:

- **One derivation per read.** The reads RPC and the daemon both answer are in
  `tau_agent_core/projections.py`: `command_vocabulary`, `flow_next_step`,
  `domain_listing`, `path_completion`, `browse_rows`, `model_catalog` and
  `extension_state`. The RPC handlers call them, and `docs/RPC-PROTOCOL.md`
  regenerates unchanged.
- **`Surface`** carries the whole command vocabulary as `CommandInfo`
  (`name`, `description`, `origin`, `flow`, `hidden`), built-ins included. It
  also carries `loaded` (`ExtensionInfo` per loaded extension) and
  `load_errors` (`[path, error]`), so the TUI's `/extensions` view under
  `--connect` shows what the local one shows. `command_args`, `shortcuts` and
  `extensions` stay, because `RemoteBackend` reads them.
- **`Attached.models`** is a list of `ModelRecord` (`name`, `model: {id,
  provider, context_window}`), the shape of RPC `get_models`.
- **Six new requests.** `next_step` and `enumerate_domain` answer with the RPC
  result shapes. `complete_path {session_id, text, offset}` resolves against the
  session's cwd on the daemon's machine. The caret is named `offset`, because
  in this protocol `cursor` means a cursor. `get_tree {session_id, cursor_id}`
  answers `{nodes, leaf, count}`. Each node is a `TreeRow`, which is RPC's row
  with `is_cursor` renamed to `is_leaf`. `fork_session {session_id, at}` forks
  through the daemon's catalog into the session's cwd and answers
  `{session_id}`. It refuses with `busy` when the copy would carry an entry that
  is still being written.
- **A command missing an argument answers its `FlowStep`.** The `submit` answer
  serializes the step, and `remote.dispatched_from_wire` rebuilds it with its
  `Argument` and `Domain`. Under `--connect` the TUI asks one argument per form.
  It enumerates select values with `enumerate_domain` and steps with
  `next_step`, both on the daemon, because only the daemon holds the session's
  extension flows. The local TUI still asks every argument in one form.
- **`/fork` and `/resume REF` answer `Ready`, unperformed.** The daemon cannot
  move a client to another session, so `daemon.SWITCHING` hands those two
  mutations back for the client to perform. The TUI forks with `fork_session`
  and then attaches, and resumes by attaching.
- **`perform` takes a required `cursor_id`.** The daemon sets `TURN_CURSOR` to
  that cursor around the call. `TauBackend`'s tree edits, `rollback_turn` and
  `apply_session_name` now act on `AgentSession._turn_cursor()`, so the
  operation runs at the named cursor and never at the head by default. No
  performable method had to be refused for a non-head cursor: the rest are
  session-wide, or already read `_turn_cursor()`. A command a `submit` performs
  runs at the submit's cursor in the same way. A `/model NAME` sent on a
  non-head cursor sets that cursor's frame, as `set_model` does.
- `set_model` left `PERFORMABLE`. The `set_model` request is the only way to
  call it.

Not built: the TUI's keystroke completions (`@path` and argument values) still
run locally and synchronously under `--connect`. They are not round trips to the
daemon.

Built note (schema, 2026-10-03). The schema typed what a client sends and not
what it receives; tau-code generates its types from it. `PROTOCOL_VERSION` is
`0.3`, because a 0.3 client reads fields a 0.2 daemon does not send.

- **Every answer is typed and linked.** `protocol.RESULTS` maps each request to
  its result record, or `None` for a `null` answer. The schema writes it twice:
  `x-result` on each request's `$def`, and the top-level `Results` map keyed by
  `type`. The daemon builds the record and `protocol.result_to_wire` checks it
  against `RESULTS` before sending, so the type and the bytes have one source.
- **Every event's data is typed.** `protocol.EVENT_DATA` maps each kind to its
  data. `Event` in the schema is a `oneOf` of one `<Kind>Event` per kind, with
  `x-data` naming each kind's data. `agent_event` is `AgentEvent`'s own pydantic
  schema, because the daemon sends the whole model and not RPC's `WireEvent`
  (until the agent_event note below).
- **Entries, messages and specs are TypedDicts.** The daemon forwards them as
  dicts, so they are shapes, not records it builds: `Entry` (one shape per
  entry `type`; an unfinished entry matches only `IncompleteEntry`), `Message`
  (the `tau_llm` pydantic models, the system prompt, and `role: "custom"`),
  `FormSpec`, `PanelSpec` and `Ask`.
- **Requests are closed; everything sent is open.** The schema says so at its
  top, and `docs/SERVE-PROTOCOL.md` says so too. `test_serve_schema.py` runs a
  real session through every request and event kind, and validates each frame
  with undeclared keys refused. `jsonschema` is not installed in the venv, so
  `tau_agent_core.testing.schema_check` validates the keyword subset the schema
  uses, and raises on any other keyword.
- **Added on the wire.** `hello` answers `pid`, `version` and `cwd`.
  `CursorState.request` is the extension request at the cursor's leaf, in the
  shape of RPC `get_pending_request`, cached per leaf. `Attached.requests` lists
  the forms open now. `submit` takes `expand_attachments`, which resolves `@path`
  against the session's cwd. Its report rides on `submission_start` as
  `attachments`. The new request `perform_ready` performs a `Ready` at a cursor.
  The two derivations are shared with RPC in `projections.py`:
  `attachment_expansion` and `request_payload`. Moving the attachment one fixed
  an RPC defect. Expanding text that named no file used to drop the request's
  own images.
- **The daemon now publishes the cursor set after every request.** It used to
  publish it only when the next event happened. So a lock that a command armed
  never reached a client until something else happened.
- `perform` of `compact` answers `CompactionResult`. `remote.value_from_wire`
  could not read that answer, and now reads it.
- **`tau serve` options.** `--schema` prints the schema from an installed τ.
  `--web-root DIR` overrides `serve.web_root`. `-d` probes with a real hello and
  starts no second daemon where one answers. It waits for a hello that carries
  its child's pid. `-d --json` prints one `ServeStarted` object.

Built note (agent_event, protocol 0.4), 2026-10-03. `agent_event` was
`AgentEvent.model_dump()`. A `message_update` carried the whole message so far on
every chunk, so the bytes grew with the square of the reply: on the socket, in
the replay history, and again on replay. It also lacked the fields tau-code's
live view reads (`block_type`, `replace`, `stop_reason`, `dropped_tool_calls`,
`cache_notice`). `PROTOCOL_VERSION` stays `0.4`, which no client had yet.

- **`agent_event` data is RPC's `WireEvent`.** Its `$def` is `WireEvent`,
  generated from `tau_agent_core.rpc_event_schema.WireEvent`, the model that
  types RPC's event lines. The daemon builds it with
  `rpc/wire_events.WireEventProjector`, which the RPC handler now uses too. It
  holds one `MessageDeltaProjector` per cursor, because two cursors stream at
  once in a comparison, and one `PromptCacheObserver` whose turns are kept per
  cursor. RPC filters to one cursor, so its bytes are unchanged:
  `test_rpc.py`, the conformance tests and `docs/RPC-PROTOCOL.md` regenerate
  as before. `cursor` is always null on this wire, since the `cursors` event
  carries every leaf.
- **Entry events name their cursor.** `EntryEventData.cursor_id` is the cursor
  whose turn or request made the write. An `entry_final` names the cursor that
  opened the entry. This field is how a client joins the two event kinds.
- **The TUI renders the wire projection, locally and remotely.**
  `TurnStream.feed` projects an `AgentEvent` with `project_event` and reads only
  the result, through `feed_wire`. `WireEvent` leaves out what the TUI draws
  beside the text: tool arguments and results, usage, and a steer's words. So
  `feed_wire` takes them as an `EventDetail`. Locally, `EventDetail.of(event)`
  reads them off the event. Under `--connect`, `remote.WireJoin` reads them
  from the entries that arrive on the same socket:
  - arguments come from the assistant entry finalized before the tool ran;
  - a result comes from its toolResult entry, so the end event waits for it in
    a sequential batch;
  - usage comes from the assistant entry the cursor finalized just before a
    `message_end` that has a `stop_reason`;
  - a held `message_start` becomes a steer when the cursor's next entry is a
    user message.

  The other way was to rebuild `AgentEvent` from the deltas. It loses because
  the entries are the only source for those fields, and inverting the
  projection would derive the same thing a second time.
  `test_connect_tui.py` checks that the render events a remote TUI builds equal
  the ones the daemon's own bus produces for the same turn. That turn has
  reasoning, text and a tool call.
- **What `--connect` does not show.** A side completion's model and spend are
  not on the wire. A compaction entry records them (`summarizerModelId`,
  `summaryUsage`) but arrives after the end event, and a branch summary records
  neither. So the box under `--connect` shows its streamed text and says `done`,
  where the local box shows the cost. It does not say "cost not reported",
  which would be false.
- **Replay keeps deltas.** The history now holds bounded items, so keeping them
  costs little. Replay sends the events a client missed, and the TUI draws a
  live message from its deltas. If deltas were left out, a client that
  reconnected mid-message would show that message cut short until a reload.
  `entry_final` repairs the replica, not the drawn text.
- Measured with a reply of 30,000 text characters and 7,500 reasoning
  characters, in ten-character chunks: before, 50,653,566 bytes of
  `agent_event`, the largest `message_update` 30,730 B. After, 2,612,247 bytes,
  every `message_update` about 700 B. `test_serve_schema.py` checks that the
  size per frame stays flat and the total stays linear.

Built note (protocol 0.6), 2026-10-03: **serve is RPC plus addressing.** A
comparison of the two tables found the same behaviour under different names
(`set_model {model}` against RPC's `{name}`, `fork_session`, `create_session`,
`navigate_tree`), fourteen operations behind one untyped `perform {method,
arguments}`, six string error codes where RPC has `SUBMISSION_REJECTED`,
`TURN_STILL_RUNNING`, `SESSION_NOT_PERSISTED` and `COMMAND_NOT_SUPPORTED`, and a
daemon that wrote every WebSocket submission as `source: "interactive",
submitter: "human"`. Only the event stream matched, since 0.4. The rule now:

- **A request RPC answers is answered here under its name, with its params and
  its result shape, plus `session_id` and `cursor_id`.** `RpcCall` carries one;
  `parse_request` checks its params with RPC's own `validate_params`, and the
  schema embeds RPC's `params_schema` and `result_schema` as the verb's `$defs`.
  `RPC_RUN` (33 verbs, `compact` and `abort` among them) run through
  `COMMAND_TABLE[verb].handler` itself, inside `TURN_CURSOR.set(cursor)`, with a
  `daemon.RpcContext` standing in for `RPCHandler`: its output queue sends an
  answer to the asking client and `compaction_end` to the session as an event,
  and the compaction in flight is kept per cursor. So the daemon has no second
  implementation of those verbs to drift.
- **That needed RPC's handlers to act at a cursor.** They read
  `AgentSession.acting_cursor` (was the private `_turn_cursor()`), which is the
  head over stdio. `set_model` at a non-head cursor sets that cursor's frame
  model, which the daemon had special-cased; it is in the core now.
  `complete_path` and `expand_attachments` resolve against `AgentSession.cwd`.
- **`submit` and `prompt` are the daemon's own** (`RPC_OWN`): RPC's params, and
  `source`/`submitter`/`submission_id` required on `submit` as RPC requires
  them. They differ from stdio where serve can do more. `multitask_strategy:
  "fork"` is accepted. A command answers success with `dispatched`, its arm,
  where stdio refuses a step or a ready flow, and the daemon performs a ready
  flow itself. The answer comes at admission, as RPC's does, plus `admitted`,
  which says a `submission_end` will follow; 0.5 answered when the turn ended.
  The TUI's `RemoteBackend.submit_turn` and `ServeClient.submit_and_wait` wait
  for that event, so a head still sees a turn end where it did.
- **Same name, serve's shape where a connection holds no session.**
  `list_sessions` answers RPC's rows plus `cwd` and `loaded`, every directory
  (`scope.cwd` null), unreadable rows listed with their `error`.
  `new_session {cwd, model?, name?}` and `fork {session_id, cursor_id, at?}`
  load the new session and answer RPC's lifecycle shape; nothing is switched,
  so the client attaches. `fork` copies the path to the cursor's leaf, as RPC's
  does; 0.5's copy of the whole tree is gone.
- **Errors keep RPC's meaning**: `submission_rejected` (with the lock in
  `data`), `command_not_supported`, `session_not_persisted`, and `busy` for
  `TURN_STILL_RUNNING`.
- **Serve's own requests are the ones RPC cannot express**: `hello`, `attach`,
  `detach`, the three cursor requests, `answer`, `describe`, `perform_ready`,
  `compare`, `end_compare`. `get_capabilities` is `hello` plus the schema;
  `switch_session` is `attach`.

Two defects surfaced on the way. RPC answered an unknown flow or domain with
`INTERNAL_ERROR`, because `next_step` re-raised it as a `RuntimeError`; both are
`INVALID_PARAMS` now. And no entry shape declared `copiedFrom`, the field
`paste_subtree` writes, which the schema test found the first time it drove
every verb against a real daemon. `test_serve_schema.py` now answers every
request and every RPC verb, and validates each answer with `strict` on.

RPC's result schemas leave shapes as prose: `messages: array` with no
`items`, `model: object`, `values: unknown[]`. Embedded verbatim, they made
0.6's answers less typed than 0.4's `$defs`, and tau-code would have had to
hand-type them (tau-code-d7's report). `RPC_TYPES` names each such node and
the record it holds, and `json_schema` puts the record there, keeping RPC's
description; `RPC_PARAM_TYPES` does the same for two request arrays, and
`compaction_end` gets the same treatment. A path that names no node raises,
so the table cannot outlive RPC's schema, and
`test_every_answer_and_event_payload_is_typed_to_its_leaves` refuses an
array without `items` or an object with neither fields nor a value type.
Strict validation then found one more shape: the context renders a
compaction or branch summary as a user message with no `timestamp`, which
no stored message matches, so a context is a list of `ContextMessage`,
whose `SummaryMessage` arm alone has no `timestamp`.

**Built note (2026-10-03, RPC 2.1):** the typing moved into core, so RPC's own
reference is typed too. The generator is `tau_agent_core.json_schema`; the
records and the tables, now `RESULT_TYPES` and `PARAM_TYPES`, are
`tau_agent_core.rpc.records`. `get_capabilities` publishes each verb's schemas
typed, plus a `$defs` map their `$ref`s resolve against, and
`docs/RPC-PROTOCOL.md` renders `$defs` under "Types". The RPC table's own
schemas stay untyped, because they are the vocabulary `validate_params`
enforces, which has no `$ref`. Serve's schema builds on the same functions.
Its structure was unchanged by the move, except for five params maps the
core test was the first to check: `correlation`, `bound` and two `values`.
The five verbs serve does not run, `get_capabilities` and the session
lifecycle, are typed and strictly validated in core's tests. Under `--connect` the picker searches a session's bounded `title`,
since the listing no longer carries `first_message`.

## 6. `tau serve`

```
tau serve                      # foreground; one log line per event
tau serve -d                   # daemonize, log to ~/.tau/serve.log, give the terminal back
tau serve --listen 0.0.0.0:PORT
tau serve --listen unix:/path  # only if it stays a one-branch addition (§6.3)
```

### 6.1 The foreground log

There is one line for each of:

- a client attaching or detaching;
- a session loading or unloading;
- a cursor opening or closing, with its owner;
- a turn starting or ending, with its `cursor_id`, model and token count;
- a tool call, with its name and duration;
- an entry left incomplete.

All of these come from the event bus the heads already subscribe to. The log is
a subscriber, not a second source.

### 6.2 Transport and auth

- **Transport.** WebSocket, through the `websockets` library (16.1.1 is in the
  venv). A browser can speak it with no relay, and so can VS Code and the TUI.
  It goes in a new `[serve]` extra, so a headless install stays as small as
  `pyproject.toml` records it today.
- **Auth is opt-in.** If the daemon config sets `serve.token`, the daemon
  requires it. The client sends `TAUD_TOKEN` from its environment. With no token
  configured, the daemon accepts every connection. The docs show
  `openssl rand -hex 32` as one way to make a token. There is no TLS: `ssh -L`
  covers that.
- **The port.** There is a default port, so `tau --connect buildbox` needs nothing
  else. Built: 8256 (John's pick, 2026-10-03).

Built note (2026-10-03, `serve/http.py`, commit 4a432f4), found during M4:
browsers apply no same-origin policy to WebSocket handshakes. So with the token
off, any page open in the user's browser could connect to `127.0.0.1:8256` and
drive a tool-running agent. John chose:

- **An Origin check, on by default.** A handshake whose `Origin` header is
  present and names neither its own `Host` header nor an entry of
  `serve.allowed_origins` gets HTTP 403, and the daemon logs it. Clients outside
  a browser send no `Origin` and pass. Comparing against `Host` rather than the
  bind address keeps `ssh -L` and `docker -p` remaps working. Tokens stay opt-in.
- **The daemon hosts the web client.** A plain HTTP request is served from
  `serve.web_root`, with `index.html` for a directory and a 404 for anything
  outside the root, so the page and its socket share one origin. tau-code's
  `packages/server` no longer needs to exist to serve it. A `web_root` that is
  not a directory fails at start.

### 6.3 Unix sockets

`asyncio` serves a unix socket with the same handler as TCP, and `websockets`
supports one (`unix_serve`), so I expect `--listen unix:` to be a single branch
in argument parsing (`inferred`). It ships only if it is that small. On Windows
the option is missing, and TCP behaves the same on every platform.

### 6.4 Built note (M2)

- **Default listen address: `127.0.0.1:8256`.** Loopback, so a build box is
  reached through `ssh -L 8256:localhost:8256 buildbox` unless its config says
  otherwise. Config keys, all optional:

  ```json
  "serve": {"listen": "0.0.0.0:8256", "token": "<secret>"}
  ```

  A token can be made with `openssl rand -hex 32`, or
  `head -c 32 /dev/urandom | base64`. The client sends `$TAUD_TOKEN`.
- `--listen unix:/path` shipped: `websockets` serves and dials a unix socket
  with the same handler, so it cost one branch in each direction. On a platform
  without unix sockets the address is refused when parsed.
- `-d` runs `python -m tau_coding_agent.cli serve` detached (a new session on
  POSIX, `DETACHED_PROCESS` on Windows), appends its output to
  `~/.tau/serve.log`, and returns when a WebSocket handshake succeeds. If the
  child exits first, `-d` prints the new part of the log and fails.
- `tau serve --tail [SESSION] [--connect ADDR]` attaches and prints one line per
  event, leaving out deltas, and reconnects across a daemon restart.
- `TauBackend` now takes `config["cwd"]`, so a served session's tools, system
  prompt and context files use the session's directory, not the daemon's.
- Fixed on the way: `AgentSession.cursor`'s setter kept every replaced head
  cursor in `cursors`. The first daemon listed the backend's scratch cursor
  beside the real one. A replaced head on another tree is now retired.

Measured run, with a stub OpenAI-compatible server streaming at 0.15 s a word:
a turn was started from a client, `kill -9` was sent to the daemon 2 s in, and
the daemon was restarted. The reopened session held the user message, an
assistant entry with `"status": "incomplete"`, and a head cursor on the user
message. The restarted daemon logged `message entry da76943b left incomplete`,
and a `--tail` client that was attached through the kill reconnected and kept
printing.

### 6.5 What runs in the daemon

Sessions, cursors, the agent loop, tools, extensions, compaction and the store
all run in the daemon. The daemon loads a session when a client first attaches
to it. A session with no client attached keeps running its turns.

An extension UI request (a confirm, a select) is sent to every attached client,
and the first answer wins. With no client attached, the request gets its
declared default when it is made. That is `HEADS-AND-MULTIPLEXER.md` §5.3's
recommendation, re-evaluating `allow_user_input` at the moment of the ask, and it
is the one core change this section needs.

## 7. The clients

### 7.1 `tau --connect HOST[:PORT]`

- A `RemoteBackend` implements the `Backend` ABC (`backends.py:1102`): `submit_turn`,
  `submit_command`, `subscribe_render`, `abort` and the rest become protocol
  messages.
- A `ReplicaSession` stands in for the seven `agent_session` getattr sites and
  the 26 `self._cursor` reads. It holds the replica of §5 and the client's
  cursor id. I have not checked every one of those 33 sites. The ones that call a
  mutator instead of reading the tree become commands. That count is the main
  unknown in this section.
- **The session picker spans every cwd.** It lists the daemon's sessions,
  grouped by cwd. A new session asks for its cwd, and the client's own cwd is the
  default only when the daemon is local.
- In-process `tau` stays the default, and nothing about it changes.

### 7.1.1 Built note (M3)

The 33 sites were cheaper than §7.1 feared, because 22 of the 26
`self._cursor` uses are `self._cursor.context()`, a pure read, and every
backend use is already `getattr(backend, name, None)`. What shipped, in
`tau_coding_agent/serve/remote.py`:

- `ReplicaSession` is a `ConversationSession` over the replica. Every read is
  local; `append_at` and `finalize` raise `RemoteUnsupportedError`, so a TUI
  path that still writes locally fails with its name rather than writing a
  second copy.
- `RemoteCursor` reads the head cursor's leaf and busy state from the replica's
  `cursors` events, and follows the head across a daemon restart.
- `RemoteBackend` has no `agent_session`, so the in-process paths skip it.
  `submit_turn` and `submit_command` are `submit`; `subscribe_render` feeds the
  daemon's `agent_event` and `channel` events into the same `RenderRouter` a
  local bus feeds. Mutations the TUI calls by name (`compact`,
  `set_session_name`, `navigate_tree`, `elide_span`, `commit_branch`,
  `paste_subtree`, `rollback_turn`, the extension actions) are one new request,
  `perform`, over an allowlist (`protocol.PERFORMABLE`).
- Reads the TUI makes synchronously on the event loop (extension commands,
  argument hints, shortcuts, managed extensions) ride in the attach answer as a
  `Surface`, refreshed by `describe` after an extension changes.
- A command that resolves to `Ready` is performed by the daemon, which holds
  the backend, and comes back as `Performed`. A `FlowStep` (a command missing
  an argument) is refused under `--connect`; give the argument in full.
- `RemoteCatalog` serves only `list`, from a worker thread; creating, opening
  and clearing a session are the app's `_remote_new_chat` / `_remote_open`.
  The picker opens on all cwds.
- `RemoteConnection` reconnects with backoff and re-attaches with `since`.

`tau --connect ADDR [--cwd PATH]`: `--cwd` is the directory new sessions get on
the daemon's machine, defaulting to this one. Every flag that configures an
in-process session (`--store`, `--session-dir`, `-e`, `--tools`, `--thinking`
and the rest, `cli._LOCAL_ONLY_FLAGS`) is refused with `--connect`, and
`--connect` is refused with `-p`, `--mode rpc` and `--mode repl`.

Measured: a real `TauApp` driven by Textual's pilot against the real daemon
process resumed the session from the §6.4 kill run and ran a turn. Its replica
equalled the daemon's file and a second client's replica
(`test_connect_tui.py`). Not built: `fork` and `/new`'s session switching from a
command (pick the session instead), and the `/extensions` view's load-error
list, which needs `get_extension_state`.

### 7.2 Starting a local daemon automatically

With `"serve": {"autostart": true}` in the config, a plain `tau` first tries to
connect to the configured address. If nothing answers, it runs `tau serve -d`,
waits until the port accepts connections, and then connects. Doing this twice is
harmless: a second daemon fails to bind the port, and that error ends it. No
lock file is needed.

Built note (M3): as planned. `"serve": {"autostart": true}` makes a plain `tau`
dial `serve.listen`, run `tau serve -d` if no handshake succeeds, and connect;
a second run finds the first daemon and reuses it.

### 7.3 tau-code

The web client and VS Code speak the stream protocol directly to `tau serve`.
`packages/server` and `packages/runner` are deleted (991 lines). When a prompt
is sent to a busy cursor, it is now queued as a follow-up. That replaces
`multitask_strategy: 'reject'` (`packages/ui/src/useTau.ts:190`). A user who
wants a parallel turn opens another cursor instead. tau-code ships a release
alongside 0.12.0.

Built note (protocol 0.4, 2026-10-03): "queued as a follow-up" is the core's
`enqueue` strategy: the prompt waits for the running turn and runs as its own
turn. Protocols 0.1–0.3 offered a `follow_up` strategy that the daemon passed
to `AgentSession.submit` as `"followUp"`, which the core has no branch for, so
every such prompt failed with `NotImplementedError`. tau-code found it from the
schema while porting. `Submit.multitask_strategy` is now the core's own
`MultitaskStrategy` Literal. `parse_request` also checks every field's value
against its annotation, as the schema does, so a value outside an enum is a
`bad_request` at the door. Before, it reached the daemon's handlers.

### 7.4 RPC stays

`tau --mode rpc` is the stdio head: a process that wants an exclusive agent
without importing τ uses it, whether that is a ROS node, a web server or an
operating system. It is not deprecated. It gets the `cursor` rename before
0.12.0. It gets a revision after `--connect` has been used enough to show what
driving τ remotely needs, and that revision is not in this pass.

Built note (RPC 2.0, serve 0.5), 2026-10-03. Serve's 0.2 rename to `leaf` was
incomplete: `FlowStep.cursor`, `Performed.cursor` and `WireEvent.cursor` still
meant an entry id. All three are core types that both protocols serialize, so
finishing serve's rename was the RPC rename. One meaning per name now holds on
both wires:

| Was | Is | Where |
|---|---|---|
| `cursor` (an entry id) | `leaf` | every mutator's result (E5), `get_state`, `get_tree`, the session tuple, `compaction_end`, `WireEvent`, `FlowStep`, `Performed`, the `next_step` / `enumerate_domain` / `complete_message_id` parameter |
| `cursor` (a caret) | `offset` | `complete_path`'s parameter, as serve already named it |
| `is_cursor` | `is_leaf` | `get_tree` rows and `BrowseNode`; RPC no longer renames the core's key |
| `ancestors_of_cursor`, `descendants_of_cursor` | `ancestors_of_leaf`, `descendants_of_leaf` | `MessageIdScope` |

`cursor` on either wire now means only a writer, and `cursor_id` names one.
RPC's `PROTOCOL_VERSION` is `2.0`, because a rename breaks what is on the wire,
and serve's is `0.5`. The pass also found `app.py`'s
`action_run_session_flow` reading `getattr(log, "cursor", None)`: `SessionLog`
has had no `cursor` attribute since `CURSORS.md`, so the read was always
`None`. It now reads the head cursor's leaf.

## 8. The demo: comparing cursors from one leaf

From any leaf, the user starts N cursors. Each one first writes a `config` entry
with a different model (or thinking level, or system prompt). The same prompt
then goes to all of them at once. Each client shows the N streams side by side,
and when they end the user keeps one leaf and continues from it. The other
cursors close, and their branches stay in the tree.

The core pieces exist: config is read by ancestry (b275de0), turns are routed
through `TURN_CURSOR` (64dca80), and `_record_config` writes only the keys that
differ. **The first task is the missing test from §1**: two concurrent top-level
turns on two cursors. If it fails, this section becomes a core change, not a UI
one.

- **Commands.** `/compare model-a model-b ...` in the TUI. tau-code gets a
  matching control. Both are clients of one protocol command, `compare`, whose
  arguments are a leaf and a list of config overrides.
- **Layout.** The TUI gets a split transcript with one column per cursor. That
  is new layout work, and the cost I am least sure of in this record.

### 8.1 Built note (M5)

- **The core is `tau_agent_core/compare.py`.** `start_comparison(session, leaf,
  models, text)` resolves every name first (an unknown one raises before any
  cursor opens), then opens one cursor per model, owned by the head and labelled
  with the model name, each with a `TurnFrame` of that model, every session tool
  and hooks on, and submits the text to all of them as background tasks.
  `Comparison.end(keep)` moves the head onto the kept cursor's leaf and closes
  every compare cursor. `TauBackend.compare` / `end_compare` hold the open
  comparisons, so the daemon and the in-process TUI call the same two methods.
- **`/compare` is a core flow**, `compare(models, text)`, so no head sends it to
  the model as prose. `/compare a b -- prompt` binds both arguments
  (`split_compare_args`); without `--` every word is a model and the prompt is
  asked for as the flow's next step, and a bare `/compare` asks for both in one
  form (a multiselect of models). The capability is off the RPC wire, the one
  such capability, and the RPC head says so when given the line; the REPL refuses
  it, having no columns to draw.
- **Keeping one while others run aborts them.** Picking a winner is the decision
  that the rest are unwanted, so `end` aborts every compare turn still running
  (cancels one not yet admitted), awaits them, then closes. It refuses, changing
  nothing, while the *kept* turn or the head's turn is still running. `keep`
  `None` (Esc in the TUI) keeps none and leaves the head where it was. Every
  branch stays in the tree either way.
- **The kept cursor closes too.** The head is now at its leaf, so a second
  cursor there would be clutter. The head continues under its own model, not
  the kept one: the branch's config entry names the compared model, and the
  head's next turn records its own config after it.
- **Columns are drawn from the turns, not from who asked.** Each compare turn's
  `Submission.correlation["compare"]` is `{id, models, index, cursor_id}`, which
  the render router already carries onto `stream_start`. The TUI routes any
  stream with it to a `CompareScreen` (`compare_view.py`): a full-screen dialog
  of N bordered columns fed by deltas, digits keep, Esc keeps none. So a second
  TUI attached to the same daemon session gets the columns too. No `ChatDisplay`
  change was needed; the layout cost §8 feared was one screen of 160 lines.
- **Protocol.** `Compare {session_id, models, text, leaf=None}` (`leaf` moved
  last and became optional; `None` is the head's leaf) answers
  `{comparison_id, cursors: [{cursor_id, model}], message}`. New request
  `EndCompare {session_id, comparison_id, keep}` answers `{leaf}`, or `busy`,
  or `not_found`. A typed `/compare ...` through `submit` comes back as a
  `Performed` with the same data. `PROTOCOL_VERSION` stays `0.1`: the change
  adds a request and reshapes one that only ever answered an error.
- Fixed on the way: the daemon's "entry left incomplete" log line was
  session-wide, so the first of two concurrent turns to end reported the other's
  still-streaming message. Open entries are now keyed by the writing cursor.

Measured: `test_serve_compare.py` (four cases over a real socket),
`test_compare_tui.py` (in-process keep, Esc with a turn still running, and
`--connect`). Live, against a stub OpenAI-compatible server and a `tau serve -d`
process: a `--connect` TUI ran `/compare fake-a fake-b -- compare me`, both
columns streamed at once, a second, plain client saw both `submission_start`s
with the comparison, and pressing `2` left the head on the fake-b answer with
both compare cursors closed and all three assistant entries in the file. Not
measured: a real model, tool calls inside a compare column, or a web client
(M4).

## 9. Order

Each milestone ends with something to examine.

| # | Work | Examine |
|---|---|---|
| M0 | The concurrent-cursors test (§8). The JSON Schema generator, with the RPC counts fixed (defect 5). | The test passes. Protocol docs are generated. |
| M1 | Durable writes (§4), including the loader rule, the contract-suite test, the JMFTS store, and deleting the made-up ending. | `kill -9` partway through a turn of `tau -p` leaves an incomplete entry, and the TUI reopens before it. |
| M2 | `tau serve` with entry and ephemeral events, replay, the foreground log, `-d`, and the opt-in token. A small `tau serve --tail` client. | A daemon whose log shows a session driven by a script, with a reconnect that catches up. |
| M3 | `tau --connect`: `RemoteBackend`, `ReplicaSession`, the cross-cwd picker, extension UI requests, autostart. | **The daemon stack, working end to end from the TUI.** |
| M4 | tau-code moves to `tau serve`. Delete `server/` and `runner/`. Fix `multitask_strategy`. | A browser and VS Code on the same session as the TUI. |
| M5 | The compare demo (§8). | §2 runs in full. |
| M6 | Rename `cursor` on the RPC wire, and protocol bump. Docs, then the release. | 0.12.0 and the matching tau-code release are tagged and pushed. |

M1 does not depend on M0. M2 depends on M0's generator. M3 depends on M2. M4
depends on M2 and can run alongside M3. M5 depends on M3 for the TUI, and on M4
for the web.

**Cost.** M3 is the largest, because of the 33 TUI sites. M2 is next: a new
module tree in `tau-coding-agent`, plus the extra. M1 is about the size of
CURSORS step 2. These sizes are estimates (`assumed`). Nothing has been measured.

## 10. Before the release

- ~~The `cursor` → leaf/entry-id rename on the RPC wire~~ — built, RPC 2.0 (§7.4).
- Correct the verb counts in ROADMAP and in tau-code's ARCHITECTURE.md (defect 5,
  closed by M0).
- Amend `HEADS-AND-MULTIPLEXER.md` §5 and `REMOTE-CONTROL.md`, both of which
  describe the hub as the multiplexer.
- A full `pytest` run, a run of `scripts/check_docs_coverage.py`, and the release
  matrix in `docs/RELEASING.md`.

## 11. Absent

- **Locks against two processes writing one session.** That is misuse (§3).
- **Mandatory or automatic auth, and TLS.** The token is opt-in, and `ssh -L`
  provides transport security (§6.2).
- **Persisting cursors.** They are not durable, by decision (`CURSORS.md` §11).
  After a daemon restart, a client opens a new cursor at its last leaf.
- **Writing deltas, or checkpoints mid-message.** A crash loses the partial text
  of the message that was streaming (§4.2).
- **Resuming an interrupted turn automatically.** The user retries from the
  nearest finalized ancestor. Running tool calls again by themselves would mean
  deciding which tools are idempotent, and that is not decided here.
- **A daemon serving several users, quotas, or isolation between sessions.**
  Anyone who can connect can open any session in the daemon's store.
- **The RPC revision.** It waits until `--connect` has been used enough (§7.4).
- **MCP.** It is not a priority.
