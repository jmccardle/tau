# D5: Config follows the cursor, as full snapshots

Status: accepted (Phase 1, 2026-10-26)

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
  - If `to` is an unstarted batch, I4 forbids appending under it, so the
    move is refused (Q16).
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
- Cost: refused go-to onto an unstarted or partial batch on another line
  (Q16). Rare; no other placement keeps both I4 and I6.
- Enforced by: I6 proptest (`config_at(at)` changes only across a move whose
  (LCA, to] contains an explicit `Config`, or which appends or reuses a
  restatement), the reuse unit test, and scenarios "branch across a config
  node" and "compaction across a model change".

## Note on the plan

The plan's wording said "commit appends the restating `Config`". There is no
commit now, so I6 applies at a move and at a sample (model.md §4). The default
(follows the cursor, full snapshots) is unchanged.
