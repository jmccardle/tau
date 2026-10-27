# D2: Input is hook-driven; input waits in the scratchpad

Status: accepted, revised in review three times (Phase 1, 2026-10-27)

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

- **Input is entirely hook-driven** (model.md §7). `Session.input(cursor,
  input)` runs that cursor's `pre_input` hooks, busy or at rest, outside its
  cycle. Each returns **handled**, **pass** (possibly rewritten), or
  **refuse**. If none handles it, the input is refused, and the frontend
  keeps the text. **Core has no default input behavior.**
- `pre_input` is not a boundary: it writes nothing under `at` and moves
  nothing. What input causes on the line happens in `pre_step`, between
  steps.
- **The scratchpad** is a persistent, modifiable list of items, off the
  tree: `{id, from: {frontend?, extension, cursor}, to: cursor, data}`.
  - Anyone may add, update, or remove any item.
  - Adding an item wakes the cursor it is addressed to.
  - An extension reads its own cursor's scratchpad only in `pre_step` and
    `post_step`; frontends read any of it at any time.
  - `to` names a cursor, not an extension, so extensions cooperate without
    permissions.
- **The base extension** ships in the default config and can be replaced:
  - `pre_input` adds the input as an item, busy or at rest;
  - `pre_step` delivers pending inputs as `User` nodes when `at` can take
    one (`deliver: after_batch` by default; `combine` joins consecutive
    inputs from one client);
  - `post_step`, after an abort, returns pending inputs to the clients that
    typed them (`cursor.tell`), or marks them returned for every frontend to
    show with send and edit buttons.
- **Delivery is recorded once.** `cursor.deliver` writes the `User` node with
  `delivers: [item ids]` and moves onto it as one durable write, then removes
  the items. Load drops any item a node delivers. So an input is delivered at
  most once, and its content is stored once, in its node.
- **Interrupt and send is not a mechanism.** It is abort, then send (input
  plus auto-step on). The input lands under what the abort left.
- **Between cursors**, the same: an extension adds an item to the other
  cursor's scratchpad, and that cursor's own `pre_step` acts on it.

## Alternatives

| option | why not |
|---|---|
| Core `append` that injects at commit (plan default) | There is no commit. `append` writes a node and never moves a cursor (glossary), and a node under an unstarted or partial batch is refused by I4 anyway. |
| A frontend buffer (plan default for queueing) | A client may switch away or detach under `tau serve`; the cursor it left must keep receiving input. A buffer dies with its client. |
| One kept input in the driver, a second refused (fourth revision) | Driver memory, so lost on a crash. A refused second input is an error the person did not need. |
| Pending inputs as uncommitted `Custom` nodes, delivered by a `User` node with `copied_from` (fifth revision) | Every message stored twice, once pending and once delivered. And a node "for" another cursor is only a side node: nothing ties it to that cursor or guarantees anyone reads it. |
| A core-side queue per cursor | The fifth revision rejected it because a reload could not restore it. A persisted scratchpad can be restored, and it is not a queue: it has no order core enforces and no consumer core picks. |
| A "delivered" marker beside the delivered node | Two writes; a crash between them delivers twice. `delivers` is part of the delivering write itself. |

## Consequences

- Core's input rule is one sentence: run the input hooks. Everything else is
  an extension, and the base extension is replaceable.
- Every input takes one path, busy or at rest: an item, then `pre_step`.
- Input to a busy cursor survives a crash, and is delivered at most once.
- Every frontend sees every cursor, every scratchpad, and every pending or
  returned input, since all are in the session.
- Cost: the default config must list the base extension. A config without an
  input hook refuses all input.
- Cost: an item consumed without a node (a discarded returned input, a
  message an extension acted on without writing) may be seen twice after a
  crash. Consumers that cannot write a node must tolerate it.
- Cost: input at rest at an unstarted batch runs the batch first (Q17).
- Enforced by: the I7 proptests (the scratchpad is part of the state a reload
  rebuilds; a crash between delivery and removal leaves the item gone) and
  the behavior scenario "crash before delivery, `pre_step` delivers once"
  (model.md §4, I7). Scenarios still to name in Phase 2: input at rest, input
  busy (pending, then delivered per `deliver` and `combine`), refused input,
  abort returns pending input to its client, a returned input when its
  client has gone, abort then send, a cursor-to-cursor item.

## History

Plan default: core offers `append` (inject at commit) and `open_cursor`;
queueing is a frontend buffer. The fourth revision removed commit (D6) and
had the driver keep one input until `next != RunTools`, refusing a second.
The fifth revision made input hook-driven and held busy input as pending
`Custom` nodes under the first root. The sixth revision replaced those with
the scratchpad, because a node is not addressed to anyone and storing every
input twice was waste, and made delivery a `pre_step` job for every input.
