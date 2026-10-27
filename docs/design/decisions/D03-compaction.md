# D3: Compaction is an extension

Status: accepted (Phase 1, 2026-10-26)

## Context

In 0.15 compaction was a node kind, plus a splice: the model's view was a
fold of the path with the summary inserted and older nodes filtered out. "What
did the model see?" had no direct answer (I1, Why).

## Decision

- Compaction is an **extension** with no special standing in core
  (glossary, extension). Core has no compaction node kind and no splice.
- `walk(n)` stays a plain parent chain. Nothing is spliced or filtered.
- The extension works through ordinary means:
  - It builds a shorter line: a summary node plus copies of the recent nodes,
    each with `copied_from`. The copies include `Config` nodes, verbatim; a
    copied `Config` counts as explicit under I6.
  - The summary is a `User` node authored by the extension (Q30). Its
    `derived_from` lists the last node of the range it summarised and, for a
    cold summary, the detached-root reply that produced it.
  - A cold summary from a cheaper model is work on a detached root. Its
    `Config` root records which model did it.
  - A `pre_sample` hook returns the end of the new line as the context tip
    (Q25). The driver samples `walk(tip)` with `config_at(tip)`.
  - If `config_at(tip) != config_at(at)` and no explicit `Config` lies in
    (LCA(at, tip), tip], I6 restates the cursor's `Config`
    (`by: core`) under the tip so the sample runs with the cursor's own
    config. The reply goes under that `Config`.
  - To move the cursor itself onto the shorter line, an extension uses a
    move at a boundary (`pre_step`, or a post hook), as any hook does.
- The compaction extension is built in Phase 5. Its algorithm (warm and cold
  summaries, the deepest verbatim prefix, speculative candidates) is not
  designed here. That is open until Phase 5.

## Alternatives

| option | why not |
|---|---|
| Core node kind (plan option 2) | Puts a policy (what to keep, when, with which model) in core, and brings back a view that is not a walk. |
| Core filter on walks | Same, and breaks I1: the model's context would no longer equal `walk(parent)`. |

## Consequences

- I1 holds: for every provider node, the recorded context equals
  `walk(node.parent)`.
- The tree shows every compaction. Nothing happens invisibly (Q25).
- Phase 5 exit criterion: compaction needs no core change. If it does, stop
  and write an ADR (plan §3).
- Cost: copies duplicate bodies, and the summary is an ordinary `User` node
  authored by the extension (Q30).
- A copied batch must be copied with all its results: a move onto a copied
  batch that is not complete on the walk is refused, and the driver never
  runs a copied call (I3). A hook that crashes partway leaves an unlanded,
  uncommitted copy.
- Enforced by: I1's behavior test (`FakeProvider` records each context) and
  the scenario "compaction across a model change" (I6).
