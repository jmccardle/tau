# 04 Scratch and detached roots

Scratch is not a kind of node and nothing marks it: it is every node that no
move has landed on or passed through. This diagram is one session, after a
tools step has completed and the next sample failed with nothing arrived. It
holds two trees. Tree 1 has a committed line and three scratch nodes: an
extension's side node, a node `pre_sample` built, and a `sample_error`
record. Tree 2 is a **detached root**: a hook's one-off summarizer call with
its own `Config` root. Committed is read off the cursor's move records.

## The session

```
 TREE 1
 k0  Config   by core        (model: big)                       committed  (root)
 └ s   System    by core      (Session.create)                  committed
   └ u1  User      by frontend  "fix the bug"                   committed
     ├ n1  Custom    by extension(note)   {kind: note}          SCRATCH  side node off the line,
     │                                                          appended by post_input(u1)
     └ a1  Assistant by provider  calls [c3, c5], stop tools    committed
       └ r5  Result    c5: Ok                                   committed
         └ r3  Result    c3: Ok                                 committed  ◀ A.at
           └ n2  User      by extension(remind)  "reminder ..." SCRATCH  built by pre_sample(r3); User per Q30
             └ e1  Custom    by core   {kind: sample_error, 429} SCRATCH  the sample failed before any
                                                                           token arrived; context tip was n2

 TREE 2   a detached root
 d    Config  by extension(compact)  (model: cheap)             SCRATCH  root; records which model did the work
 └ d1   User      by extension(compact)  "summarise s..u1"      SCRATCH
   └ d2   Assistant by provider  "...summary..."                SCRATCH

 A's move records:   open ──▶ s,  s ──▶ u1,  u1 ──▶ a1,  a1 ──▶ r5,  r5 ──▶ r3
 committed  = every node on walk(to) for some move = k0 s u1 a1 r5 r3
 scratch    = everything else                        = n1 n2 e1 d d1 d2
```

What the failed sample left behind: `A.at` is still `r3`, `next` is still
`Sample`, and auto-step is off, so the failure does not repeat unattended.
Nothing was written for the reply because no output arrived. If it had, a
reply with `stop: error` would have been written and moved onto.

## What may sit where

```
 u1 ─ n1                 allowed: a Custom note under a node that is not an unstarted or partial batch
 a1 ─ n3  (Custom)       REFUSED while a1's batch is unstarted or partial: only a Result answering a
                         still-unanswered call may be appended there (I4). An audit trail for a rewritten
                         call goes on a detached root instead (Q24).
 d1 under d             a detached root's line takes appends like any other; its root is a Config (V2)
```

## A later move can make scratch committed

```
 before                                  after  go to n1  (by frontend, Q2)
 u1 ─ n1   scratch                       u1 ─ n1  ◀ A.at   committed: n1 is on walk(n1)
   └ a1 ... committed                      └ a1 ... still committed
                                         I6: if config_at(n1) differs from config_at(r3), core appends a
                                         restating Config under n1 and A lands on that node
```

Committed is permanent (Q2). Scratch nodes are persisted and stay inspectable;
they simply are not on anyone's conversation until a move reaches them. Core
never writes a node and then abandons it, so every scratch node is either an
extension's work or a record of a failure.

A detached root is authored by the extension whose hook created it: the
driver fills in `by` from the caller, as for any append. Only the session's
first root is `by: core`. A reminder is a `User` node by the extension (Q30).

Reads with: `model.md` §3 (derived views, scratch, the two-tree example), §7
(post hooks append side nodes; hooks with `api`), §8 (a sample that fails with
nothing arrived), I4, Q2, Q3, Q5, Q24, Q30; `glossary.md` terms scratch, side
node, detached root, committed, root, tip.
