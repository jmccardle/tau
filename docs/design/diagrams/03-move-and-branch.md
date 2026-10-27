# 03 Move, branch, fast-forward, re-run

A move is the only way `at` changes, and each move writes one move record.
Whether it is a branch depends on where `to` sits relative to `at`: below
`at` is a move within a line; on `walk(at)` (an ancestor) is going back, not
a branch; anywhere else leaves the line at `LCA(at, to)` and is a branch, and
the old line stays committed. When the destination line carries a different
config, I6 restates the cursor's own config as a `Config` node under `to`
(or reuses one already there), and the cursor lands on that node. A move onto
a batch or into its run fast-forwards to the run's current end, and only
`Session.rerun` lands exactly where it was sent. A move does every check
first, then writes; it never takes a claim. A move of a busy cursor, other
than its step's own, interrupts that step first (`02-step.md`).

Notation: `◀ A.at` marks a cursor's `at`. All nodes shown are committed unless
marked.

## A move within a line

`to` is a descendant of `at`, so `LCA(at, to) == at`: the cursor goes deeper
on the line it is already on. A step's own move is always this shape (`by: core`).

```
 before                                    after  go to a3  (by frontend)
 k0 ─ u1 ─ a1 ◀ A.at ─ u2 ─ a3             k0 ─ u1 ─ a1 ─ u2 ─ a3 ◀ A.at

 LCA(a1, a3) = a1 = at          no branch
 move record appended: { cursor: A, from: a1, to: a3, by: frontend, time }
 a go-to never starts a step, even with auto-step on
```

## Going back, and rejecting a reply

Going to an ancestor (a node on `walk(at)`) is going back, not a branch: the
cursor is still on a prefix of its line. The branch forms when the next write
under that ancestor makes a sibling.

```
 reject a reply: go back to its parent, then step

 k0 ─ u1 ─ a1 ◀ A.at            go to u1: going back       k0 ─ u1 ─ a1       still committed (Q2)
                                step: the write a1' is            └ a1'  ◀ A.at   a sibling reply
                                the branch
```

## A branch with LCA and the I6 restatement

Line A changed the model with an explicit `Config`; line B, an older line the
cursor visited before, has the original config. A go-to onto `b2` would
silently switch the cursor back to model big, so the move restates A's config.

```
 before                                                     after  go to b2  (by frontend)
 k0 Config(model: big)                                      k0 Config(big)
 └ u1 User                                                  └ u1
   ├ a1 Assistant (end)                                       ├ a1 ─ k1 Config(cheap) ─ u2 User
   │  └ k1 Config(model: cheap)   explicit                    └ b1 ─ b2 ─ c  ◀ A.at
   │     └ u2 User  ◀ A.at                                              c = Config(cheap), by core,
   └ b1 Assistant (end)                                                     the restating node (I6)
     └ b2 User

 LCA(u2, b2) = u1
 config_at(u2) = cheap     config_at(b2) = big     no explicit Config in (u1, b2] = b1 b2
 move record: { cursor: A, from: u2, to: c, by: frontend }     (`to` is the restating node)
 next(walk(c)) skips Config, sees User b2: Sample (but a go-to starts nothing)
```

If `b2` were an unstarted batch, I4 forbids appending under it and the move
would be refused (Q16). If the B line held an explicit `Config` in
(`u1`, `b2`] (any `Config` except a restatement core wrote, copies included),
the cursor would take that line's config as chosen there, and nothing would
be restated. Going back to `u2` and then to `b2` again reuses `c`: no second
restatement is written.

## Fast-forward onto an existing run

Landing on a batch's `Assistant` node, or on one of its results, never leaves
the cursor above the end of a run by accident. The destination becomes the
end of the existing run: fast-forward follows only `Result` children
answering that batch's calls, takes the most recently appended where there
are several, stops at the result that completes the batch, and never passes
onto a later reply.

```
 u1 ─ a1 ─ r5 ─ r3 ─ a2 ...                 the batch of a1 is complete; a2 is a later reply
 B: go to a1  (or open_cursor at r5)        ──▶  B.at = r3     stops at r3, never on to a2
                                                               no step starts, nothing runs again

 the run is partial:   u1 ─ a1 ─ r5         c3 unanswered, A may be running it
   B: go to a1 ──▶ B.at = r5                B sits at the run's current end; no claim taken,
                                            never refused because of a claim
   B is stepped later ──▶ its step takes the claim (waits while A's step holds it),
                          re-reads the run, fast-forwards (by core), runs what is still unanswered

 a copied batch a1' (copied_from a1) that is not complete on the walk ──▶ the move is REFUSED:
 the driver never runs a copied call
```

## Re-run: Session.rerun

A re-run is a move that lands exactly where it was sent, followed at once by a
tools step for every call still unanswered on its new line. That step takes
the claim, waiting if another step holds it. It makes a sibling run, or a run
that branches partway.

```
 u1
 └ a1
   ├ r5
   │ ├ r3                       the first run (complete)
   │ └ r3'  ◀ rerun(r5)         keep c5's result, run c3 again (branches partway)
   └ r3'' ─ r5''  ◀ rerun(a1)   run both calls again (a sibling run)

 r3' and r3'' are new nodes (new ids) answering c3; r5'' answers c5 again.
 Each line answers each call once (I4):
   walk(r3)   = k0 s u1 a1 r5 r3          c5, c3
   walk(r3')  = k0 s u1 a1 r5 r3'         c5, c3'
   walk(r5'') = k0 s u1 a1 r3'' r5''      c3'', c5''
 afterwards a go-to a1 fast-forwards to the most recently appended chain: r5''
```

```
 rerun(cursor, node), in order: every check first, then the writes
   1  to = node, as given (no fast-forward)
   2  nothing unanswered on walk(node)?  refuse        (e.g. rerun(r3), the completing result)
      a copied batch?                    refuse
   3  no to == at stop: rerun(a1) while at == a1 runs the batch again
   4  I6: refuse if a restatement would be needed (node is an unstarted or partial batch)
   5  if the cursor is busy, interrupt its step first (its records are written, not moved onto)
      append move { cursor, from: at, to: node, by, time }; set at := node
   6  tools step now (takes the claim), for each call unanswered on walk(node), through pre_tool as usual
```

Reads with: `model.md` §3 (committed, LCA), §5 (move, steps 1 to 5, branching,
re-run), I3 (valid `at`, fast-forward), I4, I5, I6, Q2, Q10, Q15, Q16, Q28;
`glossary.md` terms move, move record, go to, branch, LCA, fast-forward,
re-run, claim, line.
