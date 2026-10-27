# 06 Crash, reload, resume; abort

Nothing is state unless it is a node, a move record, or a scratchpad item, so
a reload is a replay (I7). A crash in the middle of a tools run leaves the
cursor at the last result it moved onto; the in-memory claim, the auto-step
flag, open questions and stream deltas are gone, and every cursor loads with
auto-step off. When the frontend next steps the cursor, the driver resumes
the batch: it answers each unanswered call either by running it again (the
tool is idempotent) or with `Interrupted` (D4). Finished calls never run
again. A pending input survives the crash as a scratchpad item, and the
`pre_step` that runs when the cursor wakes on load delivers it. The second
half shows abort: during a sample, with and without output, during tools,
and a frontend's go-to on a busy cursor.

## Crash mid tools run

```
 before the crash                              what is on disk
 ──────────────────────────────────           ───────────────────────────────────────────
 k0 ─ s ─ u1 ─ a1 [c3, c5, stop tools]         record log: k0 s u1 a1 r5, with their moves
                    └ r5  Ok (c5)  ◀ A.at                  (u1 ──▶ a1, a1 ──▶ r5, by core)
 c3 still running                              scratchpad file: item 4 (input for A)
                                               not on disk: the claim, auto_step, the c3 outcome
 ──────────────────── process dies ────────────────────

 a torn final record (a write cut off, say r3 mid-append) is detected by its length and
 checksum and ignored on load: the state is exactly the one before that write began.
 Not repair, no guessing.
```

## Reload

```
 load = replay the record log, then read the scratchpad and drop delivered items (I7)
   A.at = r5            auto-step off            no claim held            no open questions
   committed: k0 s u1 a1 r5
   next(walk(r5)) = RunTools(c3)       nearest message-bearing node is a result, batch partial
                                       (a cursor's `at` may be the end of a partial run; never
                                        a node above the end of a run, except where rerun put it, I3)
 every cursor loads at rest; then, in the lease holder only, every cursor wakes:
 pre_step(A) runs (a read-only process never runs hooks); no step starts, auto-step is off
```

## A pending input across the crash

```
 before the crash: A busy running c3; client laptop typed "also b.rs"
   scratchpad: item 4 { from: {frontend: laptop, extension: base, cursor: A}, to: A,
                        data: {text: "also b.rs"} }

 after reload, A wakes, pre_step(A), the base extension:
   item 4 is open, and no node delivers it
   at = r5, a partial batch: no User node may go there (I4)  ──▶ deliver nothing
 a send (or the frontend steps A): the batch completes (below); the pre_step before
 the next sample delivers:
   cursor.deliver([4])    one record: u2 User "also b.rs" by frontend(laptop), delivers [4],
                          and the move r3 ──▶ u2; then item 4 is removed from the file

 crash before that record:          item 4 is still open on the next load, and is delivered then
 crash after it, before removal:    load drops item 4 because u2 delivers it: never twice
 if at could take a User node, the pre_step on load would deliver at once; A then waits
 at u2, auto-step off, for the frontend to step or resume it
```

## Claim, then resume

```
 frontend steps A (or a send turns auto-step on, Q17)

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
 post_result(r3) runs either way; an Interrupted r3 is core-written and skips only pre_result
 post_step(wrote r3);  next(walk(r3)) = Sample        batch complete
```

Why a result per call: a ten-minute `bash` that finished before the crash keeps
its `r5`; it is not marked `Interrupted` because its sibling was still running.

A crash during a sample is simpler: no reply was written, so on reload `next`
is `Sample` and a step samples again. A crash between a step's write and its
move cannot happen: the two are one record (§10), so a crash leaves both or
neither.

## Abort during a sample

Abort (esc) turns auto-step off and cancels the effect in flight. It never
cancels a hook. The step's own move lands on what was written, and hooks run
on it as usual.

```
 A.at = u1       next = Sample       pre_sample appended n1 (a reminder) under u1 and returned it as the tip

 abort ──▶ auto-step off; cancel the request
   │
   ├─ no parts had arrived
   │      nothing written                   u1 ◀ A.at       (unchanged)      next = Sample
   │      n1 stays, uncommitted             └ n1
   │      post_step(aborted)
   │
   └─ some parts had arrived                pre_reply runs (redaction applies)
          a2 Assistant by provider, parent n1       parts: [ Text "Let me ", Call{c9, write_file, '{"path":"a'} ]
                                                    stop: interrupted        (stop_raw absent: we cancelled)
          move A: u1 ──▶ a2   by core               commits u1 n1 a2
          post_reply(a2), then post_step(aborted, a2)
          next(walk(a2)) = Wait    the call c9 is not runnable and is owed no result (§2)
          later requests: the provider renders c9 as text per config.partial_calls (Q27)

 abort while pre_sample runs: the hook finishes; the sample never starts; post_step(aborted)
 then A rests: no pre_step after an abort; the base extension's post_step has returned
 any pending input to its client
```

The tokens the user saw are stored, and the model sees them next time.

## Abort during tools

```
 a1 [c3, c5, c6]   r5 Ok already written   c3 running   c6's pre_tool is asking a person
 abort ──▶ auto-step off; cancel c3 (its tool, not any hook)
   r5 stays;  c3 gets   r3 Result Interrupted  by core, appended under r5, A moved onto it,
                        post_result(r3) runs
   c6: its question stays open; A stays locked
       declined ──▶ pre_tool answers Skipped("permission not granted"): r6 written, post_result(r6)
       approved ──▶ pre_tool returns the call, but no new effect starts: r6 Interrupted (Q34)
   a call whose pre_result was running finishes it, and its outcome is written redacted
   post_step(aborted, r6);  next(walk(r6)) = Sample;  A rests
```

## A frontend's go-to during tools

```
 a1 [c3, c5]        r5 Ok already written, A.at = r5         c3 running, no hook running
 client laptop goes to u1        (refused instead while a hook of A runs, or a question is open)

 1 abort: auto-step off; cancel c3
 2 r3 Result Interrupted, by core, under r5          the run is complete on walk(r3)
 3 the step's own move r5 ──▶ r3, as usual           A.at = r3, committed
 4 post_result(r3), post_step(aborted, r3); the claim is released
 5 resolve to = u1; write the move  r3 ──▶ u1        A.at = u1; by frontend(laptop)
 6 the go-to wakes A: pre_step at u1

 u1 ◀ A.at
 └ a1 ─ r5 ─ r3        all committed: the abort is on the line, then A went back
 A later go-to a1 fast-forwards to r3: nothing runs twice.
```

Results core writes (`Interrupted`, `Err("invalid arguments")`,
`Err("hook error")`) skip `pre_result`: there is no tool output to rewrite.
Their `post_result` hooks run, aborted steps included (`model.md` §7, §8).

Reads with: `model.md` §3 (the mid-step example), §5 (a frontend's move of a
busy cursor), §7 (the cycle, the base extension, questions), §8 (abort,
failure, reload, resume at a batch), §10 (records, the scratchpad file,
delivery order, claim), I3, I4, I7, Q4, Q13, Q17, Q22, Q27, Q34, D2, D4,
D12; `glossary.md` terms resume, idempotent, claim, abort, busy, locked,
wake, pending input, delivery, delivers, outcome, auto-step.
