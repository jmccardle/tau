# D7: Calls stay in the `Assistant` node; one `Results` node per batch

Status: accepted (Phase 1)

## Context

One sample produces text, thinking, and several calls; each call finishes in
its own time. How do calls and answers sit in the tree?

## Decision

- **Calls are parts of the `Assistant` node**, in stream order. A call is
  never a node.
- A `stop == Tools` reply with calls is a **batch**; other replies' calls are
  not runnable and are rendered as text.
- **One `Results` node per batch**, in call order, written at the join when
  every call has an outcome (`Ok`, `Err`, `Interrupted`). Until then the
  outcomes are in the step's memory (D4).
- The node is `by: Tools`; each outcome carries its own `by` (`Tools`, or
  the extension that answered or edited it), so blame survives one node per
  batch.
- I4 holds by construction. Core's only check is V2: distinct calls.
- The provider expands `Results` into one tool message per call.
- The tool, or a `pre_tool` hook, answers an `args_raw` it cannot use. Core and
  the driver do not parse it. (Changed 2026-10-29: it was `Err("invalid
  arguments")` by `Core` without running; that stopped a hook from repairing
  the arguments.)
- A re-run writes a sibling `Results`; each is final (Q10).

## Alternatives

| option | why not |
|---|---|
| One `Result` node per call | Needed runs, batch states, per-line I4 checks, fast-forward, and a claim. |
| One `Results`, outcomes journaled until the join | Plumbing in core for a pulled power cord (D4). |
| A node per call | One sample made them; it splits a reply. |
| A merged call-and-result node | Breaks I2. |
| Parse and store `args_raw` as an object | Breaks byte-for-byte replay and the provider's prefix cache. |

## Consequences

- A batch is answered or not, by one node. Outcomes are in call order, not
  completion order.
- Cost: no outcome is on the tree until the slowest call finishes; frontends
  show progress from events.
- Spiked: `batch_answers_in_call_order_then_samples_again`.
