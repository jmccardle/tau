# Writing a frontend

A frontend shows a session and takes input: a terminal UI, an editor panel, a script, a bot.
It holds a **`Client`**, one object that does everything a frontend may do. Apart from opening a
session and taking a client from it, a frontend never touches the driver, the session or the
providers. There are two kinds of client, and the code that uses one is the same for both:

- **local**: your program starts the session itself, and `driver.client(id)` makes a
  `LocalClient`;
- **remote**: another process runs `tau serve`, and `RemoteClient.connect(socket, id)` makes a
  `RemoteClient` over its Unix socket. A client in another language speaks
  [the wire protocol](protocol.md) instead.

`open_client` picks for you: it opens the session the flags name, as a remote client of the
`tau serve` that holds it, else as a local one.

[`examples/chat.py`](../../examples/chat.py) is a whole small frontend (a line chat, with
commands, questions and Tab completion) over a local client. The examples here use the mock
model, which echoes what it is sent, so they need no server:

```json file=$TAU_HOME/config.json
{
  "default_model": "mock:echo",
  "models": { "mock": { "provider": "mock", "models": { "echo": { "model": "echo" } } } }
}
```

## Open a session and get a client

`open_client` is the start `tau` itself uses. It reads the same flags (`-c`, `--session`, `-e`,
`--append-system-prompt`...). If the session they choose is held by a running `tau serve`, it
**attaches**: the client is a `RemoteClient` of that server. Otherwise it runs `start_session`,
which loads the config, the extensions and the system prompt, creates or opens the session and
starts the driver, and the client is a local one. Add the flags to your own parser with the
parent parsers `session_flags()` and `extension_flags()` (and `option_flags()` for `--set`), and
pass what it parsed.

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import open_client
from tau.cli import extension_flags, session_flags


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    args = parser.parse_args(["--no-session"])  # a real program passes sys.argv[1:]
    opened = await open_client(args, client="me@here:guide", environ=os.environ, cwd=Path.cwd())
    client, cursor = opened.client, opened.cursor
    print("client", client.id, "is on cursor", cursor, "of session in", opened.started.cwd)

    await opened.close()


asyncio.run(main())
```

What to know:

- **The id** of a client (`john@laptop:tui`) says who is acting. Nodes you write are authored
  `Frontend(id)`. Keep it stable across reconnects.
- **A local start changes the process's working directory** to the session's, so the base
  tools, path completion and relative paths all agree with it. An attached client does not: the
  server's directory is `opened.welcome.cwd`.
- **`opened.cursor`** is the cursor to show. Every cursor of a session may have been closed, and
  this reopens the first one if so: a new cursor at its last node, with its labels.
  `opened.snapshot` holds the events that were read to find it (the snapshot, up to `Labels`), so
  you can draw from them. `opened.welcome` is the server's `Welcome`, or for a local start the
  same fact (session id, directory, pid); it is `None` for a session kept in memory.
- **Attaching has limits.** The server's extensions and options are loaded already, so `-e`,
  `--no-extensions` and `--set` are a `ConfigError` for a session a server holds, as are `--model`
  and the prompt flags (as for any existing session). A `--fork` or a new session never attaches.
- **It can refuse**, and a frontend should print the message and stop: `ConfigError`,
  `TrustError` (an untrusted project: run `tau trust`), `ExtensionLoadError`, `UnknownSession`,
  `Leased` (another process holds that session and is no server; `tau --fork` copies it). Pass
  `prompts=tau.prompts(stdin, stdout)` and a terminal gets asked to trust a project or pick among
  several models, instead of failing.
- **`opened.close()`** closes the client, and for a local start stops the driver and releases the
  session. A session that was never used is deleted. `opened.started` is the `Started` of a local
  start (`None` when attached) if you need the driver or the session.
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

from tau import Appended, CursorView, Live, Notice, TextDelta, open_client
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
    opened = await open_client(
        parser.parse_args(["--no-session"]),
        client="me@here:transcript",
        environ=os.environ,
        cwd=Path.cwd(),
    )
    client, cursor = opened.client, opened.cursor
    await client.watch([cursor])
    rested = asyncio.Event()
    listener = asyncio.create_task(listen(client, cursor, rested))

    await client.send(cursor, "What is 2 + 2?")  # input, with auto-step on
    await rested.wait()

    await opened.close()  # ends the stream, so the listener ends
    await listener


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
an `id`, the `text`, the `extension` that asked, `to` (a client id, or `None` for anyone) and the
`schema` of the answer.
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

from tau import CursorView, open_client
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
    opened = await open_client(args, client="me@here:commands", environ=os.environ, cwd=Path.cwd())
    client, cursor = opened.client, opened.cursor
    answering = asyncio.create_task(answer_questions(client))

    print(await client.complete_line(cursor, "/gr"))
    parsed = await client.parse_line(cursor, "/greet")
    print(parsed.command.name, parsed.missing)  # nothing is missing: it takes no arguments
    print(await parsed.run(client, cursor))

    await opened.close()
    await answering


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

## The terminal UI

`tau` with no `-p` is a frontend like the ones above: `tau.tui.TauApp` is a Textual app over a
`Client`, and it touches nothing else. `tau` opens the session the flags name with `open_client`
(a trust question is asked on the plain terminal before the screen opens) and runs the app on
the same event loop as the driver, so a session you start and a session a `tau serve` holds are
the same screen. If the connection to a server drops, the app connects again with the same client
id and redraws from the new snapshot; if the session itself fails, the screen says so, keeps the
transcript readable, and `tau` exits 1 when you quit.

You can embed it, and test one with Textual's pilot:

```python skip
app = TauApp(client, cursor, welcome=opened.welcome, home=tau_home, snapshot=opened.snapshot)
async with app.run_test(size=(120, 40)) as pilot:
    await pilot.press(*"hello", "enter")
    print(app.screen_text())
```

The screen, top to bottom: the header (the first message, the session id, the working directory),
the **transcript** of the cursor it views, the open **questions**, the **pending** lines, the
command popup, the editor and the status bar (the cursor's model, whether it is working,
auto-step, how the last step ended, a failed hook, and a line for each extension that shows
something).

- The transcript is the walk of the cursor: your messages, the model's replies with their thinking
  folded and a box per tool call (folded; its mark says whether it worked), the live text of the
  reply in flight, and a message someone else sent to the cursor under that author's name.
- Enter sends, whether the cursor is busy or not: what you send while it works is a pending item
  (shown above the editor, with its author if someone else sent it) and the next step takes it.
  Ctrl+J starts a new line. Up and Down at the first and last line walk the lines you sent.
- Esc aborts the step. Ctrl+C aborts if the cursor is busy, else empties the editor, else quits
  when pressed twice; Ctrl+Q quits. Ctrl+R folds the thinking, Ctrl+T the tool boxes.
- `/` lists the commands; Tab completes the name, then each argument by what it is (models,
  cursors, nodes, paths). A command typed without its arguments opens a form for them. An
  unknown `/name` is sent as text, and a bare name two extensions share is refused (type
  `/extension:name`). The result of a command is a toast.
- A question is a box above the editor, titled by the extension that asks. A choice has a button
  for each answer; anything else has a field (Tab completes it as a command's argument is) and an
  Answer button. Decline, or Esc inside the box, answers nothing. An answer the session refuses
  stays in the box with the reason.
- Notices (`cursor.tell`, a command's progress) are toasts.

## What to build on

- `tau.commandline` (`parse`, `completions`, `value`, `values`) is the pure line-reading library
  behind `parse_line`, for a frontend that has the forms already and wants no round trip.
- `tau.to_markdown`, `tau.list_sessions`, `tau.find_session` and `tau.rename_session` are what
  `tau export`, `tau sessions` and `/name` use.
- [The reference for clients](../reference/clients.md) and for [events](../reference/events.md)
  have every signature.
