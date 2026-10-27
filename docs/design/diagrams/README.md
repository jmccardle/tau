# Phase 1 diagrams

Hand-drawn, plain text, and consistent with `../model.md` and `../glossary.md`
(seventh revision). Rule 6: hand-drawn diagrams exist only in Phase 1; after
Phase 3 diagrams are generated from code and these files are replaced.
`model.md` and `glossary.md` win wherever a diagram and they differ.

| file | shows |
|---|---|
| [01-nodes.md](01-nodes.md) | the node, the six body kinds, `Assistant` parts and `stop`, the `Results` body, which kinds reach the model, the move record, the label record, the two kinds of scratchpad item |
| [02-step.md](02-step.md) | the step loop and its wakes; the three phases (input, sample, tools) and their shared shape; endings; the two hook shapes and the `None`/string return; outcomes held until the join; hooks moving the cursor before a commit; a frontend moving a busy cursor; go-to vs `Session.step` vs a hook's move |
| [03-move-and-branch.md](03-move-and-branch.md) | a move within a line, going back, a branch with LCA across a config change, re-run as go-to plus step, two cursors at one batch, a go-to on a busy cursor |
| [04-side-nodes-and-detached-roots.md](04-side-nodes-and-detached-roots.md) | side nodes, a detached root, a `sample_error`, what may sit where, the scratchpad beside the tree |
| [05-compaction.md](05-compaction.md) | compaction as branch-from-prefix, with `copied_from`, `derived_from`, the hook's move, and config across lines |
| [06-crash-and-resume.md](06-crash-and-resume.md) | a crash mid tools phase and reload: the cursor rests at the unanswered batch, input goes under it, a step re-runs it; a pending input across a crash; abort during a sample, during tools, and during an approval; a frontend's go-to during tools |

## Reading conventions

```
 k0      a Config root          s  System      u1  User      a1  Assistant
 r1      a Results node         c3 a call id (a part of its Assistant node, not a node)
 n1 d    a side node, a detached root
 ◀ A.at  the `at` of cursor A
 a1'     a new node (its own id) that repeats the role of a1, for example a sibling reply
 ─ └ ├   parent to child; a node sits under its parent
 item 4  a scratchpad item (off the tree): input, or an extension's
 ▶  ──▶  an arrow of time or of a move
```

- Labels stand for session-local integer ids (Q8). `by` is one of
  `frontend(client)`, `provider`, `tools`, `core`, `extension(name)`.
- ASCII is primary. A Mermaid sequence diagram appears only in `02-step.md`.
- Where `model.md` leaves a detail open, the file says **OPEN** (or "Open
  points") and names the question; the diagram draws only what is decided.
