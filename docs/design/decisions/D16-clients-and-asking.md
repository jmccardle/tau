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
written as Python type hints, and one module (`tau_next.values`) turns a hint into a
schema, checks a JSON value against it, and converts it to what the function receives.
Nothing else describes a value.

| hint | JSON | the function gets | a frontend shows |
|---|---|---|---|
| `str`, `int`, `float`, `bool` | as JSON | as JSON | a field, a checkbox |
| `Literal["a", "b"]` (str or int) | the value | the value | a dropdown |
| `list[T]` | array | `list` | a multi-select of `T` |
| `T \| None`, or a default | may be absent or `null` | `None` or the default | an optional field, prefilled |
| `Annotated[T, "text"]` | as `T` | as `T` | `T`, with the text as help |
| `pathlib.Path` | string | `Path` | a file picker, completed on the driver's machine |
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
each function when it is built (`ToolDefinitionError` names the problem), and `tool(fn)`
remains for a tool given to the driver outside any extension. A module with a `name` is an
extension.

Parameters the driver fills, by annotation, are not arguments:

| annotation | as a tool | as a command |
|---|---|---|
| `Progress` (`progress`) | the call's `ToolProgress` | a `Notice` to the client |
| `TreeHandle` | the batch's `NodeHandle` | the cursor's handle, as a `TreeHandle` |
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

**Built-in commands** are commands of the built-in extension `tau`: `/model` and
`/config`. Command names are unique across extensions; a duplicate fails when the driver
is built.

### 3. Questions

`await handle.ask(text, expect=str, *, to=None) -> value | None`, on `Cursor` and on
`NodeHandle` (a tool hook asks on its batch's cursor).

- `expect` is any hint of §1: `Literal["allow", "deny", "always"]`, `Model`,
  `list[pathlib.Path]`. The value comes back checked and converted; `None` is a decline.
- A question has an **id**, its cursor, `to` (`None`: anyone; or a client id), its text and
  its schema. It is in its cursor's `CursorState` until it closes.
- `answer(question_id, value)` answers it. The driver checks the value; a wrong one is
  refused to the answering client (`ValueError` naming the problem) and the question stays
  open. The first good answer wins; a later one raises `LookupError`.
- Abort and go-to decline the cursor's questions, as now. A question `to` one client stays
  open across that client's disconnects; asking a client that is not connected declines at
  once, so a cursor never waits on someone who is not there.

### 4. Options and config

An extension's **options** are a frozen dataclass named by its `options` attribute; each
field is a hint of §1 with a default (a field with none must be configured). They live in
the D13 layers under `extensions.<name>`, and a `Config` node may override them for a
branch. `handle.options()` returns the dataclass as it stands at the handle's node.
`tau config check` checks every extension's options.

`cursor.configure(**fields)` writes a `Config` node holding just `fields` under `at` and
moves onto it: the branch's model (`configure(model="opus")`), or an extension's options
(`configure(extensions={"step_limit": {"limit": 10}})`). `/model <Model>` and
`/config <extension>` are built-in commands that do exactly that; the second's form is the
extension's options. The user's file stays `tau config`'s to edit.

### 5. A frontend holds a `Client`, local or remote

A frontend never touches `Driver` or `Session`. It holds a `Client`: async calls, reads,
and one event stream. Two implementations, one protocol:

- `LocalClient(driver, client_id)`: in-process; the transport is free.
- `RemoteClient(socket)` (Phase 6): `tau serve` runs a `LocalClient` per connection and
  serializes calls and events generically (every argument and event is a plain frozen
  value), so there is no per-call wire code.

A network adds identity (a client names itself with a stable id such as
`john@laptop:tui`, so a reconnect is the same client and its actions are
`by: Frontend(client)`; a connection is not a client), loss (the stream ends with an error;
reconnecting is a fresh snapshot; there is no resume protocol, because the log is the
truth) and transport (a Unix socket with file permissions first). Nothing else.

**Events.** Small state is sent whole whenever it changes, never as a delta; every client
receives it for every cursor, so a frontend can show "cursor 3 is waiting on you" while
it watches cursor 1, and switching the viewed cursor is a local lookup.

| event | to | carries |
|---|---|---|
| `CursorState(cursor, …)` | all clients | `at`, busy, auto-step, open questions, pending items, the last step's ending including a hook's error |
| `Labels(labels)` | all clients | every label and its cursor |
| `Appended(nodes)` | all clients | the new nodes, bodies included |
| `Live(cursor, delta)` | clients watching the cursor | text, thinking, call and tool-progress deltas, as in D15 |
| `Notice(text)` | one client | `cursor.tell`, a refused input, a command's result and progress |

- **On connect** a client receives `Labels`, one `CursorState` per cursor, and the tree's
  node count, before anything else; nodes below the count it reads with calls (`node`,
  `walk`, `children`), nodes from it on arrive as `Appended`. Subscribing and the snapshot
  are one act on the driver's loop, so nothing falls between them.
- **Order.** One ordered stream per client. A change's `Appended` comes before the
  `CursorState` that points at its nodes. A `CursorState` or `Labels` that replaces an
  earlier copy removes it and goes to the end, so it never overtakes what it refers to.
- **`watch(cursors)`** chooses which cursors' `Live` a client gets, starting with a
  catch-up taken in the same act: the parts of the sample in flight that are not in the
  tree yet, and the text of the open part. Tool progress has no catch-up; the result
  arrives as `Appended`.
- **Bounds.** `Live` is dropped when the client's buffer is full. `CursorState` and
  `Labels` replace their own earlier copy, so they cost one slot per cursor. A client whose
  `Appended` and `Notice` backlog passes the buffer is cut off and reconnects to a snapshot.
- **Gone:** `Saved`, `HookError` as an event (it is in `CursorState`), `driver.questions()`
  and `respond`.

**Calls.** The frontend calls of today's `Driver` (`input`, `send`, `step`, `go_to`,
`set_auto`, `abort`, `open`, `set_label`, `remove_label`), with the client filled in, plus
`watch`, `answer`, `commands()` (each command's name, description and schema; fixed for the
driver's life), `run`, `options(extension)` and `complete(cursor, type, prefix)`.

**Completion** has two sources and no callbacks: the values in the schema (`Literal`), and
`complete` for the named types, answered by the driver from the session and the config
(paths on the driver's machine, which matters when the client is remote). No extension code
runs per keystroke, so completion cannot hang or raise. `tau_next.commandline` holds the
pure parts every line-based frontend shares: `parse(line, commands)` (shell words;
positional values fill parameters in order, `name=value` any; a list takes comma-separated
values) answering the command, its arguments and the required ones missing, and
`completions(line, commands, client)`. The driver's check is the authority; the frontend's
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


# Handing work to another cursor, or a new one.
def hand_off(tree: TreeHandle, to: CursorId, note: str) -> str:
    """Send a note to another cursor."""
    tree.add_item(to, note)
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
- **`Client.options(cursor, extension)`** takes the cursor (a branch may override) and returns
  `OptionsForm(extension, schema, value)`; `/config <extension> <values>` takes the changed
  options as a JSON object in a `str`. A frontend wanting a form reads the first and sends the
  second. `/config` exists only if some extension has options.
- **`Client.commands()` is a call** (async), so a remote client can ask it; **`complete`'s second
  argument** is the name of a named type (`"path"`, `"model"`, …), the schema's `"format"`.
- **A failed `Cursor` command ends its step `Failed`**, like a failing hook (`hook_error`
  names `pre_step` and its extension), and tells the client `"Type: msg"`; a plain command only
  tells. An aborted command answers its job without running the hooks.
- **Options may not be named types** (`Model` and the rest are checked against a session that
  stored options do not have): a field that names a model is a `str`.
- **`tau config check`** knows the built-in extensions only (`StepLimit`); it calls
  `check_options(catalog, extensions)`, which a program that loads others calls for them.
- **`StepLimit()` takes its limit from its options** (default 50), so `StepLimit(n)` is gone.

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
  1. `tau_next.values` and functions as tools and commands (§1, §2's descriptions and
     injection, `describe=` replaced by `Annotated`); `ask(text, expect)` (§3) on both
     handles;
  2. (built) `Client`, `LocalClient`, the §5 events and snapshot, questions with ids and targets;
     the `Harness` and the tests move to them, and `Saved`, `HookError`, `questions()` and
     `respond` go;
  3. (built) `run`, `complete`, `commandline`, options, `configure`, `/model`, `/config`; the
     §6 cases as tests; `examples/chat.py`, a line client to try it by hand;
  4. `tau serve` and `RemoteClient` stay Phase 6, but the suite then runs against both.
- Glossary: client, `LocalClient`, `RemoteClient`, cursor state, `Appended`, `Labels`,
  watch, question (amended: id, `to`, `expect`), form, command, named type, options
  (extension), `configure`, `values`, `commandline`. Removed: `Saved`, `HookError`,
  `describe`.
- `model.md` §7 (questions) and §9 (events) are rewritten to this; §7's "commands and gates
  are `pre_input` hooks" becomes "gates are"; D15's frontend rules 1–5 are replaced by §5.
- Phase 5's permissions extension is the first typed question; compaction and prune are the
  first commands that generate.
