# D16: Clients and asking

Status: accepted (2026-10-30), Phase 5's first step. Amends D15 (events), `model.md` §7
(questions) and §9 (events and frontends).

## Context

The primary target is `tau agents`: one session, many cursors acting at once, several
frontends attached over a network, each watching one cursor at a time. The Phase 4
contract was built for one in-process frontend:

- **Questions are polled.** `cursor.ask` emits no event; a frontend learns of a question
  only by calling `driver.questions(cursor)`. `respond(cursor, …)` answers "the cursor's
  oldest", which is a race once two clients can answer.
- **`Saved` says "go read the tree".** Over a network every `Saved` costs a round trip.
- **`HookError` is a special case**: live only, never replayed.
- **Nothing is replayed on subscribe**, so a late frontend must know which reads rebuild
  the state, and a race between subscribing and reading is the frontend's problem.
- **Nothing typed flows either way** between a person and an extension. τ 0.15's version
  (flows, domains, vocabularies, four ways to ask, about 10,000 lines; extension-typed
  commands had no users) is the cautionary example. Its experience is worth keeping: `/`
  lists commands with descriptions, Tab completes typed values, a bare command opens a
  form, a missing argument is asked for, an unknown command warns that the line goes to
  the model.

Two directions, two names:
- a **question** is an extension asking a person (extension → frontend);
- a **form** is a person asking an extension (frontend → extension): run a command, or
  change an extension's options.

The test of this decision is the length of the simple cases (§6). If a permission
prompt, a command that is also a tool, or an extension's options take more than a
screen, the design is wrong, not the example.

## Decision

### 1. One type language

Tool parameters, command parameters, a question's answer and an extension's options are
written as Python type hints, and one private module (`tau._values`) turns a hint into a
schema, checks a JSON value against it, and converts it to what the function receives.
Nothing else describes a value.

| hint | JSON | the function gets | a frontend shows |
|---|---|---|---|
| `str`, `int`, `float`, `bool` | as JSON | as JSON (a `float` gets an integer as a float) | a field, a checkbox |
| `Literal["a", "b"]` (str or int) | the value | the value | a dropdown |
| `list[T]` | array | `list` | a multi-select of `T` |
| `T \| None`, or a default | may be absent or `null` | `None` or the default | an optional field, prefilled |
| `Annotated[T, "text"]` | as `T` | as `T` | `T`, with the text as help |
| `pathlib.Path` | string | `Path`, a leading `~` expanded | a file picker, completed on the driver's machine |
| `Model` | string | `str` | a searchable list of the configured models |
| `CursorId` | integer, or `"new"` | `int` | a list of the open cursors, and "new" |
| `LabelName` | string | `str` | a list of the labels |
| `NodeId` | integer | `int` | the nodes of the cursor's walk, newest first |

- The last five are **named types**, a closed set. `Model`, `CursorId`, `LabelName` and
  `NodeId` are `NewType`s; a value of one is checked against the session (a model that is
  not configured, a cursor that is not open, is refused like a wrong type).
- `CursorId` `"new"` opens a cursor at the asker's node (the running cursor's `at`, or the
  batch's node for a tool) after every other argument has been checked, and the function
  receives its id. Nothing else in the language has an effect.
- `Annotated[T, "text"]` replaces `@tool(describe=…)`: the description sits on the
  parameter it describes.
- A model sees the same schema, without the named types' format, which no provider API
  accepts. The `Literal`s and descriptions it keeps.

### 2. Functions are tools and commands

An extension lists plain functions, sync or async, with a docstring and typed parameters:

- `tools`: offered to the model; the result (`str`) is the call's outcome.
- `commands`: offered to a person as `/name`; the result (`str` or `None`) is a `Notice`
  to the client that ran it.

The same function may be in both lists. No decorator and no wrapper: the driver describes
each function when it is built (`DefinitionError` names the extension, function and
parameter). The driver's own `tools=` takes plain functions the same way, and any object with
a `call` is a `Tool` as before. A module with a `name` is an extension.

Parameters the driver fills, by annotation alone (a name says nothing), are not arguments:

| annotation | as a tool | as a command |
|---|---|---|
| `Progress` | the call's `ToolProgress` | a `Notice` to the client |
| `TreeHandle` | the batch's `NodeHandle` | a handle bound to the extension, with no cursor of its own: it reads and writes the tree, opens cursors, sets labels, adds items; no `walk`, `move`, `ask`. It runs at once, whatever the cursors are doing. With a `Cursor` parameter too, the same object as the `Cursor` |
| `NodeHandle` | the batch's `NodeHandle` | refused when the driver is built |
| `Cursor` | refused when the driver is built | the cursor it runs on |

So a function that needs nothing is both; one that reads or writes the tree is both; one
that moves a cursor is a command. Writes are by `Extension(<the extension's name>)`.

**Running a command.** `run(cursor, command, args)` checks `args` (§1).
- A command that takes `Cursor` runs as a step of that cursor. It is refused (`Busy`)
  unless the cursor is at rest: commands do not queue. The driver keeps the command in
  memory and asks for a step (`Session.step`); at that step's `pre_step` it runs the
  command first, with the cursor's handle, then the extensions' `pre_step` hooks. The rest
  of the step follows the usual rules (model.md §6): a command that leaves the cursor on a
  `User` node gets a sample, one that leaves it where a reply already is gets none. Abort
  cancels a running command (unlike a hook, which abort lets finish). A crash loses a
  command that has not finished: it is never re-run.
- Any other command runs at once, on the driver's loop, whatever the cursor is doing.

`Client.run` returns the command's result (`str | None`) when the command has finished and
raises `CommandFailed` (a typed error whose message is the failure as `"Type: msg"`) if it
raised, at the moment it raises (a command that takes a `Cursor` still ends its step `Failed`
afterwards). `progress` calls arrive as `Notice`s.

**Built-in commands** are `/model`, a command of the built-in extension `tau`, and `/config`,
whose values are a mapping the type language has no hint for, so it is a call, not a function
(§4). Command names are unique across extensions; a duplicate, or `config`, fails when the
driver is built.

### 3. Questions

`await handle.ask(text, expect=str, *, to=None) -> value | None`, on `Cursor` and on
`NodeHandle` (a tool hook asks on its batch's cursor).

- `expect` is any hint of §1: `Literal["allow", "deny", "always"]`, `Model`,
  `list[pathlib.Path]`. The value comes back checked and converted; `None` is a decline.
- A question has an **id**, its cursor, `to` (`None`: anyone; or a client id), its text and
  its schema. It is in its cursor's `CursorView` until it closes.
- `answer(question_id, value)` answers it. The driver checks the value; a wrong one is
  refused to the answering client (`ValueError` naming the problem) and the question stays
  open. The first good answer wins; a later one raises `LookupError`.
- **Questions belong to the cursor, not to a client.** A question never declines for want of
  someone to answer it: asked of anyone (`to=None`) it stays open and blocks until a client
  answers, even with none connected. Under `tau serve` the agent waits there; an extension that
  wants to give up on silence does so itself (a timeout around `ask`).
- `to` names the client a question is first for, and only while that client is connected: a
  question to a client that is not connected goes into the **pool** at once (`to` becomes `None`,
  anyone may answer), and so does one whose client disconnects while it is open (the cursor's
  `CursorView` is sent again, so the other clients see they may answer). It stays in the pool if
  that client returns. While the client is connected only it may answer (`PermissionError` for
  another).
- Abort and go-to decline the cursor's questions, and a question asked later in the same step
  (a `post_step` hook, a second `ask`) is declined at once: an abort leaves nothing answerable
  behind. The cursor may ask again once its next step begins.

### 4. Options and config

An extension's **options** are a frozen dataclass named by its `options` attribute; each
field is a hint of §1 with a default (a field with none must be configured; a `list[T]` field's
default is a tuple, since a dataclass refuses a list, and its value is a list). They live in
the D13 layers under `extensions.<name>`, and a `Config` node may override them for a
branch. `handle.options()` returns the dataclass as it stands at the handle's node.
`tau config check` checks every extension's options.

A cursor handle writes a `Config` node holding just what changes under `at` and moves onto it:
`cursor.set_model(model)` sets the branch's model, and `cursor.set_options(**changes)` sets the
calling extension's own options, checked against its options dataclass folded with the walk's
(an extension cannot write another's). Both are checked before anything is written. `/model
<Model>` is a built-in command doing the first. `/config` is a call, `Client.configure(cursor,
extension, values)`: a step of the cursor writes the extension's options, the one write across
extensions, which stays private to it. A frontend reads an extension's options with
`Client.options(cursor, extension)`, a `Form` whose schema carries each default as the value now;
`commandline.parse` reads `/config step_limit limit=10` by that schema, and a bare `/config
step_limit` is the frontend showing the form. The user's file stays `tau config`'s to edit.

### 5. A frontend holds a `Client`, local or remote

A frontend never touches `Driver` or `Session`. It holds a `Client`: async calls, reads,
and one event stream. Two implementations, one protocol:

- `LocalClient(driver, client_id)`: in-process; the transport is free.
- `RemoteClient(socket)` (Phase 6): `tau serve` runs a `LocalClient` per connection and
  serializes calls and events. Events and arguments are plain frozen values (events compare
  by value; those holding a schema, a dict, are not hashable), but the core's own values
  (`Node`, `Body`, `Author`, `Item`, `ItemData`, `Ending`) need one codec each, written in
  Phase 6. Nothing is per-call.

A network adds identity (a client names itself with a stable id such as
`john@laptop:tui`, so a reconnect is the same client and its actions are
`by: Frontend(client)`; a connection is not a client), loss (the stream ends with a
`ConnectionError`, `CutOff` or the driver's failure as `"Type: msg"`, chained; a frontend's one
rule is `except ConnectionError`, then reconnect to a fresh snapshot; there is no resume
protocol, because the log is the truth) and transport (a Unix socket with file permissions first). Nothing else.

**Events.** Small state is sent whole whenever it changes, never as a delta; every client
receives it for every cursor, so a frontend can show "cursor 3 is waiting on you" while
it watches cursor 1, and switching the viewed cursor is a local lookup.

| event | to | carries |
|---|---|---|
| `CursorState(cursor, …)` | all clients | `at`, busy, auto-step, open questions, pending items, the last step's ending including a hook's error |
| `Labels(labels)` | all clients | every label and its cursor |
| `Appended(nodes)` | all clients | the new nodes, bodies included |
| `Live(cursor, delta)` | clients watching the cursor | text, thinking, call and tool-progress deltas, as in D15 |
| `Notice(text)` | one client; every client for `cursor.tell` without `to` | `cursor.tell`, a refused input, a command's result and progress |

- **On connect** a client receives one `CursorState` per cursor and `Labels` (after the cursors it
  names), before anything else; nodes it reads with calls (`node`, `walk`, `children`), and
  those added later arrive as `Appended`. A node never changes, so a client that read one and then
  meets it in an `Appended` ignores the second. Subscribing and the snapshot are one act on the
  driver's loop, so nothing falls between them.
- **Order.** One ordered stream per client. A change's `Appended` comes before the
  `CursorState` that points at its nodes, and a `Labels` after the `CursorState` of every
  cursor it names. A `CursorState` or `Labels` that replaces an
  earlier copy removes it and goes to the end, so it never overtakes what it refers to.
- **`watch(cursors)`** chooses which cursors' `Live` a client gets, starting with a
  catch-up taken in the same act: the parts of the sample in flight that are not in the
  tree yet, and the text of the open part. Tool progress has no catch-up; the result
  arrives as `Appended`.
- **Bounds.** `Live` is dropped when the client's buffer is full. `CursorState` and
  `Labels` replace their own earlier copy, so they cost one slot per cursor. A client whose
  `Appended` and `Notice` backlog passes the buffer is cut off (`CutOff`, a `ConnectionError`) and
  reconnects to a snapshot.
- **Gone:** `Saved`, `HookError` as an event (it is in `CursorState`), `driver.questions()`
  and `respond`.

**Calls.** The frontend calls of today's `Driver` (`input`, `send`, `step`, `go_to`,
`set_auto`, `abort`, `open`, `set_label(cursor, label)`, `remove_label`), with the client filled
in, plus `watch`, `answer`, `commands()` (each command's `Form`: name, description and schema;
fixed for the driver's life), `run` (returns the command's result), `options(cursor,
extension)`, `configure(cursor, extension, values)` and `complete(cursor, type, prefix)`. They
are implemented in a private `Calls`; `Driver` makes a client (`driver.client(id)`) and keeps
only `start`, `stop`, `rest`, `use_catalog`, `state` and `add_item`. `go_to` and `abort` return
nothing.

**Completion** has two sources and no callbacks: the values in the schema (`Literal`), and
`complete` for the named types, answered by the driver from the session and the config
(paths on the driver's machine, which matters when the client is remote). No extension code
runs per keystroke, so completion cannot hang or raise. `tau.commandline` holds the
pure parts every line-based frontend shares: `parse(line, commands, options)` (shell words;
positional values fill parameters in order, `name=value` any; the last parameter, when a plain
`str` or `str | None`, takes the rest of the line as typed; a list takes comma-separated values,
or a JSON array when the text starts with `[`) answering the command, its arguments and the required ones missing,
`completions(line, commands, client, cursor, options)`, and `value(text, schema)` /
`values(schema, prefix, client, cursor)`, which read and complete one value of any schema, so a
question's answer typed as text is handled as a command's argument is. The driver's check is the authority; the frontend's
only spares a round trip.

The `Harness` drives a `Client`, so the behavior suite tests the contract once for both.

### 6. The simple cases, as they must read

Each is a behavior test as written here; growing one is a design bug.

```python
# A function that is a tool and a command.
def word_count(path: Path) -> str:
    """Count the words in a file."""
    return f"{len(path.read_text().split())} words"


name = "words"  # this module is the extension
tools = commands = [word_count]  # the model calls it; a person types /word_count <Tab>
```

```python
# Permissions: a typed question from a tool hook.
class Permissions:
    name = "permissions"

    def __init__(self) -> None:
        self.always: set[str] = set()

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        if call.tool in self.always:
            return None
        verdict = await node.ask(f"{call.tool} {call.args_raw}", Literal["allow", "deny", "always"])
        if verdict == "always":
            self.always.add(call.tool)
        return None if verdict in ("allow", "always") else "the user said no"
```

```python
# A command that moves the cursor; the type gives the form, the completion and the check.
async def branch_from(cursor: Cursor, node: NodeId) -> None:
    """Go back to a node of this cursor's walk."""
    cursor.move(node)


# Handing work to another cursor, or a new one: its model is given the note.
def hand_off(tree: TreeHandle, to: CursorId, note: str) -> str:
    """Send a note to another cursor."""
    tree.send(to, note)
    return f"sent to cursor {to}"
```

```python
# Options.
@dataclass(frozen=True)
class Options:
    limit: Annotated[int, "unattended steps before auto-step turns off"] = 50


class StepLimit:
    name = "step_limit"
    options = Options  # read with cursor.options().limit; /config step_limit
```

### 7. What is not a provider

A provider is the API a step samples through. A fast "decider" model, an embedding
model, a reranker or a vector index is something an extension uses inside a hook: it holds
its own client, configured through its options (an endpoint, an `api_key_env`, a model
name), and nothing about it is recorded unless the extension writes it. The driver offers
nothing for them; a second extension that needs the same one imports it.

## Open

- **A command that should not sample.** A command that leaves the cursor on a `User` node
  gets a sample (§2). If `/compact` finds that surprising in practice, the rule to add is
  "a command's step does not sample unless the command turns auto-step on"; nothing is
  added before a real command shows the need.

Settled while drafting (from review): edited options land as a `Config` node (§4);
`CursorState` carries pending items whole, revisited only if measured; an extension may
still treat plain `/…` input in a `pre_input` hook, and the built-in frontends warn and send
an unknown `/name` as text. Trust's hash scope once `.tau/` holds hooks is not a client
question; it moves to Phase 5's trust work.

## Settled in stage 3

Where the text above was silent, as built:

- **A `Config` node holds what it changes** (`{"model": …}` and/or `{"extensions": {…}}`), and
  the driver folds the walk, because core's `config_at` is the nearest `Config` only: the model
  is the last named, an option the last set, per field. No way to unset an option.
- **`Client.options(cursor, extension)`** takes the cursor (a branch may override) and returns a `Form`
  whose schema defaults are the values now (see fix pass B for `/config`).
- **`Client.commands()` is a call** (async), so a remote client can ask it; **`complete`'s second
  argument** is the name of a named type (`"path"`, `"model"`, …), the schema's `"format"`.
- **A failed `Cursor` command ends its step `Failed`**, like a failing hook (`hook_error`
  names `pre_step` and its extension), and tells the client `"Type: msg"`; a plain command only
  tells. An aborted command answers its job without running the hooks.
- **Options may not be named types that are looked up in the session** (`CursorId`, `LabelName`,
  `NodeId`): stored options are read without one. A `Model` is allowed: it needs only the catalog.
- **`tau config check`** knows the built-in extensions only (`StepLimit`); it passes them to
  `check_config(..., extensions=)`, which a program that loads others does for them.
- **`StepLimit()` takes its limit from its options** (default 50), so `StepLimit(n)` is gone.

## Settled in review (fix pass A)

- **A handle knew its client** (`handle.client`, or `None`); fix pass B made it stateless and
  Phase 5 stage A removed it from every handle (below). `TreeHandle.send(to, text)` is
  `Client.send` for a handle: an input from the client who typed, with auto-step on (raises if
  none); stage B makes it authored by the extension instead.
- **A sync function's writes reach the loop**: the driver routes what a handle tells it back to the
  loop's thread, so a tool or a command may write from its worker thread.
- **Setting the model or options checks before it writes**: the model is in the catalog, and the options
  folded with the walk's must build (a `__post_init__` that raises refuses). A required option must
  therefore be set by the call that first makes the options whole.
- **A `T | None` with no default may be left out** and is `None`; a `CursorId` default of `"new"` is
  refused when the function is described.
- **Catch-up replays a call under the provider's own index**; a call part that was never named in
  the stream was never drawn, so it is not replayed.
- **Every client call raises first** once the driver has failed or stopped.
- **A question to anyone declines at once when no client is connected.** Reversed in Phase 5
  stage A (§3): the question waits.
- **`handle.config()` is gone**: the nearest `Config` node is only what that node changes; options
  and the model are read folded (`options()`, the sample's own resolution).

## Settled in Phase 5 (stage A)

- **Cursors are oblivious to clients.** `Cursor` and `NodeHandle` have no `client`: a cursor runs
  the same whether anyone is watching or not, and client matching exists to route a question.
  `cursor.tell(text, to=None)` sends the `Notice` to every connected client; `to=` names one.
  Only a command's `TreeHandle` has a `client`, its runner. `TreeHandle.send` still writes as the
  nearest client who typed (internal, until stage B).
- **A tool knows its cursor**: `NodeHandle.cursor_id` is the cursor whose batch it runs in.
  (`TreeHandle.cursor(id)` already reads a cursor's state, so the attribute is `cursor_id`.)

## Settled in review (fix pass B: interface reshape)

- **A handle's client is read, not remembered.** (Superseded by stage A: no handle has a
  `client`: a cursor is oblivious to its clients, and a command that takes a `TreeHandle` and a
  `Cursor` is given one object for both, so `tree.client` would have been `None` there.) It was a command's runner while it ran; otherwise the client of the
  nearest `User` node authored by a frontend on the walk, else `None`.
- **`/config` has no JSON.** `Client.options(cursor, extension) -> Form` and `Client.configure(cursor,
  extension, values)`; `commandline.parse` reads `/config step_limit limit=10` by the options form,
  and a bare `/config step_limit` is a form to show. One frozen `Form(name, description, schema)`
  serves commands and options; `"default"` is in the schema (an options form's are the values now),
  never in the schema a model is shown.
- **`Client.run` returns the result and raises the failure** (`CommandFailed`, `"Type: msg"`; an
  aborted command raises it too). `Notice`s remain for `progress` and `cursor.tell`.
- **One stream-ending rule:** `CutOff` and a driver failure are `ConnectionError`s (the second's
  message the failure's `"Type: msg"`, chained); a stopped driver ends the stream cleanly.
- **`Connected` is gone.** A node never changes and is keyed by id, so the snapshot (a `CursorState`
  per cursor, `Labels`) is well defined without a node count: a frontend reads `walk(at)` and
  ignores an `Appended` node it already has.
- **Events and forms are not hashable** where they hold a schema (a dict); making schemas read-only
  mappings would not make them hashable, so the expectation is dropped, not met.
- **`cursor.set_model(model)` / `cursor.set_options(**changes)`** replace `configure`; the second
  writes the calling extension's own options only, `/config` writing across extensions privately.
- **No `tool()`:** the driver describes plain functions in `tools=` as extensions' are. One
  `DefinitionError` replaces the hint, tool, tool-contract and extension-contract errors; options
  errors are `ConfigError`. Injection is by annotation alone.
- **Private modules:** `_values`, `_functions`, `_tools`; the exports are `Model`, `CursorId`,
  `LabelName`, `NodeId`, `Form`, `DefinitionError`, `CommandFailed` and the rest of the extension
  author's and frontend's surface.
- **`Driver` keeps `start`, `stop`, `rest`, `use_catalog`, `state`, `client` and `add_item`**; the
  client calls live in a private `Calls`.

## Alternatives

| option | why not |
|---|---|
| A `@command` decorator beside `@tool` | Two descriptions of one function; a function useful to both needs a wrapper. Lists on the extension say where it is offered. |
| A command's handle passed as a first positional parameter | Position is a convention to remember; the annotation already says what is wanted, and lets a function that needs no handle be a tool. |
| JSON Schema dicts written by hand for questions and options | A second type language beside the hints; every extension author re-learns it. |
| Open-ended `format` strings an extension registers | Completion would need extension callbacks (τ 0.15's enumerators): extension code per keystroke can hang, raise, or block a remote client. |
| Fine-grained state events (`question_opened`, `status`, `cursor_moved`, …) | Every kind needs its own replay on connect and merge rule; whole state is one rule, and it is small. |
| Subscribe per cursor | A client would not see another cursor's question; switching would need a request and would race. |
| Questions polled (Phase 4) | A frontend cannot know when to poll; over a network it costs round trips forever. |
| Commands as `pre_input` hooks that parse text | The client has already parsed and checked the arguments; completion needs declarations anyway. |
| A command as an item, run when the cursor wakes | Survives a crash and runs on reopen, long after the person moved on; the step it needs is one `Session.step` away. |
| Commands queue while the cursor is busy | A queued command strands when the step ends other than `Done` and runs on whatever wakes the cursor next. Refusing is one rule. |
| Decider and embedding models as providers | They are not sampled into the tree; making them providers drags the walk, `Finish` and replay into APIs that have none. |
| A client is a connection | Every network blip would decline that client's questions (a permission question becomes a deny). |
| A frontend reads `Driver` and `Session` directly when local | Two contracts, and the remote one rots; τ 0.15's `RemoteBackend` mirrored every call by hand. |

## Consequences

- Code (Phase 5, in this order):
  1. `tau._values` and functions as tools and commands (§1, §2's descriptions and
     injection, `describe=` replaced by `Annotated`); `ask(text, expect)` (§3) on both
     handles;
  2. (built) `Client`, `LocalClient`, the §5 events and snapshot, questions with ids and targets;
     the `Harness` and the tests move to them, and `Saved`, `HookError`, `questions()` and
     `respond` go;
  3. (built) `run`, `complete`, `commandline`, options, `set_model`, `set_options`, `/model`, `/config`; the
     §6 cases as tests; `examples/chat.py`, a line client to try it by hand;
  4. `tau serve` and `RemoteClient` stay Phase 6, but the suite then runs against both.
- Glossary: client, `LocalClient`, `RemoteClient`, cursor state, `Appended`, `Labels`,
  watch, question (amended: id, `to`, `expect`), form, command, named type, options
  (extension), `set_model`, `set_options`, `values`, `commandline`. Removed: `Saved`, `HookError`,
  `describe`.
- `model.md` §7 (questions) and §9 (events) are rewritten to this; §7's "commands and gates
  are `pre_input` hooks" becomes "gates are"; D15's frontend rules 1–5 are replaced by §5.
- Phase 5's permissions extension is the first typed question; compaction and prune are the
  first commands that generate.
