# D5: Config follows the cursor, as full snapshots

Status: accepted, revised in review (Phase 1, 2026-10-27)

## Context

A person branches back to an old line. The old line was written under a
different model, provider, tool list, or hook list. Which config applies to the
next sample?

0.15 kept settings in per-turn state and fixed key lists, and the answer
depended on which object a reader asked (glossary, `config`).

## Decision

- Config is a `Config` node holding a **full snapshot**. `config_at(n)` is the
  body of the nearest `Config` node on `walk(n)`. Partial updates are never
  folded together. Every root is a `Config` node, so it is always defined
  (Q5).
- **Config follows the cursor** (I6): moving never changes a cursor's
  effective config implicitly.
  - Both places use one reading: restate only when no node in the range
    **(LCA, to]** (below the LCA, excluding it, including the destination;
    all of `walk(to)` with no LCA) is an **explicit** `Config`, meaning any
    `Config` except a restatement core wrote. A copied `Config` is explicit.
  - A restatement is reused when the node already has a core-written
    restating child with the same body, so going back and forth adds at most
    one per place.
  - At a sample, if a `pre_sample` tip is on another line with different
    config and no explicit `Config` in (LCA, tip], the driver restates
    `Config(config_at(at))` under the tip (`by: core`), and the reply is
    appended under that `Config`.
  - At a go-to, a hook's move, or a re-run, if `config_at(to) !=
    config_at(at)` and no explicit `Config` lies in (LCA(at, to), to], the
    move restates the cursor's config under `to` and lands on it.
    `next` looks past it (model.md §6).
  - If `to` is an unstarted or partial batch, I4 forbids appending under it.
    The restatement is **deferred**: the move lands on the batch, the run
    uses `config_at` of its own walk, and when a step's own move lands the
    cursor on the result that completes the batch, the restating `Config` is
    written under it and moved onto, in the same durable write (Q16). What
    is owed is read off the move log, so it survives a reload.
- A `Config` a hook or frontend appends on purpose is explicit and persists
  until replaced. There is no automatic revert (Q26).

## Alternatives

| option | why not |
|---|---|
| Config follows the branch (plan option 2) | Branching to an old line silently switches model, provider, or tools to what was in force back then. |
| Partial config deltas | Reading config means folding nodes; `config_at` would no longer be one lookup. |
| Revert one-off choices automatically (Q26) | Behind-the-back state. The tree should show which model produced each reply. |

## Consequences

- Every reply's config is readable: `config_at(parent)` (I1 test).
- A go-to across a config change may write one restating `Config` node
  (or reuse one already there). Browsing still runs nothing: no step, no
  hook, no provider call.
- Cost: a batch reached on another line runs with that line's config (its
  tools and hooks), and the cursor's own config returns only after the
  batch completes (Q16). The fourth revision refused such moves instead; a
  refusal for a choice that had an obvious answer was worse.
- Enforced by: I6 proptest (`config_at(at)` changes only across a move whose
  (LCA, to] contains an explicit `Config`, which appends or reuses a
  restatement, or which lands in an unstarted or partial batch whose
  completing result then carries the restatement), the reuse unit test,
  the scenario "go-to an unstarted batch on a line with another config", and scenarios "branch across a config
  node" and "compaction across a model change".

## Note on the plan

The plan's wording said "commit appends the restating `Config`". There is no
commit now, so I6 applies at a move and at a sample (model.md §4). The default
(follows the cursor, full snapshots) is unchanged.
