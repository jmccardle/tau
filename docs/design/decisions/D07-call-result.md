# D7: Calls stay in the `Assistant` node; one result node per call

Status: accepted, revised in review (Phase 1, 2026-10-26)

## Context

A sample produces text, thinking, and several calls at once. Tools then run,
and each finishes at its own time. How do calls and their answers sit in the
tree?

The plan's default was calls on the `Assistant` node and one `Results` node
per batch. That node could only be written when the slowest call finished. A
crash then lost every finished call, and one `Interrupted` was written for
all (D4).

## Decision

- **Calls stay inside the `Assistant` node** as `Call{id, tool, args_raw}`
  parts, in the order they streamed. A call is never a node of its own,
  because one sample produced them all (glossary, call).
- **One `Result` node per call**, written as soon as its call finishes, with
  the cursor moved onto it (model.md §2, §7).
- The `Call` parts of an `Assistant` node with `stop == tools` and at least
  one call are a **batch**; with no calls, it is an ordinary reply. Its results form a **run**: each under the previous, in
  completion order. The provider's conversion restores call order for the
  request (model.md §2).
- **I4**: on every walk each call gets exactly one result, checked locally
  at append. A `Result` must answer a call of the batch on `walk(parent)`
  that is not yet answered on that walk.
- Only a `stop == tools` reply is owed results. Other replies' calls are not
  runnable; the provider renders them as text (model.md §2, §11).
- An `args_raw` that does not parse is answered `Err("invalid arguments")`
  without running.
- Re-running a batch is a branch: a sibling run, or one that branches
  partway. Each line answers each call once. `Session.rerun` is the
  deliberate operation; landing on a batch otherwise fast-forwards (I3).

## Alternatives

| option | why not |
|---|---|
| One `Results` node per batch (plan default) | Loses finished calls on a crash, and forces a single `Interrupted` for the batch. |
| A node per call, before the reply | Calls are not separate events; one sample made them. It would also split one reply across nodes. |
| A merged call-and-result node | Breaks I2: the call exists before the result does, and nodes are written once. |
| Parse `args_raw` and store the object | The reply is stored as streamed. Re-serialising breaks byte-for-byte replay, and with it the provider's prefix cache (model.md §11). |

## Consequences

- A ten-minute call that finished is not lost because a sibling was still
  running.
- Tree order is completion order, which is true and crash-safe.
- Cost: the tree can hold several runs under one `Assistant` node, so
  landing needs the fast-forward rule and concurrent steps need the claim
  (I3, model.md §10).
- Cost: concurrent calls write results one at a time per cursor, so a slow
  `post_result` delays the next write. `pre_result` runs concurrently, as
  soon as each tool finishes, so it does not.
- Enforced by: I4's unit tests and proptest (no walk holds two results for
  one call, or a non-result node after a batch that is not complete), and the batch
  scenarios in model.md §4: tool batch, unknown tool, bad arguments, two
  cursors at one batch, re-run from `a1`, re-run that keeps `r5`.

## History

Plan default: calls on `Assistant`, one `Results` node per batch (I4).
Review reversed the second half: one result node per call, so each is
durable when it finishes. Settled by the "Decided in review" list; Q10 (a
result is final on its line), Q13 (two cursors, one run) and Q24 (executed
call not recorded by core) followed.
