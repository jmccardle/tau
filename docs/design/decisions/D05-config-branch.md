# D5: Config is the nearest `Config` on the walk

Status: accepted (Phase 1); amended by D13: a `Config` node names
a model rather than holding a full snapshot.

## Context

A person goes to an old line written under another model, tool list, or hook
list. Which config applies? 0.15 kept settings in per-turn state, and the
answer depended on which object you asked.

## Decision

- A `Config` node holds the **model's name** (D13; it was a full snapshot). `config_at(n)` is the nearest
  `Config` on `walk(n)`; every root is `Config`, so it is always defined.
- A cursor's config is `config_at(at)`. Going to an old line uses that
  line's config; core restates nothing.
- Frontends show the change; an extension that wants the previous config
  appends a `Config` and moves onto it.
- A `Config` appended on purpose persists until replaced (Q26).

## Alternatives

| option | why not |
|---|---|
| Config follows the cursor (old I6) | A restatement rule with LCA ranges, deferred restatements at batches, and kinds of `Config`: core logic for a choice a frontend can show. |
| Partial config deltas | `config_at` becomes a fold. |
| Revert one-off choices automatically | State behind the tree's back. |

## Consequences

- One lookup, no rule.
- Cost: going to an old line runs with that line's model unless something
  says otherwise.
- Not yet spiked. Enforced by a unit test and the scenarios "branch across a
  config node" and "compaction across a model change".
