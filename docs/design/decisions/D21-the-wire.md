# D21: The wire: `tau serve` and `RemoteClient`

Status: accepted (2026-11-01), every Open question as recommended; proposed 2026-10-31. Phase 7, D16 stage 4. Amends D16 §5 (transport, calls on a
stopped driver, three new calls) and `model.md` §9. Depends on D18 (session ids, opening a
session, the shared start). Points marked "(review, 2026-10-31)" were decided in review.
**Built 2026-11-01** (7B-1: the contract and its coding; 7B-2: the server and the client), with
these differences from the text below, which stays as decided:
- The serve record's `started` is `since` (milliseconds since the epoch); `started` is the
  boolean `-d` adds when it prints the record (§9 used the name twice).
- A `RemoteClient` keeps its events in a `Feed` (§6): the same bounds on the client side, so a
  remote client that falls behind is cut off as a local one is (the server's buffer holds only what
  the socket has not taken). `RemoteClient.welcome` is what `hello` answered.
- A call on a client that was closed or cut off raises `ConnectionError`, local and remote alike
  (D16 §5 again: a client is its connection). A probe that times out is an error, not a stale
  record: only a missing or refusing socket is stale. A detached server's `.log` is removed on a
  clean exit and kept when the driver failed.
- `tau.Server` is public (`Server.start(driver, path, *, session, cwd, idle=None)`), because
  the Harness's remote mode serves in process through the public API (CLAUDE.md rule 4).
- `Started.serve(socket=None, *, idle=None)` serves a session this process started, as `tau serve`
  does, for a program that embeds τ (`start_session` -> `Started.serve`; amended 2026-11-12, for
  tau-substrate): it binds `<home>/serve/<id>.sock` (or `socket`) and writes the serve record, so
  `tau -c` in the session's directory attaches. `Server.close` is then the whole teardown, in this
  order: stop accepting and unlink the socket, stop the driver (each stream says `end`), let the
  connections finish, `Started.stop` (the lease), and last remove the record, if it still holds this
  server: no record still means the lease is free. `tau serve` goes through the same private helper
  (`serve_session`), the one writer of the record. `Server.start` itself is unchanged and writes
  nothing. `--stop` also counts as stopped a server whose socket refuses and whose session nobody
  holds (an embedder that died of the signal), and removes its stale record; the "served by pid N"
  advice names `tau serve --stop` only as the way if it is a `tau serve`.
  Follow-ups (2026-11-12): `Server.close` now, for every server, stops accepting and unlinks its
  socket, then stops the driver (so the driver need not be stopped first, and a second `close` is
  harmless); `Started.serve` refuses a socket a server already answers at (`tau serve --socket`'s
  check, now in the one helper); `tau serve` stops accepting before it winds its cursors down; and
  a client that finds a record whose socket refuses while its (live) process still holds the
  session is told "session X (pid N) is stopping; try again", the record kept: the record goes
  after the lease, so a record with no answering socket is stale only once nothing holds the
  session.
- `-d`'s wait is 5 seconds from the spawn, for the record to appear and answer (not only for a
  held lease): a server that is slower is left running, and `-d` again finds it. The child's output
  goes to a pending log until its id names `<id>.log`; a child that fails leaves
  `{"error": {"type", "message"}}` there, which the parent prints. The child is `python -m tau
  serve ... --detached-log FILE` (hidden flag; also what switches on the idle exit).
- `--stop` for a session with no answering server prints `{session, pid: null, stopped: false}`
  and exits 0 (a stale record is removed); it waits up to 5 seconds for the record to go.
- `--no-session` is refused: a served session has no id to name its socket and record.
- The schema no longer permits `"replay": null` on a `Call` part (the log refuses it): the field
  is a `Replay` or absent.
- Behavior suite: two tests that drop a client object without `close` are `local_only`; assertions
  about an exception's `__cause__`, and that a step is still running when `run` returns, are made
  in `local` mode only; `Harness.headless()` and `Harness.until()` are new.

## Context

D16 §5 says the wire is the `Client` contract serialized: "`tau serve` runs a `LocalClient`
per connection". The plan adds four things it must have on day one: `--schema`, a protocol
version checked at `hello`, discovery with `serve -d --json`, and command lines parsed and
completed on the server. The exit test: the behavior suite passes against a remote client as
against a local one.

**What exists (checked 2026-10-31).**
- `Client` (`_client.py`) has 22 async methods and is an event stream. Every argument and
  event is a plain frozen value except the core's `Node`, `Body`, `Author`, `Item`,
  `ItemData` and `Ending`, Rust values with one JSON encoding: the serde derives the log is
  written with (`tau-core/src/tree.rs`). **`Ending` had none** (it lived in `step.rs` with no
  serde derive, since the log never holds one); 7B-1 gave it the same derives, externally tagged
  like the rest (`"Done"`, `{"Failed": "E: x"}`).
- A `Feed` is bounded (`Live` dropped, an `Appended`/`Notice` backlog `CutOff`); the pump never
  waits on a client. A lost client's questions go to the pool.
- Path completion, the base tools' default `cwd`, and any relative path or subprocess in an
  extension resolve in the process's working directory. **The shared start changes the process
  into the session's working directory (D18 §1, §5), so these agree with the session's `cwd`,
  which `hello` reports** (review, 2026-10-31).
- After a failure every call raises the failure itself, after a stop `RuntimeError`; only the
  stream ends with `ConnectionError` (7B-1 changed this, §5). The `Harness` reads the session directly (the host's reads).

**τ 0.15's serve** (protocol `"0.12"`) was one daemon for every session of every directory
(`session_id` on each call) on TCP `127.0.0.1:8256` with an optional token; clients resumed
with `epoch`/`seq`/`since`; no command line was parsed on the server. **tau-code** (0.8.1)
runs `serve -d --json` per window, dials `ws://address`, keeps an `epoch`/`seq` replica,
completes `/commands` itself, and generates types from `--schema` (§10).

## Decision

### 1. One session per server process

`tau serve` serves exactly one session: the one the shared startup opens (D18's session flags)
with its extensions (D19), context (D20) and model (D13), in the session's working directory
(D18 §1). A connection is one `Client` of that session. No call names a session.

Why not many per daemon as in 0.15: **a process has one working directory** (the start changes
into it). Path completion, the base tools and every extension resolve relative paths in it, so
a daemon serving two projects runs one project's extension in the other's directory, and core
cannot fix that (extensions call `open("x")`). Also one process holds one lease, so a crash
takes one session down, and the wire stays the `Client` contract with no `session_id`.
Listing, creating and forking sessions are D18's CLI, not wire calls.

### 2. Transport and access: WebSocket over a Unix socket

- **Path.** A Unix socket, `$TAU_HOME/serve/<session id>.sock`, or `--socket PATH`.
  `$TAU_HOME/serve/` is created 0700 and refused if another user owns it or can write it; the
  file permissions are the authentication, and there is no token. A socket path past the system's
  limit (108 bytes on Linux) fails at start, suggesting `--socket`.
- **Messages.** One JSON-RPC 2.0 message per WebSocket text message, with no reliance on close
  codes (§3, `end`), so a later transport (Tectum's stdio) carries the same messages. The
  `websockets` keepalive closes a dead connection.
- **No TCP in 0.16.** Another machine uses OpenSSH's Unix socket forwarding; a browser needs
  TCP and a token (Open 4).

### 3. Messages

**`hello` comes first.** Params `{protocol: int, client: str, buffer?: int}`: `client` is the
stable id (`john@laptop:vscode`), `buffer` the `Feed` bound (256 by default). The result is
`{protocol, version, session, cwd, pid}`, where `cwd` is the session's working directory (D18
§1; the process is in it, so paths complete there). A different protocol is refused with
`ProtocolMismatch` (`data: {"server": 1}`) and the connection closed: exact match, no
negotiation. Any other request before `hello`, or a second `hello`, gets -32600 and the
connection closed. A good `hello` is `driver.client(client, buffer=buffer)`; the snapshot
events arrive before its result (Order).

**Calls.** One JSON-RPC method per `Client` call, same name, params by name only
(`{"cursor": 0, "text": "hi"}`); nothing returned is `null`. A batch or a client notification
gets -32600. **Calls begin in the order they arrive**, each running to its first wait before
the next begins (`input` then `step` keep their order), then run concurrently, so a `run`
waiting on its command does not hold back the `answer` its question needs. Responses come back
in any order, matched by id. Cancelling a call's await stops only the waiting.

**Events.** A notification `event` whose params are the event's fields plus `kind`, the class
name: `{"kind": "CursorView", "cursor": 0, …}`; a `Live`'s `delta` is tagged the same way.

**The end.** The last message is the notification `end`, then the server closes. Its params are
`{"error": null}` when the driver stopped, or an error object (§5): `CutOff`, or `Stopped`
with the driver's failure as `"Type: msg"`. `RemoteClient` turns these into the local stream's
three endings: the stream ends, `CutOff`, or `ConnectionError` (also a drop without `end`).

**Order.** One writer per connection sends everything. Before each response it sends every
event the client's `Feed` holds, and a call publishes its change before it returns, so **a
response arrives after the events its call caused**: when `open` returns 3, `CursorView` 3 has
arrived. That is stronger than local, weakens no local rule, and is what `settle` relies on.

### 4. Values

- **Core values** (`Node`, `Body`, `Author`, `Item`, `ItemData`, `Ending`) travel in the log's
  own JSON encoding, exposed by `tau-py` as a private encode/decode pair: one encoding of a
  node in the system, so a change to the log's encoding is a change to the wire (§8). Serde
  tags a variant externally (`{"User": "hi"}`) while events are tagged by `kind`: two styles
  in one schema, at the cost of a sentence in the guide.
- **Everything else** is encoded from its type hint (dataclasses as objects, tuples and
  collections as arrays, `Mapping`, `Json` and `object` as JSON), and decoding rebuilds the
  same frozen values, so a remote event equals the local one. A value passed to `answer`,
  `run` or `configure` must be JSON, as `Client` documents: a test that passes a `Path` works
  locally and fails remotely, and the suite against both enforces it.

### 5. Errors

Every exception a call raises has the code of the first class in its MRO that the table names;
`message` is `str(exc)`. `RemoteClient` raises the class the code names, so `except
NoSuchCursor` catches `CursorClosed` remotely as it does locally.

| code | name | `RemoteClient` raises | when |
|---|---|---|---|
| -32700, -32600, -32601 | (JSON-RPC) | `ProtocolError` | not JSON; not a request, or before `hello`; no such method |
| -32602 | InvalidParams | `TypeError` | params do not fit the call's signature |
| -32603 | Internal | `RuntimeError("Type: msg")` | an exception the table does not name: a server bug |
| -32001 to -32005 | Busy, NoSuchCursor, CursorClosed, NoSuchNode, NoSuchLabel | the same class | |
| -32006 | ProtocolMismatch | `ProtocolMismatch` | `hello` (kept from 0.15, which tau-code branches on) |
| -32007 | NotFound | `LookupError` | a closed or unknown question, an unknown command in `run` |
| -32008 | UnknownCommand | `UnknownCommand` | `parse_line` (§7): send the line as text, with a warning |
| -32009 | Refused | `ValueError` | a value the driver refuses; the message names the problem |
| -32010 | NotYours | `PermissionError` | a question for another connected client |
| -32011, -32012 | CommandFailed, ConfigError | the same class | |
| -32013 | Stopped | `ConnectionError` | the driver has stopped or failed (below) |
| -32014 | CutOff | `CutOff` | only in `end` |

The table is one mapping in the protocol module (`tau._protocol.ERRORS`); `--schema` emits it.
`ProtocolError` is a `RuntimeError` and `ProtocolMismatch` a `ProtocolError`. The error object is
`{code, message}` and, for the two rows that cannot be rebuilt from the message alone,
`data`: `ProtocolMismatch` carries `{"server": 1}`, `UnknownCommand` `{"name": "zz"}`. The code's
name is not on the wire; `ErrorCode`'s `x-names` has it. A row marked as not sent by exception
(the JSON-RPC codes, `InvalidParams`, `Internal`) is sent by the server for its own reasons.

**Amendment to D16 §5: a call on a stopped or failed driver raises `ConnectionError`**, locally
too (the failure's `"Type: msg"`, chained, or "the driver is stopped"; before, the failure
itself or `RuntimeError`): over the wire it can only be one, and it is the stream's rule already.
*Built (7B-1):* one function (`driver._running`) raises it, so `Driver.rest`, `add_item` and
`DriverState.check` follow the client calls.

### 6. Disconnects, questions and backpressure

- **A connection is not a client** (D16). When a connection closes, the server closes its
  `LocalClient`; if no other connection has that id, the client is lost and its open questions
  go to the pool (D16 §3). Its `watch` is gone until it calls `watch` again, which starts with
  the catch-up. A `run` it started keeps running (a command is the cursor's step); the
  response is dropped and its `Notice`s reach the id if it reconnects. Cursors keep stepping
  and questions keep waiting with nobody connected. Connections may share an id.
- **No resume** (D16: the log is the truth). A reconnect gets a fresh snapshot and reads
  `walk(at)`.
- **Backpressure is the `Feed`'s.** The writer waits on the socket's flow control while events
  stay in the client's `Feed`, whose bounds decide: `Live` is dropped, and an
  `Appended`/`Notice` backlog past `buffer` is `CutOff`, sent as `end`. The server adds no
  queue; the driver never waits on a socket.

### 7. Command lines on the server, and a range read

Three new `Client` calls, so local and remote stay one contract (amends D16 §5):
- **`parse_line(cursor, line) -> Parsed`** is `commandline.parse` with the driver's forms and
  the options forms the cursor sees. On the wire `Parsed` is `{command: Form, args, missing,
  extension, save}`: a client asks for `missing` (or shows the form), then calls `run` or
  `configure`. An unknown command is `UnknownCommand`; an unreadable line a `ValueError`.
- **`complete_line(cursor, line) -> list[str]`** is `commandline.completions` on the server:
  one round trip instead of one per form, option and named type.
- **`nodes(start, limit) -> list[Node]`**: up to `limit` nodes from id `start`, in id order;
  empty past the end; a negative `start` or `limit` is a `ValueError` (`Refused`). A tree view needs every node; with `children` alone that is a round trip
  per node. Ids are dense and nodes never change. `commandline` stays the pure library.

### 8. The protocol version and the schema

- **`PROTOCOL = 1`**, an integer. D12's rule: **a version marks a released protocol**,
  changing between tagged releases, never per commit. 0.16.0 ships 1; between releases the
  wire changes freely and a client built from master regenerates.
- **`tau serve --schema`** prints JSON Schema 2020-12, generated from `Client`'s method
  signatures, the event dataclasses, the error table and the core values' schemas (from
  `schemars` derives beside their serde derives, Open 6). Nothing is listed by hand. Shape: `$defs/<method>` is its params object with
  `x-result`; `$defs/Event` a `oneOf` over `kind`; `$defs/End`; `$defs/ErrorCode` an enum with
  `x-names`; `x-protocol` at the top. Params are closed, results open, as in 0.15. `close` is not a message: closing the
  connection is how a client disconnects (§6). `hello` is in the schema as a method like the
  others, with `Welcome` as its result.
- **Checked in** as `docs/protocol.schema.json` (written by `scripts/schema.py`, compared by
  `scripts/schema.py --check` in `scripts/check.sh`, which CI runs; `tau serve --schema` prints
  `tau._protocol.schema_text()`) and compared in CI the way the diagrams are, so
  any wire change is a readable diff. **At release:** if the schema differs from the last
  tag's, `PROTOCOL` goes up by one.

### 9. `tau serve`, `RemoteClient`, and the suite against both

- `tau serve [startup flags] [--socket PATH]` runs in the foreground (startup flags: D18's
  session flags, D19's `-e` and `--no-extensions`, D20's prompt flags). It opens the session
  with the shared startup (D18 §5), binds the socket, and writes the **serve record**
  `$TAU_HOME/serve/<session id>.json`: `{socket, pid, session, cwd, protocol, version,
  started, log}`. On SIGTERM or SIGINT it first aborts every cursor and waits for each to rest (bounded
  by 3 seconds, `WIND_DOWN_SECONDS`), so a running tool is cancelled and an agent's tool closes the
  agent's cursor (D24 §8), as `-p`'s SIGINT does (8E-5; before, a stop was crash-like and left the
  agent open and "still working"), then it stops the driver (each stream gets `end` with
  `null`), removes the socket and record, and exits 0; if the driver fails, it sends `end` with
  `Stopped`, cleans up the same way, and exits 1.
- `tau serve -d [--json]` starts the server detached (output to `$TAU_HOME/serve/<session
  id>.log`) and prints the record once a `hello` probe answers. The parent asks what needs a
  person (trust) first, then spawns a child with no terminal.
  - **Idempotent.** A record whose socket answers `hello` is printed with `"started": false`
    and nothing starts. A record whose socket does not answer is stale (a crash): removed, and
    the server started. Two `-d` at once race for the lease: the loser's child fails with
    `Leased`, and its parent prints the winner's record with `"started": false`.
  - **A session held by a process that is not a server** never yields a record. The parent
    waits a bounded time for one (5 seconds, a constant) and then fails with D18's `Leased`
    error, naming the session and suggesting `--fork` (review, 2026-10-31). The same bound
    covers the race above; there is no infinite wait.
  - A failure before the record appears (`ContextFileError`, `ExtensionLoadError`, `Leased`)
    is printed with its message intact, and exits 1. **`--json` output is one JSON object per
    line** (review, 2026-10-31): the record, an error `{"error": {"type", "message"}}`, and
    `--stop`'s `{session, pid, stopped}`. The record file is one object; `--schema` prints one
    schema document.
- `tau serve --stop [session flags] [--json]` checks with `hello` that the recorded pid answers
  on its socket and sends SIGTERM (the stop above: abort, close agents, then exit). There is no `shutdown` call on the wire.
- **Discovery** is the record (`$TAU_HOME/serve/<id>.json`, or list the directory). The Phase 8
  TUI opening a served session connects to it instead of failing with `Leased`.
- **`RemoteClient`** (`tau.RemoteClient`): `await RemoteClient.connect(socket, client, *,
  buffer=256)` does the `hello` and returns a `Client`. It raises `ProtocolMismatch`, a
  `RuntimeError` and not a `ConnectionError`, so the reconnect rule does not loop on it. One
  reader task routes responses by id and queues events for `async for`. pyright holds it to
  `Client`.
- **The suite against both** (the exit test). An autouse fixture in
  `tests/behavior/conftest.py`, parametrized over `local` and `remote`, sets the `Harness`'s
  mode. In `remote` mode the `Harness` runs the server in process over a real Unix socket in a
  short temporary directory, and `client`, `connect(id)` and `view()` are `RemoteClient`s; what
  reads the host (the session, `driver.state()`, snapshots, `add_item`, `crash`) stays the
  host's. `settle` adds one round trip per remote client once the driver is quiet; by §3's
  order its response comes after every event published before it. Tests that call
  `driver.client(...)` move to `h.connect(...)`. Wire-only tests go in `tests/serve/`
  (handshake, bad params, each `end`, `-d` idempotence, race, stale record and bounded
  `Leased` wait, `--stop`, the schema).

### 10. What tau-code changes

tau-code (0.8.1) must change its transport and its model of state. It dials the serve
record's Unix socket (`ws+unix://`) with one server and connection per session, listing with
`tau sessions --json` and opening, creating or forking with `tau serve -d --json <session
flags>`. It says `hello {protocol: 1, client}` with no token, and replaces the `epoch`/`seq`
replica with a snapshot on connect plus `walk` and `nodes` reads, reconnecting on `end` or a
drop. Its deltas, question verbs and `/command` handling move to `Live`, `answer`,
`parse_line` and `complete_line`, and its generator learns the new schema shapes. The
row-by-row mapping belongs with that work in tau-code's own repository, after 0.16.0.

## Amends, files and formats

- **D16 §5:** the transport is WebSocket over a Unix socket; a call on a stopped or failed
  driver raises `ConnectionError`; `Client` gains `parse_line`, `complete_line` and `nodes`;
  "Phase 6" becomes Phase 7. **`model.md` §9:** the same, plus a paragraph on the wire. **D12** is applied, not changed: `PROTOCOL` follows its rule for versions.
- New, none persistent: `$TAU_HOME/serve/` (0700) holding `<session id>.sock`, `.json` (the
  serve record, removed on exit) and `.log` (`-d`); `docs/protocol.schema.json`, compared in
  CI. No session file changes; `VERSION` is untouched. `websockets` becomes a runtime
  dependency, not an extra: it is small, and "install `[serve]`" asks no real decision.

## Alternatives

| option | why not |
|---|---|
| One daemon, many sessions (0.15); one server per working directory | One working directory per process, so another project's extension runs in the wrong place; routing on every call; one crash takes every session down. |
| TCP on loopback with a token file | A token is a second access check beside the file permissions every `$TAU_HOME` file relies on. TCP waits for the web client. |
| WebSocket close codes for `CutOff` and failure | They do not exist on stdio; `end` carries the error object. |
| Python-side codecs for the core values; wire-only `parse_line`/`complete_line` | A second description of `Node` that would drift; local and remote contracts that differ, so the suite could not hold them to one. |
| A schema digest at `hello`; a `shutdown` call; a response that may overtake events | D12: versions mark releases. Anyone connected could stop everyone's server (a signal to the recorded pid is the Unix way). Clients would need their own barrier. |
| `-d` waits forever when a non-server holds the session | A hang with no message; the bounded wait fails with `Leased` (§9). |

**0.15 behaviour dropped:** the multi-session daemon; TCP, port 8256, the token and the
`Origin` check; the `serve.*` config keys; `--web-root`, `--listen`, `--connect` (Phase 8
decides the TUI's); `shutdown`; `epoch`/`seq` replay; 0.15's parked-question flag at `hello`.
Elide, commit-branch, paste-subtree, compare and the panel and step-debugger verbs have no
wire counterpart (the plan sends them to commands or extensions, or drops them).

## Open (accepted as recommended, 2026-11-01)

1. **Who owns the shared startup?** Answered by D18 §5, as recommended.
2. **What D18 must give.** Answered by D18 §1-2 (a 22-character id; `tau sessions --json`).
3. **Idle exit for `-d`.** *Recommend:* a `-d` server exits after 10 minutes with no client
   connected and every cursor at rest (a cursor with an open question is busy); a foreground
   server never exits on its own.
4. **TCP for browsers.** *Recommend:* decide it with tau-code's web client, after 0.16.0:
   loopback only, with a token file `$TAU_HOME/serve/<id>.token` (0600) sent in `hello`.
5. **Reload over the wire.** *Recommend:* nothing in 0.16 (D19 §9).
6. **`schemars` and tau-core's dependencies** (review, 2026-10-31). §8 derives the core values'
   schemas with `schemars` beside their serde derives, which adds a dependency to tau-core,
   meant to stay minimal. Checked: tau-core already depends on `serde` and `serde_json` and
   holds a dozen serde derives in `tree.rs` (`Part` and `Replay` convert through wire types).
   tau-py wraps each core value in its own Python class and does not use those derives today
   (tau-store encodes the log), so §4's encode/decode pair is new tau-py code calling them. A
   derive must sit on the type's definition; in tau-py it would need mirror types (§4's drift).
   Options: (a) `schemars` as a plain tau-core dependency; (b) an optional `schema` feature on
   tau-core (`cfg_attr(feature = "schema", derive(JsonSchema))`) that only tau-py turns on; (c)
   a separate crate with mirror types; (d) hand-written schemas in Python checked against round
   trips. *Recommend (b):* no drift, and tau-core, tau-store and the Rust tests build without
   the new dependency.

**Not verified, inferred:** that the `schemars` derives sit cleanly beside `Part`'s and
`Replay`'s custom serde; that tau-code's `ws` dials `ws+unix:` as documented; the line counts.

## Glossary additions

New: **tau serve**; **wire** (the `Client` contract as JSON-RPC 2.0 over WebSocket on a Unix
socket; not `providers._wire`); **serve record**; **RemoteClient**; **hello**; **end**;
**PROTOCOL**, **ProtocolMismatch**, **ProtocolError**; **error code**; **parse_line**,
**complete_line**, **nodes**. Amended: **Client**, **Harness** (local and remote modes).

## Build list (rough lines, tests separate)

| item | lines |
|---|---|
| `tau-py`: private encode/decode for the six core values; the `schema` feature and derives in `tau-core` (Open 6); schema export | ~100 Rust |
| `tau._protocol` (coding, error table, `PROTOCOL`, schema) and `tau._serve` (`hello`, dispatch, the ordered writer, `end`, the record, signals), both private | ~450 |
| `tau.RemoteClient`; `tau serve` CLI (foreground, `-d --json` with the bounded wait, `--stop`, `--schema`) | ~300 |
| `Client` additions (`parse_line`, `complete_line`, `nodes`; `ConnectionError`) | ~50 |
| tests: `Harness` modes and `settle` (~100); `tests/serve/` (~300); docs (schema file, glossary, `model.md` §9) | ~400 |
