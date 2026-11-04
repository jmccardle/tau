# D3: Compaction is an extension

Status: accepted (Phase 1)

## Context

In 0.15 compaction was a node kind plus a splice: the model's view was a
fold of the path with the summary inserted, so "what did the model see?" had
no direct answer.

## Decision

- Compaction is an **extension**. Core has no compaction kind and no splice;
  `walk` stays a plain parent chain.
- The extension builds a shorter line: a summary (`User`, by the extension,
  Q30, with `derived_from`) under a kept prefix, then copies of the recent
  nodes, `Config` included, each with `copied_from`. A copied batch is
  copied with its `Results`.
- A cold summary from a cheaper model is work on a detached root.
- Its `pre_sample` hook **moves the cursor** onto the new line (Q25). The
  sample sends `walk(at)` (I1) and the reply lands under `at`.
- The algorithm (warm and cold summaries, what to keep) is Phase 5's.

## Alternatives

| option | why not |
|---|---|
| A core node kind | Policy in core, and a view that is not a walk. |
| A core filter on walks | Breaks I1. |
| `pre_sample` returns a context tip | A second pointer beside `at`. |

## Consequences

- I1 holds; every compaction is visible in the tree.
- Phase 5 exit criterion: compaction needs no core change.
- Cost: copies duplicate bodies.
- Built in 8E (D24 "Built: 8E-3"): `copied_from` and `derived_from` are one optional node id each.
