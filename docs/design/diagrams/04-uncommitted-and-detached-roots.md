# 04 Uncommitted nodes, detached roots, and the scratchpad

Uncommitted is not a kind of node and nothing marks it: it is every node that
no move has landed on or passed through. This diagram is one session, after a
tools step has completed and the next sample failed with nothing arrived. It
holds two trees. Tree 1 has a committed line and several uncommitted nodes:
an extension's side node, a node `pre_sample` built, and a `sample_error`
record. Tree 2 is a **detached root**: a hook's one-off summarizer call with
its own `Config` root. Committed is read off the cursor's move records. Beside
the tree sits the **scratchpad**, which is not part of it.

## The session

```
 TREE 1
 k0  Config   by core        (model: big)                       committed  (root)
 └ s   System    by core      (Session.create)                  committed
   └ u1  User      by frontend(laptop)  "fix the bug"           committed
     ├ n1  Custom    by extension(note)   {kind: note}          uncommitted  side node off the line,
     │                                                                       appended by post_step
     └ a1  Assistant by provider  calls [c3, c5], stop tools    committed
       └ r5  Result    c5: Ok                                   committed
         └ r3  Result    c3: Ok                                 committed
           └ u2  User      by frontend(laptop)  "and b.rs"      committed  ◀ A.at
             │             delivers: [7]                                   delivered by pre_step
             └ n2  User      by extension(remind)  "reminder ..." uncommitted  built by pre_sample(u2);
               │                                                              User per Q30
               └ e1  Custom    by core   {kind: sample_error, 429} uncommitted  the sample failed before
                                                                               any token; tip was n2

 TREE 2   a detached root
 d    Config  by extension(compact)  (model: cheap)             uncommitted  root; records which model
 └ d1   User      by extension(compact)  "summarise s..u1"      uncommitted  did the work
   └ d2   Assistant by provider  "...summary..."                uncommitted

 SCRATCHPAD (not part of the tree; next item id: 9)
 item 8  { from: {frontend: phone, extension: base, cursor: A}, to: A, data: {text: "thanks"} }
         pending: A's next pre_step that can take a User node delivers it
 item 7  removed when u2 was written; u2's `delivers` is the only record it existed

 A's move records:   open ──▶ s,  s ──▶ u1,  u1 ──▶ a1,  a1 ──▶ r5,  r5 ──▶ r3,  r3 ──▶ u2
 committed   = every node on walk(to) for some move = k0 s u1 a1 r5 r3 u2
 uncommitted = everything else                      = n1 n2 e1 d d1 d2
```

What the failed sample left behind: `A.at` is still `u2`, `next` is still
`Sample`, and auto-step is off, so the failure does not repeat unattended.
`post_step` ran with ending `failed`. Nothing was written for the reply
because no output arrived. If it had, a reply with `stop: error` would have
been written and moved onto.

## What may sit where

```
 u1 ─ n1                 allowed: a Custom note under a node that is not an unstarted or partial batch
 a1 ─ n3  (Custom)       REFUSED while a1's batch is unstarted or partial: only a Result answering a
                         still-unanswered call may be appended there (I4). An audit trail for a rewritten
                         call goes on a detached root instead (Q24).
 d1 under d              a detached root's line takes appends like any other; its root is a Config (V2)
 an item for a cursor    in the scratchpad, never in the tree: it reaches no model and no walk
```

## A later move can make an uncommitted node committed

```
 before                                  after  go to n1  (by frontend(laptop), Q2)
 u1 ─ n1   uncommitted                   u1 ─ n1  ◀ A.at   committed: n1 is on walk(n1)
   └ a1 ... committed                      └ a1 ... still committed
                                         I6: if config_at(n1) differs from config_at(u2), core appends a
                                         restating Config under n1 and A lands on that node
                                         the go-to turns auto-step off and wakes A: pre_step runs at n1

 a cursor opened on d2 (a sub-agent's line, Q35): the opening move commits d d1 d2
```

Committed is permanent (Q2). Uncommitted nodes are persisted and stay
inspectable; they simply are not on anyone's conversation until a move reaches
them. Core never writes a node and then abandons it, except an unreached I6
restatement, so every uncommitted node is either an extension's work or a
record of a failure. An aborted step's records are not uncommitted: the step
moves onto them before anything else moves the cursor
(`06-crash-and-resume.md`).

A detached root is authored by the extension whose hook created it: the
driver fills in `by` from the caller, as for any append. Only the session's
first root is `by: core`. A reminder is a `User` node by the extension (Q30).

Reads with: `model.md` §1 (stored objects, the scratchpad, `delivers`), §3
(derived views, uncommitted, the two-tree example), §7 (hooks append side
nodes through their cursor handle; between cursors; the base extension), §8
(a sample that fails with nothing arrived), §10 (the scratchpad file), I4,
I7, Q2, Q3, Q5, Q24, Q30, Q35; `glossary.md` terms uncommitted, side node,
detached root, committed, root, tip, scratchpad, item, pending input,
delivery, delivers.
