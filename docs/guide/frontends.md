# Writing a frontend

A frontend shows a session and takes input: a terminal UI, an editor panel, a script, a bot.
It holds a **`Client`**, one object that does everything a frontend may do. Apart from starting
a session and taking a client from it, a frontend never touches the driver, the session or the
providers. There are two ways to get a client, and the code that uses it is the same for both:

- **local**: your program starts the session itself, and `driver.client(id)` makes a
  `LocalClient`;
- **remote**: another process runs `tau serve`, and `RemoteClient.connect(socket, id)` makes a
  `RemoteClient` over its Unix socket. A client in another language speaks
  [the wire protocol](protocol.md) instead.

[`examples/chat.py`](../../examples/chat.py) is a whole small frontend (a line chat, with
commands, questions and Tab completion) over a local client. The examples here use the mock
model, which echoes what it is sent, so they need no server:

```json file=$TAU_HOME/config.json
{
  "default_model": "mock:echo",
  "models": { "mock": { "provider": "mock", "models": { "echo": { "model": "echo" } } } }
}
```

## Start a session and get a client

`start_session` is the start `tau` itself uses. It reads the same flags (`-c`, `--session`, `-e`,
`--append-system-prompt`...), loads the config, the extensions and the system prompt, creates or
opens the session, and starts the driver. Add the flags to your own parser with the two parent
parsers `session_flags()` and `extension_flags()`, and pass what it parsed.

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import first_cursor, start_session
from tau.cli import extension_flags, session_flags


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    args = parser.parse_args(["--no-session"])  # a real program passes sys.argv[1:]
    started = await start_session(args, environ=os.environ, cwd=Path.cwd())
    client = started.driver.client("me@here:guide")
    cursor = await first_cursor(started.session, client)
    print("client", client.id, "is on cursor", cursor, "of session in", started.cwd)

    await client.close()
    await started.stop()


asyncio.run(main())
```

What to know:

- **The id** of a client (`john@laptop:tui`) says who is acting. Nodes you write are authored
  `Frontend(id)`. Keep it stable across reconnects.
- **`start_session` changes the process's working directory** to the session's, so the base
  tools, path completion and relative paths all agree with it.
- **`first_cursor`** is the cursor to show. Every cursor of a session may have been closed, and
  this reopens the first one if so: a new cursor at its last node, with its labels.
- **It can refuse**, and a frontend should print the message and stop: `ConfigError`,
  `TrustError` (an untrusted project: run `tau trust`), `ExtensionLoadError`, `UnknownSession`,
  `Leased` (another process holds that session; `tau serve` is the way to share one). Pass
  `prompts=tau.prompts(stdin, stdout)` and a terminal gets asked to trust a project or pick among
  several models, instead of failing.
- **`started.stop()`** stops the driver and releases the session. A session that was never used is
  deleted. The client is closed first, with `await client.close()`.
- A program with its own extensions passes `own=[MyExtension()]`. A program with its own system
  prompt passes `system=`, a function that is given the working directory and the loaded config
  and returns the text; the prompt flags are then an error, since they would be ignored.

## Events

A client is also an event stream: `async for event in client`. The first events are a snapshot (a
`CursorView` for every open cursor, a `CursorGone` for every closed one, then `Labels`), and then
changes as they happen. There are six kinds:

| event | meaning |
|---|---|
| `CursorView` | one cursor, whole: `at`, `busy`, `auto`, the open `questions`, pending `items`, how the last step `ending` ended and the `hook_error` if a hook failed, the `model`, and what extensions `shown` |
| `Appended` | nodes added to the tree, bodies included, in id order |
| `Live` | a reply arriving: `delta` is a `TextDelta`, `ThinkingDelta`, `CallNamed`, `CallArgs` (a tool call being written) or `ToolProgress`. Only for cursors you `watch`. |
| `Labels` | every label and its cursor, whenever one changes |
| `CursorGone` | a cursor was closed; drop it. `at` is where it ended |
| `Notice` | a message for this client: `cursor.tell` (which goes to every client unless it names one), or an input that a hook refused (which goes to its author) |

`Live` is a preview: it is never in the tree and nothing is lost if you ignore it. The node that
follows in `Appended` is the record. So a client draws the deltas as a layer over the cursor, and
replaces them with the node when it arrives. A client hears `Live` only for the cursors it passes
to `watch`, which starts with what each running sample has already streamed.

This program prints a transcript. It waits for the cursor to go busy and then come to rest, which
is how a frontend knows a turn is over.

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import Appended, CursorView, Live, Notice, TextDelta, first_cursor, start_session
from tau.cli import extension_flags, session_flags


async def listen(client, cursor, rested: asyncio.Event) -> None:
    working = streaming = False
    async for event in client:
        match event:
            case Live(cursor=c, delta=TextDelta(text=text)) if c == cursor:
                print(text, end="", flush=True)
                streaming = True
            case Appended(nodes=nodes):
                if streaming:
                    print()  # end the line the deltas were drawn on
                    streaming = False
                for node in nodes:
                    by = type(node.by).__name__
                    print(f"[node {node.id}: {type(node.body).__name__} by {by}]")
            case CursorView(cursor=c, busy=busy) if c == cursor:
                if working and not busy:
                    rested.set()
                working = working or busy
            case Notice(text=text):
                print(f"\n[{text}]")


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    started = await start_session(
        parser.parse_args(["--no-session"]), environ=os.environ, cwd=Path.cwd()
    )
    client = started.driver.client("me@here:transcript")
    cursor = await first_cursor(started.session, client)
    await client.watch([cursor])
    rested = asyncio.Event()
    listener = asyncio.create_task(listen(client, cursor, rested))

    await client.send(cursor, "What is 2 + 2?")  # input, with auto-step on
    await rested.wait()

    await client.close()  # ends the stream, so the listener ends
    await listener
    await started.stop()


asyncio.run(main())
```

```text expect
[node 2: User by Frontend]
mock: What is 2 + 2?
[node 3: Assistant by Provider]
```

An event stream can end three ways, and a frontend has one rule for all: catch
`ConnectionError`, then connect again.

- the stream just ends: the driver stopped;
- it raises `CutOff` (a `ConnectionError`): the client fell behind. The buffer is bounded
  (`driver.client(id, buffer=256)`): when it fills, `Live` events are dropped first, and a backlog
  of other events cuts the client off. Read the stream promptly;
- it raises `ConnectionError`: the driver failed, and the message says how.

Every call on a closed, cut-off or stopped client raises `ConnectionError` too. After a reconnect,
a client starts again from a snapshot and reads `walk(at)` for the nodes it needs. There is no
resume: the log is the truth.

## Calls

All calls are `async` and act for the client's id. In groups:

- **Conversation**: `send(cursor, text)` adds input and turns auto-step on; `input` adds without
  turning it on, and `step(cursor)` runs one step of a cursor at rest (`Busy` otherwise).
  `abort(cursor)` aborts the step and declines its open questions. `set_auto(cursor, on)`.
- **Moving**: `go_to(cursor, node)` moves the cursor, aborting a busy step, and turns auto-step
  off. Going to an earlier node and sending is how you branch. `open(node)` makes a new cursor
  there; `close_cursor` and `reopen`; `set_label` and `remove_label`.
- **Reading**: `node(id)`, `walk(id)` (root to the node), `children(id)`, and `nodes(start,
  limit)` for reading the whole tree in ranges. Node ids are dense and a node never changes, so
  cache them.
- **Commands and options**: below.
- **Questions**: below.

A call that fails raises a typed error. The common ones: `Busy` (the cursor is not at rest),
`NoSuchCursor`, `CursorClosed`, `NoSuchNode`, `NoSuchLabel`, `ValueError` (a value the driver
refuses), `ConfigError`. [The reference](../reference/errors.md) lists them.

## Command lines

A person types `/count` or `/model mock:echo`. The client reads the line for you:

- `await client.commands()` lists every command as a `Form`: its `name`, `description`, and the
  `schema` of its arguments (JSON Schema, with `"format"` on the named types such as `"node"`
  or `"path"`), and the `extension` that owns it.
- `await client.parse_line(cursor, line)` reads a line that starts with `/` into a `Parsed`:
  the `command`, its `args`, and `missing`, the required arguments not yet given, in order. Ask
  the person for each one, add it to the line (`name=value`), and parse again.
- `await parsed.run(client, cursor)` makes the call and returns the command's result, a string
  or `None`. It raises `CommandFailed` if the command did.
- `await client.complete_line(cursor, line)` returns what could finish the last word: command
  names after `/`, then the values of the argument, such as models or paths. This is for Tab.

`parse_line` raises `UnknownCommand` for a name nobody has. A frontend then sends the line as
text, with a warning, so the model can read a `/word` that is not a command. A bare name that two
extensions share is a plain `LookupError` ("/note is two commands: /a:note, /b:note"), which a
frontend shows without sending; the qualified `/a:note` always works.

A command that takes a `Cursor` runs as a step of that cursor, so it needs the cursor at rest and
**takes as long as it takes**: `run` returns when the command finishes. A frontend therefore runs
it as a task, so that it can still answer a question the command asks. `/config EXTENSION name=value`
is read as an extension's options (`client.options(cursor, extension)` is the form, and
`--save` in the line sets `parsed.save`), and `parsed.run` makes the call to `client.configure`.

## Questions

An extension that calls `cursor.ask(...)` puts an `OpenQuestion` in that cursor's `CursorView`:
an `id`, the `text`, `to` (a client id, or `None` for anyone) and the `schema` of the answer.
It stays there until someone answers, and the cursor is busy meanwhile.

- `await client.answer(question.id, value)` answers it. `value` is JSON: a string, number,
  boolean, list, or `None` to **decline**. The first good answer wins.
- A wrong value raises `ValueError` naming the problem, and the question stays open: show the
  message and ask again.
- An answer to a question that is closed or unknown raises `LookupError`. To a question meant for
  another client, `PermissionError`.
- `commandline.value(text, schema)` reads typed text by the schema (`"3"` into an integer,
  `"yes"` into one of a `Literal`'s values), and `commandline.values(schema, prefix, client,
  cursor)` says what could finish it, for Tab.

A frontend should show the question's text, the choices the schema gives (`enum`), and let the
person decline.

A question belongs to the cursor, not to a client. `to` reserves it for one client while that
client is connected. If that client is not connected, or disconnects, the question goes to the
**pool**: the questions anyone may answer. With no client at all, it waits.

This program runs a command that asks a question, with a tiny extension (`greeter.py`) loaded by
`-e`. It parses the line, runs it in a task, and answers when the question appears.

```python file=greeter.py
from tau import Cursor


class Greeter:
    name = "greeter"

    def __init__(self) -> None:
        self.commands = (self.greet,)

    async def greet(self, cursor: Cursor) -> str:
        """Ask who is there, and greet them."""
        name = await cursor.ask("Who is there?", str)
        return "nobody came" if name is None else f"hello, {name}"


extensions = [Greeter()]
```

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import CursorView, first_cursor, start_session
from tau.cli import extension_flags, session_flags
from tau.commandline import value


async def answer_questions(client) -> None:
    """The frontend's part: show a question and send an answer (here, always `Ada`)."""
    async for event in client:
        if isinstance(event, CursorView):
            for question in event.questions:
                print("asked:", question.text, question.schema)
                await client.answer(question.id, value("Ada", question.schema))


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    args = parser.parse_args(["--no-session", "-e", "greeter.py"])
    started = await start_session(args, environ=os.environ, cwd=Path.cwd())
    client = started.driver.client("me@here:commands")
    cursor = await first_cursor(started.session, client)
    answering = asyncio.create_task(answer_questions(client))

    print(await client.complete_line(cursor, "/gr"))
    parsed = await client.parse_line(cursor, "/greet")
    print(parsed.command.name, parsed.missing)  # nothing is missing: it takes no arguments
    print(await parsed.run(client, cursor))

    await client.close()
    await answering
    await started.stop()


asyncio.run(main())
```

```text expect
['/greet']
greet ()
asked: Who is there? {'type': 'string'}
hello, Ada
```

## Over the wire

`tau serve` runs one session and serves it on a Unix socket in `$TAU_HOME/serve/`. Any number of
clients may connect, so a terminal UI, an editor and a script can share one session. The server is
a normal start: it takes the same session, extension and prompt flags, and it changes into the
session's working directory. `-d` runs it in the background, and `--json` prints a record of one
object: the `socket`, the `pid`, the `session`, the `cwd`, the `protocol` number, the τ `version`,
and `started` (false if it was already running). The record is also the file
`$TAU_HOME/serve/<session id>.json`, so a second frontend finds it by listing that directory.

```bash
tau serve -d --json --name wire > record.json
cat record.json
```

`RemoteClient.connect(socket, id)` connects and says `hello`. It is a `Client`: the code above
works on it unchanged.

```python
import asyncio
import json

from tau import CursorView, RemoteClient


async def main() -> None:
    record = json.load(open("record.json"))
    async with await RemoteClient.connect(record["socket"], "me@here:remote") as client:
        print(client.welcome)
        cursor = None
        async for event in client:  # the snapshot comes first
            if isinstance(event, CursorView):
                cursor = event.cursor  # a real client shows every cursor
                break
        await client.watch([cursor])
        await client.send(cursor, "hello over the wire")
        working = False
        async for event in client:  # until the cursor goes busy, then rests
            if isinstance(event, CursorView) and event.cursor == cursor:
                if working and not event.busy:
                    break
                working = working or event.busy
        reply = (await client.walk(event.at))[-1]
        print(reply.body.parts[0].text)


asyncio.run(main())
```

```text expect
Welcome(protocol=1, version='<*>', session='<*>', cwd='<*>', pid=<*>)
mock: hello over the wire
```

```bash
tau serve --stop --session wire
tau sessions
```

Things that differ from a local client, and how they are the same:

- **Values must be JSON.** What you pass to `answer`, `run` and `configure` crosses the wire as
  JSON, so a `Path` that works locally fails remotely. Pass strings.
- **Errors keep their class.** `RemoteClient` raises the same `Busy`, `NoSuchCursor`,
  `ValueError`, `LookupError` and `UnknownCommand` a local client does, by the code the server
  sent. `ProtocolMismatch` means the server speaks another protocol version
  (`tau.PROTOCOL` is the one this client speaks); `ProtocolError` is a message the server refused as invalid (not JSON-RPC, or no such method).
- **Order.** A response arrives after the events its call caused: when `open` returns 3, the
  `CursorView` of cursor 3 has already arrived.
- **A connection is not a client.** If yours drops, its `watch` is gone, but a command it started
  keeps running, and the questions asked of it go to the pool for others to answer.
- **A reconnect is a new client** with the same id: connect again, `watch` again, read `walk(at)`.
- **A server that stops** ends your stream (`end`), or raises `ConnectionError` if the driver
  failed.

`tau serve` has no TCP and no token: the socket's file permissions are the whole security. To use
a server on another machine, forward the Unix socket with OpenSSH.

## What to build on

- `tau.commandline` (`parse`, `completions`, `value`, `values`) is the pure line-reading library
  behind `parse_line`, for a frontend that has the forms already and wants no round trip.
- `tau.to_markdown`, `tau.list_sessions`, `tau.find_session` and `tau.rename_session` are what
  `tau export`, `tau sessions` and `/name` use.
- [The reference for clients](../reference/clients.md) and for [events](../reference/events.md)
  have every signature.
