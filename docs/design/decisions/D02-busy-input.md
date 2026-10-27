# D2: Input is hook-driven; queueing is the base extension's pending inputs

Status: accepted, revised in review twice (Phase 1, 2026-10-27)

## Context

A person types while a cursor is sampling or running tools. 0.15 answered
with submissions and a multitask strategy: queues, steering, and per-turn
state held in objects, which a reload had to guess back (glossary,
"Retired 0.15 terms").

Here a cursor is `{id, at}` and nothing else (D6). It has no place to keep a
queue, and a queue in driver or frontend memory is state that is neither a
node nor a move (I7).

Under `tau serve`, a frontend may switch to another cursor or another
session, or detach, while cursors keep working. Every active cursor must keep
operating with nobody watching. Input held by a frontend, or in driver
memory, is lost when that frontend goes away or the process dies.

## Decision

- **Input is entirely hook-driven** (model.md §7). `Session.input(cursor,
  text)` runs that cursor's `pre_input` hooks, busy or at rest. Each returns:
  - **handled**, optionally with a body and its `copied_from`. A body is
    written under `at` and moved onto, in an input step. On a busy cursor
    that deliberately interrupts the step in place (§8);
  - **pass**, with the input, possibly rewritten, to the next hook;
  - **refuse**, with a reason.
- If no hook handles it, the input is refused: "no extension handled this
  input". The frontend keeps the text. **Core has no default input behavior
  and no queue.**
- **The base extension** ships in the default config and can be replaced:
  - at rest, it writes the `User` node (together with any inputs still
    pending for the cursor, as one node whose `copied_from` lists them);
  - busy, it holds the input as a **pending input**,
    `Custom{kind: pending_input, cursor}`, a side node directly under the
    session's first root;
  - a cursor's input hooks and post hooks never run concurrently, so input
    to a busy cursor is either written before its post hooks run, and they
    deliver it, or seen once the cursor is at rest;
  - its post hooks deliver pending inputs: after the next batch completes
    or the next stop (`deliver`), one `User` node per input or all combined
    (`combine`);
  - its `post_open` hook delivers inputs still pending after a restart.
- **Delivery is recorded once.** The delivered `User` node carries
  `copied_from: [p1, …]`, written with its move as one durable write. That
  is the only delivery record. A pending input is one no node copies from;
  the store's in-memory reverse index of `copied_from` answers that in one
  lookup.
- **No cursor waits on a particular frontend connection.** Events go to
  whoever is subscribed, possibly nobody. A cursor may wait on a persisted
  pending record any frontend can answer (model.md §9, Q22).
- Other behaviors are input hooks too: "interrupt and send" handles input to
  a busy cursor with a `User` body; a command handles `/model fast` with a
  `Config` body; a gate refuses until satisfied.

## Alternatives

| option | why not |
|---|---|
| Core `append` that injects at commit (plan default) | There is no commit. `append` writes a node and never moves a cursor (glossary), and a node under an unstarted or partial batch is refused by I4 anyway. |
| A frontend buffer (plan default for queueing) | A frontend may switch away or detach under `tau serve`; the cursor it left must keep receiving input. A buffer dies with its frontend. |
| One kept input in the driver, a second refused (fourth revision) | Driver memory, so lost on a crash. A refused second input is an error the person did not need. And a special case in core that an extension can express. |
| A core-side input queue per cursor | State outside nodes and moves; a reload could not restore it (I7). Each person wants different steering anyway. |
| A "delivered" marker node beside the delivered `User` node | Two writes. A crash between them loses the marker, and the input is delivered twice. |

## Consequences

- Core's input rule is one sentence: run the input hooks. Everything else is
  an extension, and the base extension is replaceable.
- Input to a busy cursor survives a crash, and is delivered exactly once.
- Every frontend sees every cursor and every pending input, since both are
  in the session.
- Cost: the default config must list the base extension (anywhere: a post
  hook's move does not stop the other post hooks on the node). A config
  without an input hook refuses all input.
- Cost: a delivered input is a hook's write, so `post_input` does not run for
  it (Q31).
- Cost: input at rest at an unstarted batch runs the batch first (Q17).
- Enforced by: the I7 proptest (pending inputs are part of the state a
  reload rebuilds) and the behavior scenario "crash before delivery,
  `post_open` delivers once" (model.md §4, I7). Scenarios still to name in
  Phase 2: input at rest, input busy (pending, then delivered per `deliver`
  and `combine`), refused input, interrupt and send.

## History

Plan default: core offers `append` (inject at commit) and `open_cursor`;
queueing is a frontend buffer. The fourth revision removed commit (D6) and
had the driver keep one input until `next != RunTools`, refusing a second.
The fifth revision made input hook-driven and moved queueing into the base
extension's pending inputs, because under `tau serve` the frontend may switch cursors
or sessions while every active cursor keeps operating: no cursor ever waits
on a particular frontend connection.
