# Phase 1 diagrams

Hand-drawn, plain text. Rule 6: after Phase 3, diagrams are generated from
code and these files go. Where a diagram and `model.md` or the spike differ,
the spike wins, then `model.md`.

| file | shows |
|---|---|
| [01-step.md](01-step.md) | the step machine's positions and jobs, a hook's move before a commit, a batch on the driver side, abort, a crash mid-batch |
| [02-tree.md](02-tree.md) | going back, branching across a config change, re-run, two cursors at a batch, side nodes, detached roots, the scratchpad |
| [03-compaction.md](03-compaction.md) | compaction as a new line plus a `pre_sample` move |

Conventions: `k` Config, `s` System, `u` User, `a` Assistant, `r` Results,
`n`/`e` side nodes, `d` a detached root, `c3` a call id (a part, not a node),
`◀ A.at` cursor A's position, `a1'` a sibling or copy of `a1`.
