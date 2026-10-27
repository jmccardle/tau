# 06 Crash, reload, resume; abort

Nothing is state unless it is a node, a move record, a label record, or a
scratchpad item, so a reload is a replay (I7). A crash in the middle of a
tools phase leaves the cursor at the batch's `Assistant` node, with a
**journal item** for every call that finished. Auto-step, open questions,
shown state and stream deltas are gone, and every cursor loads at rest with
auto-step off. The next tools phase at that batch uses each journal item's
outcome, runs idempotent calls again, and answers the rest `Interrupted`
(D4). The join's `Results` node delivers the journal items. Pending input
survives as items too. The second half shows abort: during a sample, during
tools, during a `pre_tool` waiting for approval, and a frontend's go-to on a
busy cursor.

## Crash mid tools phase

```
 before the crash                              what is on disk
 ──────────────────────────────────           ───────────────────────────────────────────
 k0 ─ s ─ u1 ─ a1 [c3, c5, stop tools] ◀ A.at  record log: k0 s u1 a1, with their moves
 c5 finished, pre_result ran                                (u1 ──▶ a1, by core)
 c3 still running                              scratchpad file: item 6 journal
                                                 { from: {cursor: A}, to: A,
                                                   data: {batch: a1, call: c5, outcome: Ok(…)} }
                                               item 4 input for A
                                               not on disk: auto_step, the c3 run, questions
 ──────────────────── process dies ────────────────────

 a torn final record (a write cut off) is detected by its length and checksum and ignored on
 load: the state is exactly the one before that write began. Not repair, no guessing.
```

## Reload

```
 load = replay the record log, then read the scratchpad and drop delivered items (I7)
   A.at = a1        auto-step off        no open questions        items 4 and 6 still there
   next(walk(a1)) = RunTools(c3, c5)     next never looks below at
 then, in the lease holder only, every cursor wakes: A's step runs pre_step and post_step,
 no phase (auto-step off, Q39). A read-only process never runs a hook.
```

## The next tools phase: journal, idempotent, Interrupted

```
 a send, or Session.step: the step's phases apply
 input phase: input pending, but next == RunTools ──▶ waits (Q40)
 sample phase: next == RunTools ──▶ does not apply
 tools phase, per call of a1:
```

```
 c5 has a journal item for batch a1 ──▶ its outcome is used; c5 does not run; pre_result
        │                               already ran before the crash
 c3 has none
        ├─ tool is idempotent ──▶ pre_tool(c3) ─▶ run c3 ─▶ pre_result ─▶ journal item 8
        │
        └─ otherwise ───────────▶ Interrupted, by core; skips pre_result
                                   the tool may or may not have run (D4)

 join:  r1 Results { c3: …, c5: Ok(…) } under a1, delivers [6, 8] (or [6]),
        + move A: a1 ──▶ r1 (by core)            one record; then items 6 and 8 are removed
        post_result(r1), post_step(done)
 next step: input phase delivers item 4 as u2 under r1, then the sample phase
```

Why the journal: a ten-minute `bash` that finished before the crash keeps its
outcome; it is not marked `Interrupted` because its sibling was still
running, and it does not run again.

```
 crash after the join's record, before the journal items' removal:
   load drops items 6 and 8, because r1 delivers them: never used twice
 crash after A went elsewhere, then back to a1 later (Q41):
   the journal items are still there, and the next tools phase at a1 uses them; a frontend
   that wants a clean re-run removes them first
```

A crash during a sample is simpler: nothing was committed for the reply, so
on reload `next` is `Sample` and the next step samples again. A crash between
a commit and its move cannot happen: they are one record (§11).

## A pending input across the crash

```
 before the crash: A busy running c3; client laptop typed "also b.rs"
   scratchpad: item 4 { from: {frontend: laptop, cursor: A}, to: A, data: {text: "also b.rs"} }

 after reload: item 4 is open, and no node delivers it. A wakes with auto-step off: no phase.
 a send from any client (or Session.step):
   step 1   input waits (next == RunTools at a1); the tools phase runs, the join commits r1
   step 2   input phase: pre_input(item 4), then u2 User "also b.rs" by frontend(laptop),
            delivers [4], under r1, + the move r1 ──▶ u2; item 4 is removed; sample phase

 crash before u2's record:   item 4 is still pending on the next load
 crash after it:             load drops item 4 because u2 delivers it: never twice
```

## Abort during a sample

Abort (esc) turns auto-step off and cancels the effect in flight. It never
cancels a hook, and no later effect starts. The draft is committed under
`at` and its hooks run as usual.

```
 A.at = u1        sample phase: pre_sample appended n1 (a reminder) and moved A onto it

 abort ──▶ auto-step off; cancel the request
   │
   ├─ no parts had arrived
   │      no draft: nothing committed           u1 ─ n1 ◀ A.at    (the hook's move stands)
   │      post_step(aborted)                    next = Sample
   │
   └─ some parts had arrived                    the reply draft: parts so far, stop interrupted
          pre_reply runs (redaction applies)
          a2 Assistant by provider, committed under at = n1     parts: [ Text "Let me ",
          move A: n1 ──▶ a2   by core                            Call{c9, write_file, '{"path":"a'} ]
          post_reply(a2), then post_step(aborted)
          next(walk(a2)) = Wait    c9 is not runnable (stop is not tools)
          later samples: the provider renders c9 as text per config.partial_calls (Q27)

 abort while pre_sample runs: the hook finishes; the sample never starts; post_step(aborted)
 then A rests. Pending input stays pending (Q42); any frontend may edit or remove it
```

The tokens the user saw are stored, and the model sees them next time.

## Abort during tools, with a pre_tool waiting for approval

```
 a1 [c3, c5, c6]   c5 finished (journal item)   c3 running   c6's pre_tool is asking a person
 abort ──▶ auto-step off; cancel c3 (its tool, not any hook)
   c3: Interrupted
   c6: its question stays open; A stays locked; the join waits
       declined ──▶ ask returns None; the hook returns "permission not granted"
                    ──▶ c6: Err("permission not granted"), in the extension's words
       approved ──▶ the hook returns None, but no new effect starts ──▶ c6: Interrupted (Q34)
   a call whose pre_result was running finishes it, and its outcome is used, redacted
 join: r1 Results { c3: Interrupted, c5: Ok(…), c6: … } under a1, delivers c5's journal item
       + move A: a1 ──▶ r1;  post_result(r1);  post_step(aborted);  A rests
```

## A frontend's go-to during tools

```
 a1 [c3, c5]      A.at = a1      c5 finished (journal item), c3 running, no hook running
 client laptop goes to u1        (refused instead while a hook of A runs, or a question is open)

 1 abort: auto-step off; cancel c3
 2 c3: Interrupted; the join commits r1 Results under a1, delivers c5's item; A.at = r1
 3 post_result(r1), post_step(aborted)
 4 the move r1 ──▶ u1, by frontend(laptop)          A.at = u1
 5 the go-to wakes A: pre_step, post_step, no phase (auto-step off)

 u1 ◀ A.at
 └ a1 ─ r1        the abort is on the tree, then A went back
 a later go-to a1 and Session.step runs both calls again and commits a sibling Results (a re-run)
```

Outcomes core writes (`Interrupted`, `Err("invalid arguments")`,
`Err("hook error")`) skip `pre_result`: there is no tool output to rewrite.
`post_result` runs once on the `Results` node, aborted steps included
(`model.md` §7, §8).

Reads with: `model.md` §2 (the journal), §6 (phases, endings, waking), §7
(input, questions), §8 (abort, failure, crash during tools, reload), §11
(records, the scratchpad file, delivery order), I4, I7, Q13, Q27, Q34, Q39,
Q40, Q41, Q42, D4, D12; `glossary.md` terms journal, idempotent, abort,
busy, locked, wake, input, delivers, outcome, auto-step.
