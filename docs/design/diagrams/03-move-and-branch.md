# 03 Move, branch, re-run

A move is the only way `at` changes, and each move writes one move record. A
move checks only that `to` is a node of the session (I3); it never looks at
what is below `to`. Whether it is a branch depends on where `to` sits
relative to `at`: below `at` is a move within a line; on `walk(at)` (an
ancestor) is going back, not a branch; anywhere else leaves the line at
`LCA(at, to)` and is a branch. The cursor's config is always
`config_at(at)`, whatever line it lands on. A go-to turns auto-step off and
wakes the cursor, so its step runs only `pre_step` and `post_step` (Q39). A
frontend's move of a busy cursor aborts its step first, and is refused while
a hook of it runs or a question on it is open; a cursor hook moves only its
own cursor (`02-step.md`).

Notation: `◀ A.at` marks a cursor's `at`.

## A move within a line

`to` is a descendant of `at`, so `LCA(at, to) == at`: the cursor goes deeper
on the line it is already on. A commit's move is always this shape
(`by: core`).

```
 before                                    after  go to a3  (by frontend(laptop))
 k0 ─ u1 ─ a1 ◀ A.at ─ u2 ─ a3             k0 ─ u1 ─ a1 ─ u2 ─ a3 ◀ A.at

 LCA(a1, a3) = a1 = at          no branch
 move record appended: { cursor: A, from: a1, to: a3, by: frontend(laptop), time }
 the go-to turns auto-step off and wakes A: pre_step, no phase, post_step(done);
 a phase runs only if an extension's pre_step turns auto-step on
```

## Going back, and rejecting a reply

Going to an ancestor (a node on `walk(at)`) is going back, not a branch: the
cursor is still on a prefix of its line. The branch forms when the next
commit under that ancestor makes a sibling.

```
 reject a reply: go back to its parent, then step

 k0 ─ u1 ─ a1 ◀ A.at            go to u1: going back       k0 ─ u1 ─ a1       still on the tree
                                Session.step: the sample          └ a1'  ◀ A.at   a sibling reply
                                phase commits a1', the branch
```

## A branch with LCA, across a config change

Line A changed the model with an explicit `Config`; line B, an older line,
has the original config. A go-to onto `b2` puts the cursor on B's line, and
its config is now B's. Nothing is restated: I6 is retired (D5).

```
 before                                                     after  go to b2  (by frontend(laptop))
 k0 Config(model: big)                                      k0 Config(big)
 └ u1 User                                                  └ u1
   ├ a1 Assistant (end)                                       ├ a1 ─ k1 Config(cheap) ─ u2 User
   │  └ k1 Config(model: cheap)                               └ b1 ─ b2  ◀ A.at
   │     └ u2 User  ◀ A.at
   └ b1 Assistant (end)
     └ b2 User

 LCA(u2, b2) = u1
 config_at(u2) = cheap   ──▶   config_at(b2) = big       the cursor now runs with big
 move record: { cursor: A, from: u2, to: b2, by: frontend(laptop) }
 next(walk(b2)) = Sample; auto-step is off after the go-to, so A waits
 a frontend or extension that wants cheap here appends a Config under b2 and moves onto it
```

## Re-run: go to the batch, then step

The cursor never looks at children. Going to an `Assistant` node whose batch
already has a `Results` child gives `next == RunTools` again, and a step runs
the batch and commits a sibling `Results`. There is no `Session.rerun`, no
fast-forward, and no claim.

```
 u1
 └ a1 Assistant [c3, c5], stop tools
   ├ r1 Results  c3: Ok, c5: Ok        the first run
   │ └ a2 ...                          the line continued
   └ r1' Results c3: Ok, c5: Err       go to a1, then Session.step: a sibling Results  ◀ A.at

 walk(r1)  = k0 s u1 a1 r1      walk(r1') = k0 s u1 a1 r1'      each Results answers each call once (I4)
 a frontend that does not want a re-run goes to r1, not a1
 every call of the re-run passes through pre_tool as usual: a hook may still refuse one
```

Two cursors at one batch each run it, and each commits its own `Results`: a
batch's outcomes are held in each cursor's own step (Q13). An extension that wants "run once" coordinates
through the scratchpad.

```
 u1 ─ a1 [c3]  ◀ A.at  ◀ B.at       both stepped
      ├ r1  Results by A's step
      └ r1' Results by B's step
```

## A go-to on a busy cursor

```
 A.at = a1 [c3, c5], tools phase running, c5 finished (outcome held in memory), c3 running
 client laptop: go to u1

 refused if a hook of A is running, or a question on A is open: nothing queued
 otherwise:
   1 abort: auto-step off; cancel c3 (its tool, never a hook)
   2 c3: Interrupted; the join commits r1 Results {c3: Interrupted, c5: Ok} under a1;
     A moves onto r1 (by core)
   3 post_result(r1), post_step(aborted): hooks may even move A
   4 the move: A.at = u1, by frontend(laptop)                         the frontend's move wins
   5 the go-to wakes A at u1: pre_step, post_step, no phase (auto-step off)

 u1 ◀ A.at
 └ a1 ─ r1        the abort is on the tree; a later go-to a1 and step would run both calls again
```

## What a move checks, in order

```
 1  is `to` a node of the session?                     no ──▶ refused (I3)
 2  a frontend's move: a hook of the cursor running,
    or a question on it open?                          yes ──▶ refused, nothing queued
    a hook's move: of its own cursor, from a cursor hook?  no ──▶ refused
 3  to == at?                                          yes ──▶ nothing written
 4  a frontend's move of a busy cursor: abort the step, let it end (commit, post hooks,
    post_step), then move
 5  write the move { cursor, from: at, to, by, time }; at := to; emit cursor_moved
 6  a go-to: auto-step off, wake the cursor
```

A refused move changes nothing and raises to whoever asked for it.

Reads with: `model.md` §3 (LCA), §5 (move, branching, re-run, a frontend's
move of a busy cursor), §6 (next, waking), §8 (abort), I3, I4, I6 (retired),
Q10, Q13, Q15, Q28, Q39; `glossary.md` terms move, move record, go to,
branch, LCA, re-run, line.
