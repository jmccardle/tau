# D2: Input is an item; the input phase delivers it

Status: accepted (Phase 1)

## Context

A person types while a cursor samples or runs tools. 0.15 kept submissions in
queues and per-turn objects that a reload had to guess back. Here a cursor is
`{id, at}` (D6) and has nowhere to keep a queue, and under `tau serve` the
client that typed may detach while the cursor keeps working. Cursors also
need to send each other work.

## Decision

- **The scratchpad**: a persistent, modifiable list of items off the tree,
  each addressed to one cursor. Anyone may add, update, or remove any item;
  adding one wakes its cursor. An extension reads its own cursor's items in
  `pre_step` and `post_step`.
- **Input is the one item kind core knows** (`ItemData::Input`).
  `Session.input` adds one; it is refused only for an unknown cursor.
- **The input phase delivers it**, whenever input is pending, even at an
  unanswered batch. Per item, oldest first: `pre_input`, then a `User` node
  under `at` with `delivers: [item]`, by `Frontend(client)`, and the move,
  as one record. Holding input behind a batch is an extension's choice.
- **A string from `pre_input`** refuses it: the item is removed, its client
  gets a notice, the step halts. A hook error leaves it pending.
- **Abort leaves pending input pending** (Q42).
- **Delivery is recorded once**, in `delivers`; load drops delivered items.
- Interrupt-and-send is abort, then send. Between cursors, an extension adds
  an item and the other cursor's `pre_step` writes it.

## Alternatives

| option | why not |
|---|---|
| A frontend buffer | Dies with its client under `tau serve`. |
| One input kept in driver memory | Lost on a crash. |
| Pending input as `Custom` nodes | Every message stored twice; nothing ties a node to a cursor. |
| A base extension that delivers in `pre_step` | Policy every config needed; `pre_input` could not move the cursor. |
| Input waits while `next == RunTools` | Queueing policy in core; an extension can hold input in `pre_input`. |
| A "delivered" marker beside the node | Two writes; a crash between them delivers twice. |

## Consequences

- One path for every input, busy or at rest; it survives a crash and is
  delivered at most once.
- An item consumed without a node may be seen twice after a crash.
- Spiked: `plain_turn`, `pre_input_string_refuses_with_a_notice`,
  `after_a_crash_mid_batch_input_goes_under_the_unanswered_batch`.
