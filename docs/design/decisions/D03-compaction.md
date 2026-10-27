# D3: Compaction is an extension

Status: accepted (Phase 1, 2026-10-26; updated in the seventh revision)

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
    each with `copied_from`. The copies include `Config` nodes, verbatim.
  - The summary is a `User` node authored by the extension (Q30). Its
    `derived_from` lists the last node of the range it summarised and, for a
    cold summary, the detached-root reply that produced it.
  - A cold summary from a cheaper model is work on a detached root. Its
    `Config` root records which model did it.
  - A `pre_sample` hook **moves the cursor** to the end of the new line
    (Q25). The sample sends `walk(at)` with `config_at(at)` (I1), and the
    reply is committed under `at`. The cursor stays on the shorter line.
  - The shorter line's config is the nearest `Config` on it, usually a copy.
    If the extension wants another config there, it appends a `Config` and
    moves onto it (I6 is retired, D5).
- The compaction extension is built in Phase 5. Its algorithm (warm and cold
  summaries, the deepest verbatim prefix, speculative candidates) is not
  designed here. That is open until Phase 5.

## Alternatives

| option | why not |
|---|---|
| Core node kind (plan option 2) | Puts a policy (what to keep, when, with which model) in core, and brings back a view that is not a walk. |
| Core filter on walks | Same, and breaks I1: what was sent would no longer equal `walk(at)`. |
| A `pre_sample` hook that returns a context tip (sixth revision) | A second pointer beside `at`, and the reply landed under the tip while the cursor stayed elsewhere. Moving the cursor says the same thing with the one pointer core has. |

## Consequences

- I1 holds: every sample sends exactly `walk(at)`, and `FakeProvider`
  records it.
- The tree shows every compaction. Nothing happens invisibly (Q25).
- Phase 5 exit criterion: compaction needs no core change. If it does, stop
  and write an ADR (plan §3).
- Cost: copies duplicate bodies, and the summary is an ordinary `User` node
  authored by the extension (Q30).
- A copied batch should be copied with its `Results` node. A go-to onto a
  copied `Assistant` node with calls and a step runs the calls, as on any
  batch (model.md §5); the extension copies the `Results` too, so the cursor
  lands below it. A hook that crashes partway leaves an unfinished copy, a
  side node no cursor's walk passes through.
- Enforced by: I1's behavior test (`FakeProvider` records each request) and
  the scenario "compaction across a model change".
