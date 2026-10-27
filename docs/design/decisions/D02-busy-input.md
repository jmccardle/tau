# D2: Input is an item; the input phase delivers it

Status: accepted, revised in review four times (Phase 1, 2026-10-27)

## Context

A person types while a cursor is sampling or running tools. 0.15 answered
with submissions and a multitask strategy: queues, steering, and per-turn
state held in objects, which a reload had to guess back (glossary,
"Retired 0.15 terms").

Here a cursor is `{id, at}` and nothing else (D6). It has no place to keep a
queue, and a queue in driver or frontend memory is state that is neither
stored nor rebuildable (I7).

Under `tau serve`, many clients may attach to one cursor over the network,
switch to another cursor or session, or detach, while cursors keep working.
Every active cursor must keep operating with nobody watching. Input held by
a frontend, or in driver memory, is lost when that frontend goes away or the
process dies.

Cursors also need to send each other work: a sub-agent's task, or its answer.

## Decision

- **The scratchpad** is a persistent, modifiable list of items, off the
  tree: `{id, from: {frontend?, extension?, cursor}, to: cursor, data}`.
  - Anyone may add, update, or remove any item.
  - Adding an item wakes the cursor it is addressed to.
  - An extension reads its own cursor's scratchpad in `pre_step` and
    `post_step`; frontends read any of it at any time.
  - `to` names a cursor, not an extension, so extensions cooperate without
    permissions.
- **Input is the one kind of item core knows** (model.md §1, §7): an item
  whose `from` names a frontend client. `Session.input(cursor, input)` adds
  one, `{from: {frontend: client, cursor}, to: cursor, data: {text,
  images}}`. It is refused at once only while a question is open on the
  cursor.
- **The input phase delivers it.** The first phase of every step takes the
  cursor's pending input, oldest first. For each item, `pre_input` runs on
  its draft, then core commits a `User` node under `at` with
  `delivers: [item]`, authored `frontend(client)`, and moves onto it, as
  one record. The item is removed after. Two inputs from two clients are two
  `User` nodes in a row.
- **The input phase waits while `next == RunTools`** (Q40). Input given to a
  cursor running a batch is delivered by the next step, after the batch's
  `Results` node, never under the batch.
- **`pre_input` may refuse.** A string return refuses the input: its item is
  removed, its client gets a `notice` with the reason (Q43), and the step
  halts. A hook error leaves the input pending.
- **An abort leaves pending input pending** (Q42). The cursor rests; every
  frontend shows the items and may edit or remove them. An extension may
  offer them back to its client in `post_step(aborted)`.
- **Delivery is recorded once.** Load drops any item a node delivers. So an
  input is delivered at most once, and its content is stored once, in its
  node.
- **Interrupt and send is not a mechanism.** It is abort, then send (input
  plus auto-step on). The input lands under what the abort left.
- **Between cursors**, the same: an extension adds an item to the other
  cursor's scratchpad, and that cursor's own `pre_step` acts on it, writing
  a node with `cursor.write(…, delivers=[item])`.

## Alternatives

| option | why not |
|---|---|
| Core `append` that injects at commit (plan default) | There is no commit pointer. `append` writes a node and never moves a cursor (glossary). |
| A frontend buffer (plan default for queueing) | A client may switch away or detach under `tau serve`; the cursor it left must keep receiving input. A buffer dies with its client. |
| One kept input in the driver, a second refused (fourth revision) | Driver memory, so lost on a crash. A refused second input is an error the person did not need. |
| Pending inputs as `Custom` nodes, delivered by a `User` node with `copied_from` (fifth revision) | Every message stored twice, once pending and once delivered. And a node "for" another cursor is only a side node: nothing ties it to that cursor or guarantees anyone reads it. |
| A base extension that turns input into items in `pre_input` and delivers them in `pre_step`, with `deliver` and `combine` settings (sixth revision) | `pre_input` could not move the cursor, because it ran on a busy cursor outside the step, so it could not prepend anything. A config without the base extension refused all input. And `deliver`, `combine`, and returning items were policy in an extension every config needed. |
| A core-side queue per cursor | A reload could not restore it. A persisted scratchpad can be restored, and it is not a queue: nothing but input has an order core enforces. |
| A "delivered" marker beside the delivered node | Two writes; a crash between them delivers twice. `delivers` is part of the delivering record itself. |

## Consequences

- Every input takes one path, busy or at rest: an item, then the next step's
  input phase, through `pre_input`, which holds the cursor and may move it.
- Input to a busy cursor survives a crash, and is delivered at most once.
- Every frontend sees every cursor, every scratchpad, and every pending
  input, since all are in the session.
- Cost: input at a batch with no `Results` runs the batch first (Q17, Q40).
  To skip it, go to the reply's parent first.
- Cost: an item consumed without a node (a message an extension acted on
  without writing) may be seen twice after a crash. Consumers that cannot
  write a node must tolerate it.
- Enforced by: the I7 proptests (the scratchpad is part of the state a reload
  rebuilds; a crash between a delivering record and the removal leaves the
  item gone) and the Phase 2 scenarios: input at rest, input while busy
  (delivered by the next step, after the batch), refused input (notice,
  item removed), abort leaves input pending, abort then send, a crash before
  delivery, a cursor-to-cursor item that wakes an idle cursor.

## History

Plan default: core offers `append` (inject at commit) and `open_cursor`;
queueing is a frontend buffer. The fourth revision removed commit (D6) and
had the driver keep one input until `next != RunTools`, refusing a second.
The fifth revision made input hook-driven and held busy input as pending
`Custom` nodes. The sixth revision replaced those with the scratchpad and a
base extension that delivered in `pre_step`. The seventh revision made input
the one item kind core knows, delivered by the step's input phase, and
removed the base extension, `deliver`, and `combine`.
