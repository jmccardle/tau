# Phase 1 diagrams

Hand-drawn, plain text, and consistent with `../model.md` and `../glossary.md`
(sixth revision). Rule 6: hand-drawn diagrams exist only in Phase 1; after
Phase 3 diagrams are generated from code and these files are replaced.
`model.md` and `glossary.md` win wherever a diagram and they differ.

| file | shows |
|---|---|
| [01-nodes.md](01-nodes.md) | the node, the six body kinds, `Assistant` parts and `stop`, which kinds reach the model, the move record, the scratchpad item |
| [02-step.md](02-step.md) | the cycle and its wakes; the two step kinds: pre hooks, effect, pre-write hooks, write and move, post hooks, `post_step`; input, the scratchpad, and delivery; what a hook may do and where (boundaries); a frontend moving a busy cursor; go to vs re-run vs a hook's move |
| [03-move-and-branch.md](03-move-and-branch.md) | a move within a line, a branch with LCA and the I6 restatement, fast-forward, `Session.rerun` |
| [04-uncommitted-and-detached-roots.md](04-uncommitted-and-detached-roots.md) | side nodes, a detached root, a `sample_error`, committed vs uncommitted, the scratchpad beside the tree |
| [05-compaction.md](05-compaction.md) | compaction as branch-from-prefix, with `copied_from`, `derived_from`, and I6 at a sample |
| [06-crash-and-resume.md](06-crash-and-resume.md) | crash mid tools run, reload, the wake on load and a pending input, claim, resume; abort during a sample and during tools; a frontend's go-to during tools |

## Reading conventions

```
 k0      a Config root          s  System      u1  User      a1  Assistant
 r5      a Result (answers c5)  c3 a call id (a part of its Assistant node, not a node)
 n1 d    a side node, a detached root
 ◀ A.at  the `at` of cursor A
 a1'     a new node (its own id) that repeats the role of a1, for example a second run
 ─ └ ├   parent to child; a node sits under its parent
 committed / uncommitted   every node is one or the other, derived from the moves (never stored)
 item 4  a scratchpad item (off the tree)
 ▶  ──▶  an arrow of time or of a move
```

- Labels stand for session-local integer ids (Q8). `by` is one of `frontend(client)`,
  `provider`, `tools`, `core`, `extension(name)`.
- ASCII is primary. A Mermaid sequence diagram appears only in `02-step.md`.
- Where `model.md` leaves a detail open, the file says **OPEN** (or "Open
  points") and names the question; the diagram draws only what is decided.
