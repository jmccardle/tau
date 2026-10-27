# D7: Calls stay in the `Assistant` node; one `Results` node per batch

Status: accepted, revised in review twice (Phase 1, 2026-10-26; seventh revision 2026-10-27)

## Context

A sample produces text, thinking, and several calls at once. Tools then run,
and each finishes at its own time. How do calls and their answers sit in the
tree?

The plan's default was calls on the `Assistant` node and one `Results` node
per batch. That node could only be written when the slowest call finished,
so a crash lost every finished call (D4). Revisions one to six answered with
one `Result` node per call; the seventh returns to the plan's shape and
solves the crash cost with the journal.

## Decision

- **Calls stay inside the `Assistant` node** as `Call{id, tool, args_raw}`
  parts, in the order they streamed. A call is never a node of its own,
  because one sample produced them all (glossary, call).
- The `Call` parts of an `Assistant` node with `stop == tools` and at least
  one call are a **batch**; with no calls, it is an ordinary reply.
- **One `Results` node per batch**, `{outcomes: [{call, outcome}]}` in call
  order, committed under the batch at the **join**, when every call has an
  outcome (model.md §2, §6). `outcome` is `Ok`, `Err`, or `Interrupted`.
- **The journal** keeps finished calls across a crash: each call's outcome is
  a scratchpad item until the `Results` node delivers it (D4).
- **I4 holds by construction**: the join writes one outcome per call, and
  nothing writes half a `Results` node. Core's only append check is that a
  `Results` node's outcomes name distinct calls (I5).
- **The provider expands `Results`** into one tool message per call when a
  walk is sent. A `Results` node anywhere else (under a `User` node, say) is
  made legal by the provider's conversion, for example as a `User` message
  (model.md §10).
- Only a `stop == tools` reply runs calls. Other replies' calls are not
  runnable; the provider renders them as text.
- An `args_raw` that does not parse is answered `Err("invalid arguments")`
  without running.
- Re-running a batch is going to its `Assistant` node and stepping. It writes
  a sibling `Results` node. Each `Results` node is final (Q10).

## Alternatives

| option | why not |
|---|---|
| One `Result` node per call, moved onto as each finishes (revisions one to six) | Needed runs, batch states (unstarted, partial, complete), per-line I4 checks at append, fast-forward on landing, and a claim so two steps would not run one batch. Each existed only because a batch's answer was spread over several nodes. |
| One `Results` node per batch with nothing saved before the join (plan default) | Loses finished calls on a crash. The journal fixes that without changing the node. |
| A node per call, before the reply | Calls are not separate events; one sample made them. It would also split one reply across nodes. |
| A merged call-and-result node | Breaks I2: the call exists before the result does, and nodes are written once. |
| Parse `args_raw` and store the object | The reply is stored as streamed. Re-serialising breaks byte-for-byte replay, and with it the provider's prefix cache (model.md §10). |

## Consequences

- A batch is answered or not, by one node. `next` reads only the walk, and
  the cursor never looks at children.
- A ten-minute call that finished is not lost because a sibling was still
  running: its journal item survives.
- Tree order no longer records completion order; outcomes are in call order.
- Cost: no outcome is in the tree until the slowest call finishes. A frontend
  shows finished calls from the journal items.
- Cost: one scratchpad rewrite per finished call (D12).
- Enforced by: I4's unit tests and proptest (every `Results` node the tools
  phase wrote answers each call of its parent exactly once), and the batch
  scenarios: tool batch, unknown tool, bad arguments, `pre_tool` blocks one
  call of two, abort during tools, crash mid-batch, two cursors at one
  batch, re-run by going to the `Assistant` node and stepping.

## History

Plan default: calls on `Assistant`, one `Results` node per batch (I4).
Review reversed the second half: one result node per call, so each is
durable when it finishes. The seventh revision reversed it back. In review:
"we don't need the invariant of '1 node = 1 chat message submitted over the
API'; we can have one result node and expand it to 3 tool calls and 3 tool
responses when we walk the tree." The journal in the scratchpad keeps the
durability that per-call nodes had bought.
