# 06 Crash, reload, resume; abort

Nothing is state unless it is a node or a move record, so a reload is a replay
(I7). A crash in the middle of a tools run leaves the cursor at the last result
it moved onto; the in-memory claim, the auto-step flag and stream deltas are
gone, and every cursor loads with auto-step off. When the frontend next steps
the cursor, the driver resumes the batch: it answers each unanswered call
either by running it again (the tool is idempotent) or with `Interrupted`
(D4). Finished calls never run again. The second half shows abort during a
sample, with and without output.

## Crash mid tools run

```
 before the crash                              what is on disk
 ──────────────────────────────────           ───────────────────────────────────────────
 k0 ─ s ─ u1 ─ a1 [c3, c5, stop tools]         nodes: k0 s u1 a1 r5
                    └ r5  Ok (c5)  ◀ A.at      moves: ... u1 ──▶ a1,  a1 ──▶ r5     (by core)
 c3 still running                              not on disk: the claim, auto_step, the c3 outcome
 ──────────────────── process dies ────────────────────

 a torn final record (a write cut off, say r3 mid-append) is ignored on load:
 the state is exactly the one before that write began. Not repair, no guessing.
```

## Reload

```
 load = replay nodes and moves (I7)
   A.at = r5            auto-step off            no claim held
   committed: k0 s u1 a1 r5
   next(walk(r5)) = RunTools(c3)       nearest message-bearing node is a result, batch partial
                                       (a cursor's `at` may be the end of a partial run; never
                                        a node above the end of a run, except where rerun put it, I3)
 nothing runs yet: a reload starts no step
```

## Claim, then resume

```
 frontend steps A (or switches auto-step on; or gives input, which answers the batch first, Q17)

 0  the step takes the batch's claim on a1 (a move never takes one)
      another step holds it ──▶ that step is running the batch: wait for it to finish
 1  re-read the run: are there results of this batch below `at`?
      some ──▶ fast-forward onto the end of the run (the step's own move, by core)
      none below r5 ──▶ stay (c5 is answered; it does not run again)
 2  for each call unanswered on walk(r5):   c3
```

```
 c3 unanswered on walk(r5)
        │
        ├─ tool is idempotent ──▶ pre_tool(c3) ─▶ run c3 ─▶ pre_result ─▶ r3 Result Ok(...)   by tools
        │                          runs again like any call; post_result(r3) follows
        │
        └─ otherwise ───────────▶ r3 Result Interrupted                                         by core
                                   the tool may or may not have run (D4)

 k0 ─ s ─ u1 ─ a1 ─ r5 ─ r3 ◀ A.at         r3 appended under r5, A moved onto it (by core)
 post_result(r3) runs if r3 came from the tool; an Interrupted r3 is core-written and runs no hooks
 next(walk(r3)) = Sample                    batch complete
```

Why a result per call: a ten-minute `bash` that finished before the crash keeps
its `r5`; it is not marked `Interrupted` because its sibling was still running.

A crash during a sample is simpler: no reply was written, so on reload `next`
is `Sample` and a step samples again. A crash between a step's write and its
move cannot happen: the two are durable together (§10), so a crash leaves both
or neither.

## Abort during a sample

```
 A.at = u1       next = Sample       pre_sample appended n1 (a reminder) under u1 and returned it as the tip

 abort ──▶ cancel the request
   │
   ├─ no parts had arrived
   │      nothing written                   u1 ◀ A.at       (unchanged)      next = Sample
   │      n1 stays, SCRATCH                 └ n1                              auto-step off
   │      no post hooks
   │
   └─ some parts had arrived                pre_reply still runs (redaction applies)
          a2 Assistant by provider, parent n1       parts: [ Text "Let me ", Call{c9, write_file, '{"path":"a'} ]
                                                    stop: interrupted        (stop_raw absent: we cancelled)
          move A: u1 ──▶ a2   by core               commits u1 n1 a2
          no post_reply hooks, auto-step off
          next(walk(a2)) = Wait    the call c9 is not runnable and is owed no result (§2)
          later requests: the provider renders c9 as text per config.partial_calls (Q27)
```

The tokens the user saw are stored, and the model sees them next time.

## Abort during tools (the same family)

```
 a1 [c3, c5]        r5 Ok already written         c3 still running
 abort ──▶ cancel c3 and its hooks
   r5 stays;  c3 gets   r3 Result Interrupted  by core, appended under r5, A moved onto it
   (also true of a call whose tool finished while its pre_result was running: its raw outcome is never written)
   no post hooks run, auto-step off     next(walk(r3)) = Sample
```

Results core writes (`Interrupted`, `Err("invalid arguments")`,
`Err("hook error")`, `Skipped("superseded by input")`) run no hooks: there is
no tool output to rewrite (`model.md` §7).

Reads with: `model.md` §3 (the mid-step example), §8 (abort, failure, reload,
resume at a batch), §10 (durability, claim), I3, I4, I7, Q4, Q13, Q17, Q27, D4;
`glossary.md` terms resume, idempotent, claim, abort, outcome, auto-step.
