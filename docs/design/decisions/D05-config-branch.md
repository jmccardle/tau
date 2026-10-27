# D5: Config is the nearest `Config` on the walk, as full snapshots

Status: accepted, revised in review twice (Phase 1, 2026-10-27). The seventh
revision retired "config follows the cursor" (I6).

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
- **A cursor's config is `config_at(at)`.** The last `Config` node on the
  walk sets the cursor's model settings, extensions, and tools. Going to an
  old line uses that line's config. Core restates nothing.
- Frontends and extensions deal with the implications: a frontend may show
  the model a go-to switches to, and an extension that wants a line to keep
  the cursor's previous config appends a `Config` and moves onto it.
- A `Config` a hook or frontend appends on purpose persists until replaced.
  There is no automatic revert (Q26).

## Alternatives

| option | why not |
|---|---|
| Config follows the cursor (I6, revisions one to six) | Needed a restatement rule with an LCA range, "explicit" versus core-written `Config` nodes, reuse of restatements, and a deferred restatement at a batch read off the move log. All of it was core logic about expectations, for a choice a frontend can show and an extension can make. |
| Config follows the branch, with restatement (plan option 2, as the plan worded it) | The same restatement machinery, at a commit that no longer exists. |
| Partial config deltas | Reading config means folding nodes; `config_at` would no longer be one lookup. |
| Revert one-off choices automatically (Q26) | Behind-the-back state. The tree should show which model produced each reply. |

## Consequences

- One lookup, no rule: the walk says which config is in force.
- Cost: going to an old line silently runs with that line's model and tools
  unless a frontend or extension says otherwise. That is the trade the
  review chose.
- Enforced by: unit test (`config_at` is the nearest `Config`); behavior
  scenarios "branch across a config node" (the step uses the destination
  line's config) and "compaction across a model change".

## History

The plan's default was "config follows the cursor": moving never changes a
cursor's effective config implicitly, with a restating `Config` appended at
commit. Revisions one to six kept it as I6, applied at a move and at a
sample, with a deferred restatement for batches (Q16). The seventh revision
retired it. In review: "the last `Config` node in the walk would set the
cursor's model settings, and the frontend / extensions should come up with
ways to deal with the implications."
