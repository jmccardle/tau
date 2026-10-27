# 02 The step

A step is one cycle for one cursor. It has five phases: pre hooks on the
effect, the effect, pre hooks on the node, **write then move**, post hooks on
the written node. There are three step kinds. `input` writes a body an input
hook handled input with; `sample` makes one provider request and writes one
`Assistant` node; `tools` runs every unanswered call and writes one `Result`
per call, moving onto each as it is written. After the step the driver
evaluates `next(walk(at))` and, with auto-step on, keeps stepping until
`next` is `Wait`. Pre hooks change what is written; post hooks change what
happens around a written node. Every hook is `hook(cursor, subject)` and may
act through its cursor at any hook point. A step records the `at` it started
from, and its own move is compare-and-set: any other move of its busy cursor
interrupts it.

## The five phases

```
 1 pre hooks      2 effect         3 pre hooks       4 write + move       5 post hooks
   on the effect                     on the node       (durable together)   on the written node
 ────────────────────────────────────────────────────────────────────────────────────────────
 input   (none)        (none)        (none)            append, move onto    post_input
         (the body an input hook handled input with: pre_input runs before, outside the step)
 sample  pre_sample    provider      pre_reply         append, move onto    post_reply
 tools   pre_tool      run the tool  pre_result        append, move onto    post_result
         (per call)    (per call)    (per call)        (per result)         (per result)

 then: next(walk(at))  ──▶  Sample | RunTools | Wait
       auto-step on: step again until Wait       auto-step off: cursor pauses, already moved
```

Hooks at one point run in the order listed in `config_at(at)`; each gets what
the previous one returned. A cursor is **busy** from the step's start to its
last post hook. `post_open` runs outside steps, when a cursor opens or a
session loads.

## Sample step

```
 A.at = u1 (User)        next = Sample

 pre_sample(tip = u1)
   │  returns tip'   ......... u1 itself, a node it appended (a reminder), a Config
   │                           (model choice), or the end of a line it built (compaction)
   │  returns a reply  ....... ANSWER: provider not called, remaining pre_sample hooks skipped
   │  raises  ................ hook error: nothing written, auto-step off
   ▼
 driver: apply I6 to tip'   ·   can a reply be appended under tip'? (I4) else refuse the step
   ▼
 provider.sample( walk(tip'), config_at(tip') )          stream_delta events; parts accumulate
   ▼
 pre_reply(reply)
   │  returns reply' ......... parts possibly rewritten (guard, redaction), never `stop`;
   │                           also runs after an ANSWER
   │  returns nothing ........ REFUSE: nothing written, its calls never existed, auto-step off
   ▼
 append Assistant a2 under tip'     by provider   (extension(name) if answered or rewritten)
                                    (under I6's restating Config instead, if the driver appended one)
 move A: u1 ──▶ a2                  by core       only if A.at is still u1 (compare-and-set);
                                                  commits tip''s line, then a2
   ▼
 post_reply(cursor, a2)   runs unless the step was interrupted
   │  may append side nodes (scratch); may write "continue" (cursor.write)
   │  may move or rerun any cursor; the later post_reply hooks still run on a2,
   │  seeing cursor.at wherever this one left it
   ▼
 next(walk(at))
```

## Tools step

```
 A.at = a1 (Assistant, stop = tools, calls c3 and c5)     next = RunTools(c3, c5)
 A's step takes a1's claim (waits if another step holds it), re-reads the run

 per call                 pre_tool(call)
                            │ returns call'   rewritten (sandbox, remote host)
                            │ returns outcome ANSWER: Ok (cache, mock) or Skipped("blocked by ...")
                            │                 tool not run; pre_result still runs
                            │ raises          the call is answered Err("hook error") by core
                            ▼
                          run the tool      c3 and c5 run concurrently unless a tool is exclusive
                            ▼
                          pre_result(call', outcome)   as soon as this tool finishes, concurrently
                            │ returns an outcome of the same kind; content may be redacted
                            │ cannot drop it: every call is owed a result (I4)
                            ▼ (results wait in line, in completion order)
 one at a time per cursor append Result under the previous node (a1 for the first)
                          move A onto it                 by core
                          post_result(node) finishes     before the next outcome is written
                            │ may append side nodes, or act through its cursor
                            │ a move before the batch is complete interrupts the calls still
                            │ running: each is answered Interrupted under the run (scratch)
```

```mermaid
sequenceDiagram
    participant D as driver
    participant H as hooks
    participant T as tools
    participant S as session
    Note over D,S: A.at = a1, claim held, next = RunTools(c3, c5)
    D->>H: pre_tool(c3), pre_tool(c5)
    H-->>D: call, or an outcome (answer)
    par concurrent unless exclusive
        D->>T: run c3
    and
        D->>T: run c5
    end
    T-->>D: c5 finishes first
    D->>H: pre_result(c5, outcome)
    H-->>D: outcome
    D->>S: append r5 under a1
    D->>S: move A to r5
    D->>H: post_result(r5)
    T-->>D: c3 finishes meanwhile
    D->>H: pre_result(c3, outcome), concurrently with post_result(r5)
    Note over D,H: r3 waits for r5's post hooks before it is written
    D->>S: append r3 under r5
    D->>S: move A to r3
    D->>H: post_result(r3): it completes the batch (the base extension may deliver input here)
    Note over D,S: batch complete, next = Sample
```

A call whose `args_raw` does not parse is answered `Err("invalid arguments")`
(by core) without running. Results core writes (`Err("invalid arguments")`,
`Interrupted`, `Err("hook error")`) skip `pre_result`: there is nothing to
rewrite. They are written and moved onto, and their `post_result` hooks run,
so a hook that waits for the completing result always sees it. Under a reply with any other `stop`, no call runs
and none is owed a result.

## Input

```
 frontend: Session.input(A, "fix it too")       A busy or at rest; nothing else in core looks at input
   ▼
 pre_input(cursor, input)   each hook, in config order
   │  HANDLED, with a body ...... an input step writes it under at and moves onto it (by core),
   │                             then post_input; on a busy cursor the step is interrupted in place first
   │  HANDLED, no body .......... the hook did all it meant to (e.g. wrote a pending input)
   │  PASS (input') ............. the next hook gets input', possibly rewritten
   │  REFUSE (reason) ........... nothing written; Session.input raises; the frontend keeps the text
   │  every hook passes ......... REFUSED: "no extension handled this input"
   ▼
 input step: append (by frontend if it is the input as given, else extension(name)); move A onto it
   ▼
 post_input(cursor, node)   e.g. write a sibling u1' with derived_from [u1] and move onto it;
   ▼                        the typed u1 stays, committed
 next(walk(at))
```

## The base extension: busy input, the inbox, delivery

```
 k0 Config ─ s ─ u1 ─ a1 [c3, c5] ─ r5 ◀ A.at        A is busy: c3 still running
 └ ib  Custom{kind: inbox}             by extension(base)   side node under the first root, scratch
   └ p1  Custom{kind: pending_input,   by extension(base)   Session.input(A, "also check b.rs")
           cursor: A, text: "also check b.rs"}               while busy: pending, handled, no body

 c3 finishes:  r3 written under r5, A moved onto it (by core); the batch is complete
 post_result(cursor, r3), the base extension's hook (deliver: after_batch):
   pending for A = pending_input nodes for A that no node copies from = [p1]   (reverse index: one lookup)
   cursor.write( User "also check b.rs", copied_from: [p1] )               one durable write: node + move
   other post_result hooks on r3 run too, before or after it, whatever the config order

 k0 ─ s ─ u1 ─ a1 ─ r5 ─ r3 ─ u2 ◀ A.at      u2 by extension(base), copied_from [p1]    next = Sample
 p1 is delivered: u2 copies from it. No other marker; a crash cannot deliver it twice.
```

- At rest, the base extension handles input with a `User` body, after
  delivering anything still pending for the cursor, so order is kept.
- At rest at an unstarted or partial batch, it writes a pending input and
  steps the cursor; the batch is answered first (Q17).
- `deliver: after_stop` waits for a `post_reply` on a reply that is not a
  batch. `combine: false` writes one `User` node per pending input.
- After a reload, its `post_open` delivers what is still pending, if `at` can
  take a `User` node (`06-crash-and-resume.md`).

## What a hook may do

Every hook point gets the same cursor handle, bound to the hook's extension:

```
 read   cursor.at   cursor.next   cursor.busy   cursor.auto_step   cursor.session (every node, every cursor)
 act    append side nodes      cursor.write(body)  (append under at + move, one durable write)
        move / rerun any cursor      set auto-step      step      abort      open cursors
```

No hook point forbids any of these; the tree decides what each does:

| situation | what the action does |
|---|---|
| append or move breaks I3, I4, or I6 | refused; raises into the hook, which may catch it |
| the moved cursor is busy | its step is interrupted, the move wins (below) |
| a post hook moves after its step's last own move | the step is done, so nothing is interrupted; the other post hooks on the node still run, and see the new `cursor.at` |
| two post hooks on one node both move | two real moves, in config order |
| the moved cursor is at rest | the move lands; when the hook returns, the driver carries on per auto-step |
| a hook writes a node or a move | no hooks run for it; a step it causes runs hooks as usual |
| a hook opens a cursor | the new cursor's `post_open` runs (Q32) |

What a hook *returns* still steers its own hook point: rewrite, answer, or
refuse, as before.

| hook point | rewrite | answer | refuse |
|---|---|---|---|
| `pre_input` | pass a rewritten input | handled (optionally with a body) | refuse (a reason) |
| `pre_sample` | return a different tip | yes: a reply | no |
| `pre_reply` | yes, never `stop` | n/a | yes |
| `pre_tool` | yes | yes: `Ok` or `Skipped` | no: answers `Skipped` instead |
| `pre_result` | content only, never the kind (Q18) | n/a | no |
| post hooks, `post_open` | n/a | n/a | n/a |

## Moving a busy cursor

```
 A is sampling: started at u1, context tip u1, parts streaming
 a hook (any point, any cursor) or a frontend moves A to b2

 1 cancel the step's running parts (not the hook that asked)
 2 write its records where they came from:   u1 ─ a2 Assistant "Let me…", stop interrupted   (pre_reply ran)
 3 the step's own move to a2: compare-and-set fails (A was moved) ──▶ skipped; a2 is SCRATCH
 4 resolve to = b2 (fast-forward, I6), write the move; A.at = b2
 5 what follows is the mover's: a go-to sits; a hook's move carries on per auto-step

 in a tools step, the same: every call without a result gets Interrupted under its run (scratch),
 the claim is released, and then the move resolves, so it never lands above those results (I3)

 Session.abort(A) is the same interrupt without a move: step 3 succeeds, A moves onto a2,
 and auto-step goes off. Handled input with a body is also in place, then writes the body under a2.
```

## Go to, re-run, and a hook's move

```
                    go to                     re-run                      hook's move
 call               Session.move             Session.rerun               cursor handle: move
 caller             frontend                 frontend, or any hook       any hook, on any cursor
 move `by`          frontend                 frontend / extension(name)  extension(name)
 runs hooks         none                     its tools step runs hooks   none for the move; the step that
                                             as usual (pre_tool,         follows runs hooks as usual
                                             pre_result, post_result)
 starts a step      never, even with         yes: a tools step for each  no; when the hook returns, the
                    auto-step on             call unanswered on the new  driver evaluates next and continues
                                             line (when the hook         per auto-step; counts to max_steps
                                             returns, if a hook asked)
 busy cursor        interrupts its step      interrupts its step         interrupts its step
 lands              fast-forwards onto a     exactly where sent          fast-forwards, checked like a go-to
                    batch or its run
 claim              never taken              taken by its tools step     never taken
 I6                 applies                  applies                     applies
 refused when       restatement needed under nothing unanswered there;   same as go-to; the hook may catch it
                    an unstarted or partial  copied batch; restatement
                    batch; a copied batch    needed
                    that is not complete
```

A step's own move (phase 4, and a fast-forward the step makes when it
re-reads a run) is none of these: it is never refused, I6 does not apply to
it, and it is compare-and-set, so it is skipped if the cursor was moved. A
tool's handle cannot move or re-run.

Reads with: `model.md` §5 (move, compare-and-set, re-run), §6 (next), §7
(hooks, hook points, steps, input, the base extension, what still holds),
§8 (interrupt, abort); invariants I1, I3, I4, I5, I6, I7; Q15, Q17, Q18, Q20,
Q21, Q25, Q31, Q32; `glossary.md` terms step, busy, hook, hook point, cursor
handle, answer, refuse, handled, pass, interrupt, base extension, inbox,
pending input, auto-step, claim, re-run, go to.
