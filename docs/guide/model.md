# The model in ten minutes

τ is built on one idea: **the conversation is a tree of nodes that never change, and everything
else is a pointer into the tree, a function of it, or a note in a scratchpad.** This page walks through the words you will
meet in the rest of the guides. [`docs/design/model.md`](../design/model.md) is the full specification
and [`glossary.md`](../design/glossary.md) defines every term.

## The session and its nodes

A **session** is one conversation's tree, kept in a directory on disk as an append-only log.
One process holds it at a time (it takes the session's **lease**; a second process that opens it
is refused), and it has one working directory, which its tools run in. `tau sessions` lists them.

A **node** is one thing that was said or decided. It has an `id` (a small integer, never reused),
a `parent`, a `time`, an author (`by`) and a `body`. A node is written once and never edited. The
bodies are:

| body | what it holds |
|---|---|
| `System` | the system prompt, written when the session is created |
| `User` | text a person (or an extension) sent |
| `Assistant` | the model's reply: `parts` (`Text`, `Thinking`, `Call`) and why it `stop`ped |
| `Results` | the outcomes of one **batch** (the tool calls one reply asked for), in call order |
| `Config` | a settings snapshot: which model, and extension options |
| `Custom` | a node an extension wrote for its own use; the model never sees it |

The `by` of a node says who wrote it: `Frontend` (a client, by its id), `Provider` (the model),
`Tools`, `Core`, or `Extension` (by name). Every session starts with a `Config` root and a
`System` node under it.

```python
from tau import Author, Body, Part, Session, Stop, model_config

session = Session.create(model_config("mock:echo"), "You are terse.", now=0)
first = session.cursors()[0]
system = session.cursor(first).at
me = Author.Frontend("guide")

ask = session.append(system, Body.User("What is 2 + 2?"), me, 1)
reply = session.append(ask, Body.Assistant([Part.Text("4")], Stop.End), Author.Provider(), 2)

print([n.id for n in session.tree.walk(reply)])  # the nodes from the root to the reply
```

`Session` is the tree and the step machine, with no IO: most programs reach it through a `Client`
instead, as the rest of the guides do. It is used here to show the data.

## Walks and what the model sees

The **walk** to a node is the path from the root down to it. The model sees exactly one thing:
the walk to the cursor's node, with the settings of the nearest `Config` on it. Nothing else
reaches the model, so a session replays and exports the same every time.

Nodes that no cursor's walk passes through are **side nodes**: an extension's notes, a failed
sample's record, a reply someone went back from. They stay in the tree.

## Cursors

A **cursor** is a pointer to a node (`at`). It is where the conversation is *now*. A session has
one or many: the first is opened for you, and `open(at)` makes another at any node. A cursor
also has:

- **labels**: names for it (`main`, `try-2`); a label names one cursor at a time.
- **auto-step**: whether it keeps stepping by itself, or only when asked.
- a **scratchpad**: items waiting for it, such as typed input that no step has taken yet.
- **busy** or **at rest**: whether a step is running.

A cursor can be **closed**; its last node and labels stay. `reopen` then makes a new cursor (with
a new id) at that node and moves the labels onto it.

A cursor's moves are recorded. Moving it is the only way `at` changes: when a reply is
committed, when a hook moves it, and when a frontend goes to another node (`go_to`).

## Branches

Nothing in a node is edited. To change your mind, move a cursor to an earlier node and write
from there: the new node is a second child of the old one, and that is a **branch**. Going back
to the `Assistant` node that asked for tools and stepping again runs the batch again.

```python continues
other = session.append(system, Body.User("What is 3 + 3?"), me, 3)

print(session.tree.children(system))  # the two branches from the system prompt
print(session.tree.lca(reply, other))  # where they split
second = session.open(other, me, 4)  # a second cursor, on the new branch
print(session.cursor(second).next)  # what the machine does next there
```

```text expect
[0, 1, 2, 3]
[2, 4]
1
Next.Sample
```

The walk is nodes 0 to 3; the system prompt (1) has children 2 and 4; the branches split at
node 1; and the new cursor's next is a sample, because the model owes a reply.

Every branch is still in the tree, and you can go back to any of them.

## The step

A **step** is how a cursor moves forward. It is one pass over a fixed loop, run for one cursor
and driven by the **driver**, which does the IO (the model call, the tools):

1. `pre_step` hooks run.
2. **Input**: if input is pending, each item becomes a `User` node.
3. **Sample**: if the walk ends in a `User` or `Results` node, the model is asked for a reply,
   which becomes an `Assistant` node.
4. **Tools**: if the reply asked for tools, they run and their outcomes become one `Results` node.
5. `post_step` hooks run, and the step **ends** (`Done`, `Halted`, `Failed` or `Aborted`).

With **auto-step** on, a step that moved the cursor is followed by another if work remains (a
reply to ask for, tools to run, or input waiting), so a model that asks for a tool gets the
result and keeps going. If work remains but the step did not move the cursor, trying again would
do the same thing, so auto-step turns off: **a cursor never spins in place**. A runaway loop where
the cursor does move (a model calling tools forever) is stopped by the `step_limit` extension,
which is on by default.

## Hooks

A **hook** is a function an extension offers for one point of the step. There are seven cursor
hooks, in the order a step reaches them: `pre_step`, `pre_input`, `pre_sample`, `pre_reply`,
`post_reply`, `post_result`, `post_step`; and two that run once for each tool call, `pre_tool` and
`pre_result`. A hook can read the tree, write nodes, move its cursor, change the draft about to
be written, or stop the step by returning a string (a tool hook's string fails only that one call).
Returning `None` carries on.

This one just says where a step is. It runs against the mock model, which echoes what it is
sent, so it needs a config that has one:

```json file=$TAU_HOME/config.json
{ "models": { "mock": { "provider": "mock", "models": { "echo": { "model": "echo" } } } } }
```

```python
import argparse
import asyncio
import os
from pathlib import Path

from tau import Cursor, Subject, first_cursor, start_session
from tau.cli import extension_flags, session_flags


class Tracer:
    name = "tracer"

    async def pre_step(self, cursor: Cursor, subject: Subject) -> None:
        print("pre_step    at node", cursor.cursor(cursor.id).at)

    async def pre_input(self, cursor: Cursor, subject: Subject) -> None:
        print("pre_input  ", subject.draft.body.text)

    async def pre_sample(self, cursor: Cursor, subject: Subject) -> None:
        print("pre_sample")

    async def post_reply(self, cursor: Cursor, subject: Subject) -> None:
        print("post_reply   node", subject.node.id)

    async def post_step(self, cursor: Cursor, subject: Subject) -> None:
        print("post_step   ", subject.ending)


async def main() -> None:
    parser = argparse.ArgumentParser(parents=[extension_flags(), session_flags()])
    args = parser.parse_args(["--no-session"])  # keep it in memory
    started = await start_session(
        args, environ=os.environ, cwd=Path.cwd(), model="mock:echo", own=[Tracer()]
    )
    client = started.driver.client("guide")
    cursor = await first_cursor(started.session, client)

    await client.send(cursor, "What is 2 + 2?")  # input, with auto-step on
    await started.driver.rest(cursor)  # wait for the cursor to come to rest
    here = started.session.cursor(cursor).at
    print("branch:", [n.id for n in started.session.tree.walk(here)])

    await client.close()
    await started.stop()


asyncio.run(main())
```

```text expect
pre_step    at node 1
pre_input   What is 2 + 2?
pre_sample
post_reply   node 3
post_step    Done
branch: [0, 1, 2, 3]
```

[Writing an extension](extensions.md) covers hooks in full.

## Extensions, tools, commands and options

An **extension** is an object with a `name` and any of: hooks; `tools` the model can call;
`commands` a person types as `/name`; and `options`, settings kept in the config. A **tool**
is a function the model calls (`read`, `write`, `edit`, `grep`, `find` and `bash` are the base tools, which a session has unless the `base_tools` extension is off); the
model is given its name, its docstring and a schema built from its type hints. An extension
can also **ask a question** of whoever is watching (a permission, a choice) and wait for the
answer.

## Clients and the driver

The **driver** runs a session's cursors over providers (the model APIs), tools and extensions.
A frontend does not talk to it directly. It holds a **client**:

- calls: `send`, `step`, `go_to`, `open`, `run` a command, `answer` a question, and reads such as
  `walk` and `children`;
- one **event stream**, which the client itself is (`async for event in client`): a `CursorView`
  for each cursor whenever it changes, `Appended` for new nodes, `Live` for a reply as it streams
  in, `Notice` for a message to one client or to all.

A client is a connection with an id, not a person. Several clients can watch one session, and
they may be in another process: `tau serve` gives each connection the same `Client` over a
socket, so a terminal UI, an editor and a script can all use one session.

## What is kept

A session reloads by replaying its log: nodes, cursor moves, labels, closes and scratchpad items.
Auto-step, whether a cursor is busy, open questions, and what an extension shows on a cursor are
*not* kept: a reloaded session wakes every cursor at rest with auto-step off, and an extension
that wants to resume after a crash turns its cursor's auto-step on itself. τ does not repair or
guess.

## Read on

- [Writing an extension](extensions.md) and [writing a frontend](frontends.md).
- [`docs/design/model.md`](../design/model.md): the tree, the invariants (I1 to I7) and the step in full.
- [`docs/design/glossary.md`](../design/glossary.md): every term, with where it lives in the code.
