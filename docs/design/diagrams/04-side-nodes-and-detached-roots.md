# 04 Side nodes, detached roots, and the scratchpad

A node is on the tree once it is written. There is no committed or
uncommitted set: nodes that no cursor's walk passes through are ordinary
nodes, and any of them may be gone to. This diagram is one session, after a
tools phase completed and the next sample failed with nothing arrived. It
holds two trees. Tree 1 has a cursor's line and several nodes off it: an
extension's side node, a reminder `pre_sample` appended, and a `sample_error`
record. Tree 2 is a **detached root**: a hook's one-off summariser call with
its own `Config` root. Beside the tree sits the **scratchpad**, which is not
part of it.

## The session

```
 TREE 1
 k0  Config   by core        (model: big)                       root
 └ s   System    by core      (Session.create)
   └ u1  User      by frontend(laptop)  "fix the bug"           delivers: [2]
     ├ n1  Custom    by extension(note)   {kind: note}          side node, appended by post_step
     └ a1  Assistant by provider  calls [c3, c5], stop tools
       └ r1  Results   c3: Ok, c5: Ok    by tools               written at the join
         └ u2  User      by frontend(laptop)  "and b.rs"        delivers: [7]
           └ n2  User      by extension(remind)  "reminder ..."  appended by pre_sample, which
             │                                                   moved A onto it  ◀ A.at
             └ e1  Custom    by core   {kind: sample_error, 429} side node under at; A did not move

 TREE 2   a detached root
 d    Config  by extension(compact)  (model: cheap)             root; records which model
 └ d1   User      by extension(compact)  "summarise s..u1"      did the work
   └ d2   Assistant by provider  "...summary..."

 SCRATCHPAD (not part of the tree; next item id: 10)
 item 8  { from: {frontend: phone, cursor: A}, to: A, data: {text: "thanks"} }         input
         pending: A's next step with auto-step on (a send) delivers it in its input phase
 item 9  { from: {extension: note, cursor: B}, to: A, data: {kind: "review-request"} }  extension's
         A's extensions read it in pre_step; adding it woke A
 items 2, 7         removed when the nodes that deliver them were written; `delivers` is the
                    only record they existed

 A's move records:   open ──▶ s,  s ──▶ u1,  u1 ──▶ a1,  a1 ──▶ r1,  r1 ──▶ u2,
                     u2 ──▶ n2 (by extension(remind))
```

The reminder: `pre_sample` appended `n2` under `u2` and moved A onto it, so the
sample sent `walk(n2)` (I1). The sample failed before any token, so nothing
was committed for the reply. The driver appended `e1` under `at` without
moving.

What the failed sample left behind: `next(walk(n2))` is still `Sample`, and
auto-step is off, so the failure does not repeat unattended. `post_step` ran
with `failed(429)`. If output had arrived, a reply with `stop: error` would
have been committed under `n2` and moved onto.

## What may sit where

Every transition is legal in the tree. Core checks only V1 (a root is a
`Config`) and V2 (a `Results` node names distinct calls). Making a walk legal
on the wire is the provider's conversion (I5, §10).

```
 u1 ─ n1   (Custom)       a side node anywhere; never reaches the model (Q3)
 a1 ─ u3   (User)         legal: input under a batch with no Results. The provider renders the
                          calls as text (partial_calls, Q27). Input pending at an unanswered
                          batch goes here: after a crash mid-batch, "the power went out" (D4)
 u3 ─ r9   (Results)      legal: a Results node a hook wrote under a User node. The provider turns
                          it into a User message
 u1 ─ u4 ─ u5             two User nodes in a row: two inputs, or two clients; the provider merges
                          or keeps them as its API needs
 d1 under d               a detached root's line takes appends like any other; its root is a Config (V1)
 an item for a cursor     in the scratchpad, never in the tree: it reaches no model and no walk
```

## Any node may be gone to

```
 before                                  after  go to n1  (by frontend(laptop))
 u1 ─ n1                                 u1 ─ n1  ◀ A.at       config_at(n1), whatever it is
   └ a1 ... ◀ A.at                         └ a1 ...            nothing restated (I6 retired)
                                         the go-to turns auto-step off and wakes A: pre_step,
                                         post_step, no phase

 a cursor opened on d2 (a sub-agent's line): its opening move lands there like any move
```

A detached root is authored by the extension whose hook created it: the
driver fills in `by` from the caller, as for any append. Only the session's
first root is `by: core`. A reminder is a `User` node by the extension (Q30).

Reads with: `model.md` §1 (stored objects, the scratchpad, `delivers`), §3
(derived views, the two-tree example), §4 I3, I5, §6 (where things are
committed), §7 (handles, between cursors), §8 (a sample that fails with
nothing arrived), §10 (expand `Results`, render calls as text), §11 (the
scratchpad file), Q3, Q5, Q27, Q30; `glossary.md` terms side node,
detached root, root, scratchpad, item, input, delivers.
