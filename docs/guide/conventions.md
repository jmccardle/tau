# Conventions clients and extensions may rely on

τ's protocol carries the tree, the cursors and a few events, and no more than a client cannot work
out for itself. Where a client can work something out from what the tree already holds, τ writes
down the shape and keeps to it, and does not add a call. These are those shapes: documented, not
protocol. A convention is as stable as the protocol is (`PROTOCOL` 3): a change to one is a change
for every client, so it is a release note.

Each is named by an id (`CV-NN`) that the design notes and the release notes use.

| id | what | read |
|---|---|---|
| CV-01 | what `/undo` follows: a `Custom` node of kind `undo`, and what `/undo` answers | [Undo markers](#cv-01-undo-markers) |
| CV-02 | what a reply cost: `Assistant.meta.usage`, and `/usage json` for money | [Usage](#cv-02-usage) |
| CV-04 | which cursor a sub-agent belongs to: its label | [Sub-agent parentage](#cv-04-sub-agent-parentage) |

## CV-01: undo markers

A change that `/undo` can take back leaves an **undo marker** in the tree: a `Custom` node of
kind `undo`, written by the extension that made the change, whose `data` is a JSON object.

```json
{"label": "/compact", "return_to": 41, "reverses": "compact", "priority": "move"}
```

| key | what it holds |
|---|---|
| `label` | what was done, as a person would name it: `/compact`, `/clear`, `edit of a.py` |
| `return_to` | the node going back puts the cursor on: where the cursor was before the change |
| `reverses` | the kind of change, a short name its owner chooses: `compact`, `prune`, `clear`, `checkpoint` |
| `priority` | which `/undo` follows it: `move` (the default when the key is absent) or another name, such as `file` |
| anything else | the owner's own data (a checkpoint's commit, say), which `/undo` passes back to the owner |

What a client and an extension may rely on:

- **The writer is the owner.** The node's `by` is `Extension(name)` of the extension that made the
  change. `compaction`, `prune` and `session` (`/clear`) write `move` markers.
- **A marker is never sent to a model**, like every `Custom` node, and it **decides nothing**: the
  cursor rests or goes on as the node before the marker says. Compaction at the hard limit writes
  its marker in the middle of a turn, and the turn carries on under it.
- **The walk is the undo stack.** `/undo` walks back from the cursor's node toward the root, so it
  only ever reaches markers on that cursor's branch, nearest first, and undoing a change moves
  the cursor off its marker.
- **`move` markers need no owner.** Going back is a move of the cursor to `return_to`, so a
  `move` marker still works when the extension that wrote it is switched off.
- **Any other priority is the owner's to reverse.** A plain `/undo` passes over it (so a file
  checkpoint written mid-turn never takes over "edit my last message"); `/undo file` follows the
  nearest `file` marker, and only if the extension that wrote it is loaded and reverses it.
- **Without the `undo` extension**, markers are inert records: the session, the tree screen and
  `tau export` treat them as any `Custom` node.

`tau.UndoMarker` writes and reads them. `UndoMarker.of` gives `None` for any other body and a
`ValueError` naming the problem for a marker whose data is not this shape:

```python
from tau import Body, UndoMarker

marker = UndoMarker("edit of a.py", 12, "checkpoint", "file", {"commit": "abc123"})
body = marker.body()
assert isinstance(body, Body.Custom) and body.kind == "undo"
print(body.data)
print(UndoMarker.of(body) == marker, UndoMarker.of(Body.User("hi")))
```

```text expect
{"commit": "abc123", "label": "edit of a.py", "return_to": 12, "reverses": "checkpoint", "priority": "file"}
True None
```

### Writing a marker from an extension

Write the marker after the change, on the line the cursor goes on from. A command or hook that
moves the cursor to a new line puts the marker at the end of that line and moves onto it; one that
changes something outside the tree writes it under the cursor (`cursor.write`), with `return_to`
the node it should go back to.

An extension whose markers are not `move` reverses them itself: it defines `reverse`, which
`/undo <priority>` calls (through `Cursor.reverse`) before it moves the cursor. `reverse` is given
a cursor handle bound to the extension and the marker. Raising refuses the undo, and the cursor
stays where it was. A file checkpoint, in outline:

```python skip
from tau import Cursor, Subject, UndoMarker


class Checkpoints:
    name = "checkpoints"

    async def post_result(self, cursor: Cursor, subject: Subject) -> None:
        assert subject.node is not None and subject.node.parent is not None
        commit = snapshot_the_working_tree()  # yours
        marker = UndoMarker("edit", subject.node.parent, "checkpoint", "file", {"commit": commit})
        cursor.write(marker.body())

    async def reverse(self, cursor: Cursor, marker: UndoMarker) -> None:
        restore_the_working_tree(marker.extra["commit"])  # yours; raise to refuse
```

`/undo file` then restores the files and puts the cursor back on the batch's node, before its
results. A step that would carry the turn on from there (run the tools again) is halted instead,
and the status bar says so: step to carry on.

### What `/undo` answers

`/undo` answers text for a person (`undid /compact: back to node 41`; for the last message, `back
to node 12, before your message:` and the message; with nothing to go back over, `nothing to undo
on this branch`). With `format=json` (`client.run(cursor, "undo", {"format": "json"})`, or `/undo
format=json` typed) it answers an object a client acts on:

```json
{"from": 44, "to": 12, "marker": null, "label": "your last message", "user": {"node": 13, "text": "fix the parser"}}
```

`marker` is the marker's node (`null` for the last message), and `user` is what to put back in an
editor: the message as it was typed (`Node.typed`, else its text), or `null` when a change was
undone. Pass `at` (the node the client shows the cursor on) and `/undo` refuses if the cursor has
moved since, so two people pressing undo on one cursor undo one change, not two. The terminal UI's
Ctrl+Z does exactly this when `undo` is loaded.

## CV-02: usage

Every reply a provider sampled carries what the server said it used, in `Body.Assistant.meta`:
a JSON object held as text, in the field `usage`. A client may read it from each node it holds and
add the nodes up, with no call to τ. Money is the exception: prices are the server's
configuration, so a client that wants cost asks `/usage json`.

### The shape of `meta.usage`

`meta` is text; parsed, it is an object with the reply's facts, among them:

```json
{
  "usage": { "input": 12300, "output": 812, "cached": 11800, "written": 0, "reasoning": 140 },
  "usage_raw": { "input_tokens": 500, "output_tokens": 812, "cache_read_input_tokens": 11800 }
}
```

`usage` is the normalized report, and the same for every provider. Every value is a non-negative
integer, a count of tokens:

| key | always there | meaning |
|---|---|---|
| `input` | yes | every prompt token the request held, cached and written ones included |
| `output` | yes | every token the model produced, thinking included |
| `cached` | no | the part of `input` the server read from its prompt cache. Absent: the server did not say. `0`: it said, or (Gemini) left it out, and it read nothing |
| `written` | no | the part of `input` the server processed and stored in the cache |
| `written_1h` | no | the part of `written` kept for an hour (Anthropic) |
| `reasoning` | no | the part of `output` that was thinking |
| `tool_prompt` | no | the part of `input` a server-side tool's use added (Gemini) |

A key that is absent was not reported; do not read it as `0`. The parts never exceed their whole:
`cached + written <= input`, `written_1h <= written`, `tool_prompt <= input`. τ drops the whole
`usage` of a reply that breaks this, or that sent a malformed count, rather than hand a client a
number that cannot be right. `usage_raw` is what the server sent, as it sent it, kept for a person
to read; its shape is the provider's and no client should depend on it.

A reply with no `usage` key reported none. That is a fact to show ("no usage"), not a zero: a reply
that was aborted, or that a server sent without counts, still used something.

### Adding replies up

A client that sums `usage` over nodes must count the same replies `/usage` counts, or its total
will differ. A node counts when all three hold:

- its body is an `Assistant` and its `by` is `Provider` (a reply an extension wrote in place of a
  sample was never billed);
- `copied_from` and `derived_from` are both unset (compaction and prune write copies of replies
  that are already counted; the copy is not a second bill);
- it has a `usage`.

Replies in the tree that are not on the cursor's walk count too: a branch you left, a summary or
a sub-agent spent real tokens. To total a session, add every node, not the walk.

```python skip
import json


def totals(nodes):
    """Tokens in and out, over every reply the session paid for."""
    tokens_in = tokens_out = 0
    for node in nodes:
        body = node.body
        if not isinstance(body, tau.Body.Assistant) or body.meta is None:
            continue
        if node.copied_from is not None or node.derived_from is not None:
            continue
        if not isinstance(node.by, tau.Author.Provider):
            continue
        usage = json.loads(body.meta).get("usage")
        if usage is not None:
            tokens_in += usage["input"]
            tokens_out += usage["output"]
    return tokens_in, tokens_out
```

### Money: `/usage json`

Cost needs the model's price, which is configuration only the server has (`models.<name>.usage.price`),
and a reply is priced by the model its branch was on at that node. A client asks for it with the
command `usage` and its `format` argument, which takes `text` (the default: the table a person
reads) or `json`: `client.run(cursor, "usage", {"format": "json"})`, or `/usage json` typed. The
result is a JSON object as text:

```json
{
  "models": [
    {
      "model": "anthropic:sonnet", "configured": true, "priced": true,
      "samples": 12, "input": 210000, "output": 9800,
      "cached": 191000, "cached_input": 210000, "written": 4100,
      "cost": "1.270000", "silent": 0, "aborted": 0,
      "estimates": 0, "estimated_cost": null
    },
    {
      "model": "local:qwen", "configured": true, "priced": false,
      "samples": 3, "input": 36900, "output": 2400,
      "cached": 0, "cached_input": 0, "written": 0,
      "cost": null, "silent": 0, "aborted": 0,
      "estimates": 0, "estimated_cost": null
    }
  ],
  "total": {
    "configured": true, "priced": true, "samples": 15, "input": 246900, "output": 12200,
    "cached": 191000, "cached_input": 210000, "written": 4100,
    "cost": "1.270000", "silent": 0, "aborted": 0, "estimates": 0, "estimated_cost": null
  },
  "cost_complete": false
}
```

- `models` has a row for every model that has replied, in the order each first replied, and
  `total` is their sum (it has no `model`).
- `samples` counts replies that reported usage, and `input`, `output`, `cached` and `written` add
  their `meta.usage`. `cached_input` is the `input` of those replies that said whether they read the
  cache, so `cached / cached_input` is the hit rate, and is not skewed by a server that never says.
- `cost` is dollars as an exact decimal string (never a float, never scientific notation), or
  `null` for a model with no price: `null` is "unknown", never "free". `cost_complete` is false
  when a model that replied has no price, so the `total.cost` leaves it out (the text form ends that
  total with `+`).
- `silent` counts replies that reported no usage and `aborted` those cut off before they did.
  τ estimates what they cost, apart from `cost`: `estimates` is how many and `estimated_cost` their
  guessed price, or `null` when nothing is estimated. `/usage` and the spending limit count the
  estimate; `cost` does not.
- Cost is worked out when you ask, from the prices in the config the session was started with.

The figures are those of the text form: `/usage` and `/usage json` read the same rows.

## CV-04: sub-agent parentage

A client that draws the cursors of a session may group a sub-agent under the cursor that started
it. The label says which. τ's `agents` extension labels each cursor it opens

```text
agent:P:N
```

where `P` is the id of the cursor whose `agent` call opened it, in decimal, and `N` counts that
cursor's agents from 1, so the first agent the main cursor (id 0) starts is `agent:0:1`, the next
`agent:0:2`, and one started by a branch on cursor 2 is `agent:2:1`. Read it with the pattern
`^agent:(\d+):(\d+)$`; anything that starts with `agent:` and does not match is not τ's.

- **Where a client sees it.** Labels are in the session snapshot's `Labels` event and in each
  later one, as `(label, cursor)` pairs. A cursor with a label of this shape is a sub-agent of
  cursor `P`, whether or not `P` is still open and whether or not the agent is: an agent is
  closed once it has answered, and its label stays on the closed cursor as its restart record.
  `Labels` is in the snapshot, so a client that attaches late groups them too.
- **Depth is one.** An agent is not offered the `agent` tool, so a sub-agent has no sub-agents of
  its own and no label has two parents.
- **Continuing.** The model continues an agent with `to="agent:P:N"`. The label does not change, and
  neither does `P`: the parent is the cursor that first started the agent.
- **The name the model reads.** The result of a call starts `agent:P:N answered:`, so the same label
  appears in the tree's `Results` node, and a client can match a result to its cursor.
- **Other labels.** Other extensions label their cursors for their own use (`compaction:1`, a
  character, a search): none of them is a sub-agent, and none starts with `agent:`.

The parent is not in the tree as a node, and not on `CursorView`: a client that wants the group
reads the label, and a client that does not draws the cursors flat, as before.
