# τ serves its trees: the 0.12.0 plan

Plan and cost record (2026-10-02), nothing built. This record continues
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
- **Two concurrent top-level turns on separate cursors are untested.**
  `test_cursor.py` covers opening, moving and isolating cursors. Concurrent
  sub-agents are tested elsewhere. Nothing runs two user turns at the same time
  on two cursors of one session, and that is what §8 needs.
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
once, so the `SessionLog` contract suite gains that rule as a test. The JMFTS
store must update the document in place, or follow the same last-wins rule. I
did not check which of those its client supports.

### 4.2 What is written when

| Event | Written |
|---|---|
| Turn admitted | The user message (and queued inputs), finalized at once |
| `message_start` (assistant) | The entry, opened, with no content |
| `message_end` | The entry, finalized, with its content and usage |
| Tool call starts | The tool-result entry, opened, holding the call's name and arguments |
| Tool call ends | The tool-result entry, finalized |

Deltas are not written. A crash therefore loses the partial text of the message
that was streaming, but keeps the fact that it was streaming, and everything
finished before it. Writing a checkpoint every N deltas could be added later. It
is absent here (§11).

This replaces the end-of-turn write in `_run_one_turn`. The `_TurnPersistence`
latch (`inputs_written`, `loop_written`) exists only because writing happened
late, so it goes away.

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

**`cursor` means a cursor.** The new protocol names entry ids `leaf` and
`entry_id` from its first version. The RPC rename (ROADMAP, "RPC says `cursor`
where it means an entry id") follows it before the release.

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
  else. I have not picked a number yet.

### 6.3 Unix sockets

`asyncio` serves a unix socket with the same handler as TCP, and `websockets`
supports one (`unix_serve`), so I expect `--listen unix:` to be a single branch
in argument parsing (`inferred`). It ships only if it is that small. On Windows
the option is missing, and TCP behaves the same on every platform.

### 6.4 What runs in the daemon

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

### 7.2 Starting a local daemon automatically

With `"serve": {"autostart": true}` in the config, a plain `tau` first tries to
connect to the configured address. If nothing answers, it runs `tau serve -d`,
waits until the port accepts connections, and then connects. Doing this twice is
harmless: a second daemon fails to bind the port, and that error ends it. No
lock file is needed.

### 7.3 tau-code

The web client and VS Code speak the stream protocol directly to `tau serve`.
`packages/server` and `packages/runner` are deleted (991 lines). When a prompt
is sent to a busy cursor, it is now queued as a follow-up. That replaces
`multitask_strategy: 'reject'` (`packages/ui/src/useTau.ts:190`). A user who
wants a parallel turn opens another cursor instead. tau-code ships a release
alongside 0.12.0.

### 7.4 RPC stays

`tau --mode rpc` is the stdio head: a process that wants an exclusive agent
without importing τ uses it, whether that is a ROS node, a web server or an
operating system. It is not deprecated. It gets the `cursor` rename before
0.12.0. It gets a revision after `--connect` has been used enough to show what
driving τ remotely needs, and that revision is not in this pass.

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

- The `cursor` → leaf/entry-id rename on the RPC wire (ROADMAP, Open work).
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
