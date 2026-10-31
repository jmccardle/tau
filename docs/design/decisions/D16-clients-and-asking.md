# D16: Clients and asking

Status: draft (2026-10-30), Phase 5's first step. Amends D15 (events), `model.md` §7
(questions) and §9 (events and frontends).

## Context

The primary target is `tau agents`: one session, many cursors acting at once, several
frontends attached over a network, each watching one cursor at a time. The Phase 4
contract was built for one in-process frontend:

- **Questions are polled.** `cursor.ask` emits no event; a frontend learns of a question
  only by calling `driver.questions(cursor)`. `respond(cursor, …)` answers "the cursor's
  oldest", which is a race once two clients can answer.
- **`Saved` says "go read the tree".** Over a network every `Saved` costs a round trip.
- **`HookError` is a special case**: live only, never replayed, "expect none for a failure
  that happened before you subscribed".
- **Nothing is replayed on subscribe**, so a late frontend must know which reads rebuild
  the state, and a race between subscribing and reading is the frontend's problem.
- **Nothing typed flows either way** between a person and an extension: no typed question,
  no command with arguments, no extension config. τ 0.15's version of this (flows,
  domains, vocabularies, four ways to ask, about 10,000 lines across core and two
  frontends; extension-typed commands had no users) is the cautionary example. Its
  experience is worth keeping: `/` lists commands with descriptions, Tab completes typed
  values, a bare command opens a form, a missing argument is asked for, an unknown command
  warns that the line goes to the model.

Two directions, two names:
- a **question** is an extension asking a person (extension → frontend);
- a **form** is a person asking an extension (frontend → extension): run a command, or
  change its config.

## Decision

### 1. A frontend holds a `Client`, local or remote

A frontend never touches `Driver` or `Session`. It holds a `Client`: async calls, reads,
and one event stream. Two implementations, one protocol:

- `LocalClient(driver, client_id)`: in-process; the transport is free.
- `RemoteClient(socket)`: `tau serve` runs a `LocalClient` per connection and serializes
  calls and events generically (every call argument and event is a plain frozen value), so
  there is no per-call wire code.

What a network adds, and nothing else:
- **Identity.** A client names itself when it connects (a stable id such as
  `john@laptop:tui`), so a reconnect is the same client; its actions are
  `by: Frontend(client)`. A connection is not a client: a network blip changes nothing in
  the session and declines nothing.
- **Loss.** The stream ends with an error. Reconnecting is a fresh connect (a snapshot,
  §2); there is no resume protocol, because the log is the truth.
- **Transport.** A Unix socket with file permissions first. TCP and authentication are
  not part of this decision.

The `Harness` drives a `Client`, so the behavior suite tests the contract once for both.

### 2. State is sent whole; growth is sent as it happens

Small state is sent whole whenever it changes, never as a delta. Every client receives it
for every cursor, which is what lets a frontend show "cursor 3 is waiting on you" while it
watches cursor 1, and makes switching the viewed cursor a local lookup.

| event | to | carries |
|---|---|---|
| `CursorState(cursor, …)` | all clients | `at`, busy, auto-step, open questions (§3), status per extension (`cursor.show`), pending items, the last step's ending including a hook's error |
| `Labels(labels)` | all clients | every label and its node |
| `Appended(nodes)` | all clients | the new nodes, bodies included |
| `Live(cursor, delta)` | clients watching the cursor | text, thinking, call and tool-progress deltas, as in D15 |
| `Notice(text)` | one client | `cursor.tell`, a refused input |

- **On connect** a client receives `Labels`, one `CursorState` per cursor, and the tree's
  node count before anything else; nodes below the count it reads as calls (`node`,
  `walk`, `children`), nodes from it on arrive as `Appended`. Subscribing and the snapshot
  are one act on the driver's loop, so nothing falls between them.
- **Order.** One ordered stream per client. A change's `Appended` comes before the
  `CursorState` that points at its nodes. A `CursorState` or `Labels` that replaces an
  earlier copy removes it and goes to the end, so it never overtakes what it refers to.
- **`watch(cursors)`** chooses which cursors' `Live` a client gets. Watching a cursor
  starts with a catch-up, taken in the same act as the change of watch: the parts of the
  sample in flight that are not in the tree yet, and the text of the open part (the driver
  already keeps both, which is how a cancelled reply saves what arrived). Tool progress is
  not kept, so it has no catch-up; the result arrives as `Appended`.
- **Bounds.** `Live` is dropped when the client's buffer is full (as in D15). `CursorState`
  and `Labels` replace their own earlier copy, so they cost one slot per cursor. A client
  whose `Appended` and `Notice` backlog passes the buffer is cut off and reconnects to a
  snapshot. No event carries a "dropped" mark.
- **Gone:** `Saved`, `HookError` as an event (it is part of `CursorState`, so a late
  client sees it), and `driver.questions()` as the way to learn of a question.

### 3. Questions

`await cursor.ask(text, schema=None, target=Anyone) -> value | None`.

- A question has an **id**, its cursor, a **target** (`Anyone` or one client), its text,
  and an optional schema (§5). No schema means a string answer.
- It appears in its cursor's `CursorState` until it closes. `answer(question_id, value)`
  answers it; `None` declines. The driver validates the value against the schema; an
  invalid value is refused to the answering client and the question stays open.
- The first answer wins. A later answer to a closed question raises `LookupError` (as
  `respond` does today).
- Abort and go-to decline the cursor's questions, as now. A question for one client stays
  open across that client's disconnects (it is answered when the client returns, or
  declined by anyone's abort or go-to); asking a client that is not connected declines at
  once, so a cursor never waits on someone who is not there.

### 4. Forms: commands and extension config

**Commands.** `@command` is `@tool`'s machinery for a person: an async function whose
first parameter is the `Cursor` handle and whose other parameters are typed. Its name,
docstring and schema come from the function, as for a tool. Commands are declared, found
on the extension like its hooks when the driver is built; no extension code runs to list
them. A value only known at run time (the current todo ids) is asked for by the command
itself, with a question whose schema carries the values.

- **Running.** `run(cursor, command, args)` is refused (`Busy`) unless the cursor is at
  rest: commands do not queue. It validates `args` against the schema and adds an
  `ItemData.Extension` item addressed to the command's extension, its data naming the
  command, the arguments and the client; the item wakes the cursor. At that step's
  `pre_step` the driver removes the item, then calls the command, before the point's
  extension hooks, so they never see it. No core change: it is the existing "add an item,
  the cursor wakes, `pre_step` acts on it" path.
- **What follows from that.** The command holds the cursor busy and can `ask`. It runs at
  most once: a crash before it starts loses it, and a crash during it does not re-run it.
  Abort cancels it (the driver's task; unlike a hook, which abort lets finish, model.md
  §8), so `/compact` stops when told to. The rest of the step follows the usual rules: a
  command that moves the cursor to a `User` node with auto-step on gets a sample, as a
  hook's move would.
- **What the person sees.** `/` lists every command with its description; Tab completes
  the name, then each argument (§5); an unknown name warns that the line goes to the model
  as text; a command missing a required argument opens a form for the rest.
- **One registry.** Built-in commands (`/model`, `/tree`) are commands of a built-in
  extension, not a separate list. Names are unique across extensions; a duplicate fails
  when the driver is built, like a duplicate extension name.

**Extension config.** An extension declares `config_schema`. Its config lives in the D13
layers under `extensions.<name>`, and a `Config` node may override it for a branch with
the same shape; `config_at` returns it under that key. `tau config check` validates every
extension's config against its schema. A client reads `config(extension)` (schema and
current value). Changing it is a command, the built-in `/config <extension>`, whose form is
that schema and which writes a `Config` node on the cursor's branch (recorded, replayed,
branch-local). The user's file stays `tau config`'s to edit.

### 5. One schema, one completer, one validator

The schema is the JSON Schema subset `@tool` already produces from type hints: `str`,
`int`, `float`, `bool`, `list[T]`, `Literal[...]` of str or int, `T | None`. To it D16 adds
**named formats**, a closed set: `path`, `model`, `cursor`, `label`, `node`, written as
types (`pathlib.Path`, and `Model`, `CursorRef`, `LabelName`, `NodeRef` from `tau_next`).

Completion has two sources and no callbacks:
- **Fixed values** (`Literal` in a command's signature, or an `enum` in a question's
  schema).
- **Named formats**, answered by `complete(cursor, format, prefix)`, a `Client` call the
  driver implements. Paths are completed on the driver's machine, which matters when the
  client is remote.

No extension code runs per keystroke, so completion cannot hang, raise, or need to be
async on the extension's side. Every frontend needs one form renderer, one completer and
one validator; the driver's validator is the authority, the frontend's only spares a round
trip.

## Open

- **A command that should not sample.** A command that moves the cursor to a `User` node
  with auto-step on gets a sample (§4). If `/prune` or `/compact` finds that surprising in
  practice, the rule to add is "a command's step does not sample unless the command turns
  auto-step on"; nothing is added before a real command shows the need.

Settled while drafting (from review): edited extension config lands as a `Config` node
(§4); `CursorState` carries pending items whole, revisited only if measured; an extension
may still treat plain `/…` input in a `pre_input` hook, and the built-in frontends warn and
send an unknown `/name` as text (§4). Trust's hash scope once `.tau/` holds hooks is not a
client question; it moves to Phase 5's trust work.

## Alternatives

| option | why not |
|---|---|
| Fine-grained state events (`question_opened`, `status`, `cursor_moved`, …), as model.md §9 sketched | Every kind needs its own replay on connect and its own merge rule; whole state is one rule, and it is small. |
| Subscribe per cursor | A client would not see another cursor's question; switching would need a request and would race. |
| Questions polled (Phase 4) | A frontend cannot know when to poll; over a network it costs round trips forever. |
| Commands as `pre_input` hooks that parse text (model.md §7 today) | The client has already parsed and validated the arguments; a hook re-parsing text is pure loss, and completion needs declarations anyway. |
| A command as a new kind of core job, or a new item kind | The `ItemData.Extension` path already gives a busy cursor, `ask` and a step to run in; a new job kind is a core change for nothing. |
| Commands queue while the cursor is busy | A queued command strands when the step ends other than `Done` (no next step starts) and then runs on whatever wakes the cursor next, long after the person moved on. Refusing is one rule. |
| `commands()` computed per listing, for dynamic values | Extension code runs on every `/`; the command asks with an `enum` instead, at run time. |
| A client is a connection | Every network blip would decline that client's questions (a permission question becomes a deny) and fill `by` with throwaway ids. |
| Completion callbacks into extensions (τ 0.15's enumerators) | Extension code per keystroke: can hang, raise, block a remote client; and needs a sync/async rule. |
| A frontend reads `Driver` and `Session` directly when local | Two contracts, and the remote one rots; τ 0.15's `RemoteBackend` mirrored every call by hand. |

## Consequences

- Code (Phase 5, in this order):
  1. `Client`, `LocalClient`, the §2 events and the snapshot; the `Harness` and the tests
     move to them, and `Saved`, `HookError` and polling go;
  2. questions with ids, targets and schemas;
  3. `@command`, `run`, `complete`, extension config and `/config`;
  4. `tau serve` and `RemoteClient` stay Phase 6, but the suite then runs against both.
- Glossary: client, `LocalClient`, `RemoteClient`, cursor state, `Appended`, `Labels`,
  `Notice`, watch, question (amended: id, target, schema), form, command, named format,
  extension config. Removed: `Saved`, `HookError`.
- `model.md` §7 (questions) and §9 (events) are rewritten to this; §7's "commands and gates
  are `pre_input` hooks" becomes "gates are"; D15's frontend rules 1–5 are replaced by §2.
- Phase 5's permissions extension is the first typed question (allow, deny, always);
  compaction and prune are the first commands (`/compact`, `/prune`).
