# Writing an extension

An extension adds to a session without changing τ. It is a Python object with a `name` and any
of these:

- **tools** the model can call,
- **commands** a person types as `/name`,
- **options**, settings kept in the config,
- **hooks**, functions τ calls at fixed points of a step.

It can also show a status on a cursor and ask whoever is watching a question. If you have not
read [the model in ten minutes](model.md), read it first: a *cursor*, a *step* and a *hook point*
mean something exact.

## A complete extension

This one gives the model a `shout` tool, gives you a `/count` command, has one option, and asks
for permission before any tool runs. It is a module with an object named `extensions`.

```python file=shouter.py
"""shouter: a tool, a command, an option, a hook and a status."""

from dataclasses import dataclass
from typing import Annotated, Literal

from tau import Body, CallDraft, Cursor, NodeHandle, ToolError


@dataclass(frozen=True)
class Options:
    max_chars: Annotated[int, "longest text the shout tool takes"] = 40


class Shouter:
    name = "shouter"
    options = Options

    def __init__(self) -> None:
        self.tools = (self.shout,)
        self.commands = (self.count,)

    def shout(self, text: Annotated[str, "what to say"], node: NodeHandle) -> str:
        """Return the text in capitals."""
        longest = node.options().max_chars
        if len(text) > longest:
            raise ToolError(f"say at most {longest} characters")
        return text.upper()

    async def count(self, cursor: Cursor) -> str:
        """Say how many nodes are on this branch."""
        mine = await cursor.ask("Count only the messages you sent?", bool)
        nodes = cursor.walk()
        if mine:
            nodes = [n for n in nodes if isinstance(n.body, Body.User)]
        cursor.show(f"{len(nodes)} counted")
        return f"{len(nodes)} nodes"

    async def pre_tool(self, node: NodeHandle, call: CallDraft) -> str | None:
        verdict = await node.ask(f"let the model call {call.tool}?", Literal["yes", "no"])
        return None if verdict == "yes" else "the user said no"


extensions = [Shouter()]
```

The rest of this page explains each part, then how to test the extension and how τ finds it.

## Tools

A tool is a plain function in the extension's `tools`. τ builds everything the model is told from
the function:

- the **name** is the function's name, and the **description** is its docstring. A function with
  no docstring is refused when the extension loads (`DefinitionError`).
- the **parameters** are a JSON Schema built from the type hints. Every parameter needs one.
  `Annotated[T, "text"]` adds a description the model reads. A parameter with a default is
  optional.
- the function **returns a `str`**, which becomes the call's result.

The hints may be `str`, `int`, `float`, `bool`, `Literal` of strings or integers, `list[T]`,
`T | None`, `pathlib.Path`, and the named types `Model`, `CursorId`, `LabelName` and `NodeId`
(a value that must be a configured model, an open cursor, an existing label or an existing node).
Anything else is refused when the extension loads, with a message naming the function and
parameter.

A function that fails tells the model why. Raise `ToolError("message")` and the model reads
exactly that text. Any other exception is reported as `Type: message`. A bad argument from the
model (a missing one, the wrong type) is reported the same way, without your function running.

A **sync** function runs in a worker thread, so it never blocks the session; an `async` one runs
on the loop. A few parameters are filled in by τ, found by their annotation, and hidden from the
model:

| annotation | what you get |
|---|---|
| `NodeHandle` | a handle on this call's **batch**, the tool calls one reply asked for (as `shout` uses it) |
| `TreeHandle` | a handle on the tree, with no cursor of its own |
| `Progress` | a function, `progress("text")`, that shows partial output while the tool runs |

A tool that takes a handle (`NodeHandle` or `TreeHandle`) must be listed on an extension, since
the handle is bound to it; any other tool can also be given to a driver on its own. A `Cursor` is
refused: a tool does not run on a cursor.

Tool names are not namespaced, since the model and the log use them as they are. Two tools
with one name are an error.

## Commands

A command is a function in `commands`, offered to a person as `/name`. The same function may also
be a tool. Its docstring is the help text and its parameters become the command's arguments,
read from what the person types (`/count`, `/ralph PROMPT.md 4`) and checked by their hints.

A command that takes a `Cursor` runs *as a step of that cursor*: it gets the cursor's handle and
runs before the `pre_step` hooks. It is refused with `Busy` unless the cursor is at rest, and
aborting the cursor stops it. A command without a `Cursor` runs at once, whatever the cursor is
doing. Return a string to show a result; raise to fail (the caller gets `CommandFailed`). To refuse (bad arguments, say) raise `CommandFailed(message)` yourself: the caller gets your message as written, with no traceback, and a `Cursor` command's step does not fail.

An extension whose commands come from files (the built-in `skills`) lists a method `rescan(self)
-> bool`: τ calls it before it lists or runs any command, and it returns whether `commands`
changed (then it has made them again). Keep it cheap when nothing changed: compare what a `stat`
says, never read every file each time.

Two extensions may each have a `/note`. The bare `/note` then names neither, and every command
also has a **qualified name**, `/shouter:count`, that nothing can take. The name `config` is
τ's own.

## Options

`options` names a *frozen dataclass*. Each field is a hint from the same list as a tool's (but not
`CursorId`, `LabelName` or `NodeId`, which need a session), with a plain default (a field with
none must be configured). Read the values with `cursor.options()`,
`node.options()`; they give an instance of your class, and the class's `__post_init__` may refuse
a bad value.

The values come from, in order, `extensions.<name>` in the config files and the `Config` nodes on
the cursor's branch, so an option can be set in these ways:

```json
{ "extensions": { "shouter": { "max_chars": 80 } } }
```

- in `config.json` as above, or with `tau config set shouter.max_chars=80`, which writes that line
  into your user config; a trusted project's `.tau/config.json` can set it too;
- `/config shouter max_chars=80` in a session, for the current branch only;
- the same with `--save`, for the branch and for your config file.

`tau config check` checks the values against the type for every extension it can load.

## Hooks

A hook is an `async` method named for its point. Any subset is fine. A cursor hook is called as
`hook(cursor, subject)` and a tool hook as `hook(node, call)`. The points, in the order a step
reaches them:

| point | when | the hook is given |
|---|---|---|
| `pre_step` | the step begins | nothing |
| `pre_input` | once for each pending input, before it becomes a `User` node | `draft`: the node about to be written |
| `pre_sample` | before the model is asked | `draft` (setting it answers the sample yourself, with no model call) and `tools`, the names of the tools to offer (`None`: all) |
| `pre_reply` | the reply came back, before it is written | `draft`: the `Assistant` node about to be written |
| `post_reply` | the reply is in the tree (not run if `pre_reply` left no draft: nothing was written) | `node`: that node |
| `pre_tool` | once for each tool call, before it runs | `call`: a `CallDraft` |
| `pre_result` | once for each tool call, after it ran | `call`, with its `outcome` |
| `post_result` | the tool results are in the tree | `node`: the `Results` node |
| `post_step` | the step ends | `ending`: `Done`, `Halted`, `Failed` or `Aborted` |

What a hook returns:

- `None` carries on to the next hook and the next phase.
- A string **stops**: the later hooks of that point do not run, and the step halts with that
  reason. For a tool hook the string becomes the call's error, so the model reads it: in
  `shouter`, `"the user said no"`.
- An exception fails the step (a tool hook: that one call). The cursor's `CursorView` carries the
  error, the traceback goes to standard error, and the driver goes on.

Hooks run in the order the extensions load. A cursor hook acts on its own cursor, through the
`cursor` it is given. It may read the tree, `write` a node under the cursor's `at`, `move` the
cursor, change `subject.draft` (assign a new `Draft`, or `None` to write nothing), and turn
auto-step on or off with `set_auto`. Core checks only that the tree stays valid; it does not
limit what a hook changes. Do not keep a handle after the hook returns: it stops working.

A tool hook may rewrite `call.args_raw`, and may set `call.outcome` to an `Ok` or `Err` outcome:
in `pre_tool` that answers the call and the tool does not run; in `pre_result` it replaces the
result.

Cursors talk to each other through the scratchpad. `cursor.send(to, text)` adds an input for
another cursor, written by your extension; `cursor.add_item` leaves a note of your own for it.

## Showing a status

`cursor.show("3 of 10")` puts a line of text next to the cursor for your extension, and
`cursor.show(None)` takes it away. Every client sees it in the cursor's `CursorView` (`shown`
holds `(extension, text)` pairs), and a frontend draws it somewhere. It is not written to the
log, so a reloaded session has none. `/count` above shows how many nodes it counted.

## Asking a question

`await cursor.ask(text, expect=str)` stops your hook or command until someone answers, then
returns the answer checked and converted by `expect`. It is `None` if the question was declined,
so check for it. `expect` is any hint from the list above, and the answer's schema reaches the
frontend, which can offer the choices: `Literal["yes", "no"]` is a menu, `bool` a yes or no,
`Path` a filename that completes with Tab. `node.ask` does the same in a tool hook, on the
cursor whose tool call it is.

Every client is shown the question, and the first good answer wins. `to="john@laptop:tui"`
reserves it for that client while it is connected; if it is not, or disconnects, the question goes
to the **pool**, the questions anyone may answer. While it waits, the cursor is busy. Aborting the cursor, or going to another node, declines
every open question, so a hook must expect `None`. With nobody connected, the question waits.
[Writing a frontend](frontends.md) shows how a client answers.

## Counting tokens

`await cursor.count()` answers what the walk to the cursor's node will cost as a request: a
`RequestCount` whose `total` is a `Count` (`tokens`, `provenance`, `bound`, and `upper`, their
sum) and whose `nodes` hold a count for each node, so an extension can see how big a part of the
conversation is without guessing from its characters. `count(at=node_id)` counts another node's
walk, and a tool hook's `node.count()` the batch's. Every count is what the servers reported
where a report covers it (`provenance` is `reported`), and for what none does, such as your new
message, the node's bytes as a ceiling (`ceiling`; `upper` is the most it can be). Counts add, and
a sum is as weak as its weakest part.

An extension can do better for the nodes no report covers by offering a **counter**: an attribute
`counter` holding an object with `count(job)`, which returns a `CounterAnswer` with a count for
some of `job.unreported`. It runs in a thread, so it must not touch the network. It cannot change
a report: an answer for a node a report covers fails the count with a `DefinitionError` naming the
extension. Only one loaded extension may offer a counter, and `cursor.counter` is its key (`None`
if there is none). [The built-ins guide](built-ins.md#a-long-conversation-can-be-shortened) shows
what compaction does with the counts.

## Testing it

Drive the extension with a scripted model: a *provider* that plays back replies. Providers are
the model APIs; one is an object with `name`, `check`, `aclose` and an async generator `sample`.
This one yields the parts you give it. With it, you can run the extension through real steps, tool
calls and questions, with no network.

This program runs `shouter` together with a second extension, `tidy`, that edits what is
written and what the model is offered.

```python file=tidy.py
from tau import Body, Cursor, Draft, Subject


class Tidy:
    name = "tidy"

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        draft = subject.draft
        assert draft is not None and isinstance(draft.body, Body.User)
        subject.draft = Draft(Body.User(draft.body.text.strip()), draft.delivers, draft.by)
        return None

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> str | None:
        subject.tools = ["shout"]
        return None


extensions = [Tidy()]
```

```python
import asyncio
import json

import shouter
import tidy

from tau import (
    Body,
    CursorView,
    Driver,
    Finish,
    Outcome,
    Part,
    Session,
    Stop,
    SystemClock,
    model_config,
    read_models,
)


class Scripted:
    """A provider that plays back replies: each is the parts to yield, and the stop."""

    name = "scripted"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.offered = []  # the tool names each sample was offered

    def check(self, model):
        return []  # no problems with the model's settings

    async def aclose(self):
        """Nothing is held open."""

    async def sample(self, walk, model, tools):
        self.offered.append([t.name for t in tools])
        parts, stop = self.replies.pop(0)
        for part in parts:
            yield part
        yield Finish(stop)


async def main():
    provider = Scripted(
        ([Part.Call("c1", "shout", json.dumps({"text": "hello"}))], Stop.Tools),
        ([Part.Text("done")], Stop.End),
    )
    models = {"scripted": {"provider": "scripted", "models": {"m": {"model": "m"}}}}
    session = Session.create(model_config("scripted:m"), "Be brief.", SystemClock().now())
    driver = Driver(
        session,
        {"scripted": provider},
        read_models(cli={"models": models}),
        extensions=[*shouter.extensions, *tidy.extensions],
    )
    driver.start()
    client = driver.client("test")
    cursor = session.cursors()[0]

    async def answer_questions():
        """The frontend's part: say yes to everything asked."""
        async for event in client:
            if isinstance(event, CursorView):
                for question in event.questions:
                    yes = True if question.schema["type"] == "boolean" else "yes"
                    await client.answer(question.id, yes)

    answering = asyncio.create_task(answer_questions())

    await client.send(cursor, "  shout hello  ")
    await driver.rest(cursor)
    walk = session.tree.walk(session.cursor(cursor).at)
    assert walk[2].body == Body.User("shout hello")  # tidy stripped the input
    assert provider.offered == [["shout"], ["shout"]]  # and offered only shout
    results = walk[4].body
    assert isinstance(results, Body.Results)
    assert results.outcomes[0][1] == Outcome.Ok("HELLO")  # shouter ran the tool

    assert await client.run(cursor, "count", {}) == "1 nodes"  # /count, asked about, answered yes

    await driver.stop()
    await answering
    session.close()
    print("shouter works")


asyncio.run(main())
```

For your own tests you will usually want the scripted provider to fail too: yield a
`Part.Text`, then raise `SampleError("overloaded")` to see how your hooks behave on a failed
sample.

## How τ finds an extension

Every way of loading uses the same rule for what a file provides: **a module with a `name` (a
string) is itself the extension; otherwise it must define `extensions = [...]`**, a non-empty
list of extension objects, added in order. A name is ASCII letters, digits, `_` and `-`: a `:` would
break `/shouter:count`, and a `.` would break `tau config set shouter.max_chars=80`.

The places an extension can come from, in the order they load (the order hooks run in). A
**drop-in** is a `.py` file or a package directory in an `extensions/` directory; names starting
with `.` or `_` are ignored.

| source | where | key | on unless |
|---|---|---|---|
| built-in | inside τ (`agents`, `attachments`, `base_tools`, `compaction`, `permissions`, `prune`, `skills`, `step_limit` and `usage`, and the key `retry` for the provider wrapper) | its name | `load` says off |
| package | the `tau.extensions` plugin group of an installed package | the registration's name | `load` says off, or `--no-extensions` |
| user | `$TAU_HOME/extensions/NAME.py` or `NAME/` | `NAME` | `load` says off, or `--no-extensions` |
| project | `.tau/extensions/NAME.py` or `NAME/` in a trusted project | `NAME` | the project is untrusted, or `load` says off, or `--no-extensions` |
| `-e` | a path or a key on the command line | the file's name without `.py` | never: naming it is the consent |

The `tau` extension (`/model`) always loads first, and `session_name` (`/name`) always loads.

Try the file above. `-e` names it once, for this run:

```bash
tau ext list -e shouter.py
```

`tau ext list --json` prints one JSON object per source, one per line (JSON lines), so `grep` and
`jq` work on it line by line; it is not one JSON array.

To keep it, put it in your user drop-ins:

```bash
mkdir -p "$TAU_HOME/extensions"
cp shouter.py "$TAU_HOME/extensions/"
tau ext list
tau config set shouter.max_chars=80
tau config check
```

A project can carry its own extensions in `.tau/extensions/`. They are code that runs with your
authority, so they load only after you approve the project with `tau trust`. Approval pins the
exact bytes, and a changed file is untrusted again. `tau ext list` says `untrusted` for them
until then. [Configuration](configuration.md#projects-and-trust) has the details.

An installed package can offer extensions through the `tau.extensions` plugin group in its
`pyproject.toml`. The value is a *module* that follows the same rule, not `module:attribute`:

```toml
[project.entry-points."tau.extensions"]  # rules: allow(entry): the name of the Python packaging table
shouter = "my_package.shouter"
```

When the same key appears in two sources, the later source in the table **shadows** the earlier:
a project's `shouter.py` replaces the user's `shouter.py`, and `-e ./search.py` replaces the
installed `search` package. A shadowed source is never imported, and `tau ext list` says what
shadowed it. Two sources of one row with one key, two extensions with one name, and two tools with
one name are errors.

To turn a source off, or back on, without deleting it:

```bash
tau ext disable shouter
tau ext list
tau ext enable shouter
```

This writes `"load": { "shouter": false }` into your user config. `--no-extensions` turns off the
package, user and project rows for one run, and `-e` still loads what you name.

The tools the model starts with, `read`, `write`, `edit`, `grep`, `find` and `bash`, come from a
built-in extension, `base_tools`. `tau ext disable base_tools` (or `"load": { "base_tools": false }`)
starts sessions without them; `--no-extensions` does not, it skips only packages and drop-ins.
`tau ext list` shows what is left. Then the model has only the tools your extensions list. To have fewer of them, turn `base_tools` off and
list the ones you want (`from tau import ReadTool`, in your extension's `tools`), or put a
drop-in named `base_tools` where it shadows the built-in. A program that builds a `Driver` itself
gives it `tools=base_tools(cwd)`.

**A source that fails to load stops τ from starting**, and the message names the source, the
error, and the way past it (`tau ext disable KEY`, or `--no-extensions`). A guard extension that
silently failed to import would leave the session unguarded, so τ does not carry on without it.
Every failing source is reported at once.

## Where to look next

- [`examples/speculative/`](../../examples/speculative/): seven larger extensions (plan mode, a
  loop, a router, a bash guard...), each with a note on what it needed from τ.
- [The reference for extensions](../reference/extensions.md): every handle method and its
  signature, from the docstrings.
- [`docs/design/glossary.md`](../design/glossary.md), under "Hooks": the exact rules.
