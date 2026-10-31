# 02 Moves, branches, and what sits where

A move checks only that `to` is a node, and never looks below it. Every
commit is a write under `at` plus the move onto it, one record.

## Going back, rejecting a reply, branching

```
 reject a reply: go to its parent, then step
 k0 ─ s ─ u1 ─ a1           go to u1, Session.step       k0 ─ s ─ u1 ─ a1
                            the sample commits a1'                  └ a1' ◀ A.at

 a branch across a config change: go to b2
 k0 Config(big)
 └ u1
   ├ a1 ─ k1 Config(cheap) ─ u2      ◀ A.at before
   └ b1 ─ b2                         ◀ A.at after
 LCA(u2, b2) = u1;  config_at(b2) = big, nothing restated (D5)
 the go-to turned auto-step off: A runs pre_step, post_step, and waits
```

## Re-run, and two cursors at one batch

```
 u1
 └ a1 [c3, c5], stop Tools
   ├ r1  Results          the first run
   └ r1' Results          go to a1, then Session.step   ◀ A.at

 two cursors at a1, both stepped: each runs the batch and writes its own
 Results (Q13). "Run once" is an extension's, through the scratchpad.
```

A frontend that wants to look at a batch without re-running it goes to its
`Results` node, not to the `Assistant` node.

## Side nodes, detached roots, the scratchpad

```
 k0 Config(big)                               the first root
 └ s ─ u1 ─ a1 ◀ A.at
        ├ n1 Custom(note) by extension        side node: on no cursor's walk
        └ e1 Custom(sample_error) by core     a failed sample, appended without a move
 d0 Config(cheap)                             a detached root: one-off work
 └ d1 User "summarise…" ─ d2 Assistant       its Config records which model did it

 scratchpad (off the tree, never sent to the model)
   item 4  to A   Input { by: Frontend(laptop), text: "also check b.rs" }
   item 7  to B   Extension { name: timer, data: tick }
```

Any node may be gone to, side nodes and detached roots included. A sub-agent
opens cursor B on a detached root, `send`s it the task (an input authored by the sub-agent's
extension), and `await rest(B)`s for the ending of the step that caused. An item like 7 (a
timer tick) wakes B, and its `pre_step` acts on it with `cursor.write(…, delivers=[7])`.
