# 02 The cycle and the step

A **cycle** is what a cursor does from the moment it wakes until it rests:
`pre_step`, then steps, each followed by `post_step`. A **step** is one
sample or one tools run. It has five phases: pre hooks on the effect, the
effect, pre hooks on the node, **write then move**, post hooks on the written
node; then `post_step`. Pre hooks change what is written; post hooks react
to a written node. Every hook is `hook(cursor, subject)` and acts only on its
own cursor, and on its cursor's line only at a **boundary**. Cursors talk
through the scratchpad. A cursor is **busy** for its whole cycle.

## The cycle

```
 wake: an item added to its scratchpad · it opens · the session loads · a go-to lands
       · a frontend steps it, re-runs on it, or turns auto-step on

 ┌▶ 1 pre_step                         boundary; reads the scratchpad (the base extension delivers here)
 │  2 next(walk(at))  ──▶  Sample | RunTools | Wait
 │  3 next != Wait and (auto-step on, or the cycle owes a step)?
 │       yes: one step (below), then post_step(ending)
 │            aborted ──────────────────────────────────────────────▶ rest
 │            auto-step on and next != Wait ──▶ back to 1
 │  4 scratchpad changed since the last pre_step started (and not aborted)?
 └──── yes, throttled: ≥ 250 ms since the previous pre_step started
    5 rest
```

Nothing polls. An item an extension leaves in place causes no further
`pre_step` until the scratchpad changes again.

## The five phases of a step

```
 1 pre hooks      2 effect         3 pre hooks       4 write + move       5 post hooks       then
   on the effect                     on the node       (one record)         on the node
 ─────────────────────────────────────────────────────────────────────────────────────────────────
 sample  pre_sample    provider      pre_reply         append, move onto    post_reply         post_step
 tools   pre_tool      run the tool  pre_result        append, move onto    post_result        post_step
         (per call)    (per call)    (per call)        (per result)         (per result)       (once)

 boundaries (a hook may write under at, move, or re-run its own cursor):
   pre_step · post_reply · post_result once the batch is complete · post_step
```

Hooks at one point run in the order listed in `config_at(at)`; each gets what
the previous one returned.

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
 move A: u1 ──▶ a2                  by core       commits tip''s line, then a2
   ▼
 post_reply(cursor, a2)   a boundary
   │  may append side nodes; may write "continue" (cursor.write) under a non-batch reply
   │  may move A: later post_reply hooks still run on a2, seeing cursor.at where it now is
   ▼
 post_step(cursor, wrote a2)
```

## Tools step

```
 A.at = a1 (Assistant, stop = tools, calls c3 and c5)     next = RunTools(c3, c5)
 A's step takes a1's claim (waits if another step holds it), re-reads the run

 per call                 pre_tool(call)
                            │ returns call'   rewritten (sandbox, remote host)
                            │ returns outcome ANSWER: Ok (cache, mock) or Skipped("blocked by ...")
                            │                 tool not run; pre_result still runs
                            │ may ask a person (cursor.ask): A is locked until answered or declined
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
                            │ not a boundary until the batch is complete: calls are still running,
                            │ so a move or a write under at here is refused
 after the last result    post_step(cursor, wrote r3)
```

```mermaid
sequenceDiagram
    participant C as core (cycle)
    participant D as driver
    participant H as hooks
    participant T as tools
    Note over C,T: A.at = a1, claim held, next = RunTools(c3, c5)
    C->>D: job: pre_tool(c3), pre_tool(c5)
    D->>H: run
    H-->>D: calls
    D-->>C: results
    C->>D: job: run c3, c5
    par concurrent unless exclusive
        D->>T: run c3
    and
        D->>T: run c5
    end
    T-->>D: c5 finishes first
    D-->>C: c5 done
    C->>D: job: pre_result(c5)
    D-->>C: outcome
    C->>D: job: persist r5 under a1 + move A to r5
    C->>D: job: post_result(r5)
    T-->>D: c3 finishes meanwhile
    D-->>C: c3 done
    C->>D: job: pre_result(c3), concurrently with post_result(r5)
    Note over C,D: r3 waits for r5's post hooks before it is written
    C->>D: job: persist r3 under r5 + move A to r3
    C->>D: job: post_result(r3), a boundary: the batch is complete
    C->>D: job: post_step(wrote r3)
    Note over C,D: next = Sample; with auto-step on, pre_step runs before the sample
```

A call whose `args_raw` does not parse is answered `Err("invalid arguments")`
(by core) without running. Results core writes (`Err("invalid arguments")`,
`Interrupted`, `Err("hook error")`) skip `pre_result`: there is nothing to
rewrite. They are written and moved onto, and their `post_result` hooks run,
so a hook that waits for the completing result always sees it. Under a reply
with any other `stop`, no call runs and none is owed a result.

## Input, the scratchpad, and delivery

```
 client laptop: Session.input(A, "also check b.rs")       A busy or at rest; refused if A is locked
   ▼
 pre_input(cursor, input)   each hook, in config order, outside A's cycle
   │  HANDLED ................... the hook did what it meant to (the base extension: added an item)
   │  PASS (input') ............. the next hook gets input', possibly rewritten
   │  REFUSE (reason) ........... nothing written; Session.input raises; the frontend keeps the text
   │  every hook passes ......... REFUSED: "no extension handled this input"
   ▼
 scratchpad:  item 4 { from: {frontend: laptop, extension: base, cursor: A},
                       to: A, data: {text: "also check b.rs"} }          adding it wakes A
```

```
 k0 Config ─ s ─ u1 ─ a1 [c3, c5] ─ r5 ◀ A.at        A is busy: c3 still running; item 4 waits

 c3 finishes:  r3 written under r5, A moved onto it (by core); the batch is complete
 post_step(wrote r3); auto-step on and next = Sample, so the cycle goes back to pre_step:
 pre_step, the base extension's hook (deliver: after_batch):
   open base items for A = [4]; at = r3 can take a User node
   cursor.deliver([4])      one record: u2 User "also check b.rs", by frontend(laptop),
                            delivers [4], and the move r3 ──▶ u2; then item 4 is removed

 k0 ─ s ─ u1 ─ a1 ─ r5 ─ r3 ─ u2 ◀ A.at      next = Sample: the sample step follows
 a crash after the record but before the removal: load drops item 4, because u2 delivers it
```

- Every input takes this path, busy or at rest. At rest, adding the item
  wakes the cursor and its `pre_step` delivers at once; with auto-step on (a
  send), the sample follows.
- At an unstarted or partial batch, `pre_step` delivers nothing (I4). A send
  turns auto-step on, the tools step answers the batch, and the `pre_step`
  before the next sample delivers (Q17).
- `deliver: after_stop` delivers only at a `pre_step` where `next` is `Wait`.
  `combine: false` writes one `User` node per item; `combine: true` joins
  consecutive items from one client.

## What a hook may do

```
 any hook point   read     cursor.at  cursor.next  cursor.busy  cursor.auto_step  cursor.session
                  append   side nodes, detached roots
                  items    add / update / remove, in any cursor's scratchpad
                  people   cursor.ask (locks the cursor)  cursor.show  cursor.tell
                  also     set its own auto-step · open cursors · sample a walk · run a tool
 boundaries only  cursor.write(body)  cursor.deliver(items)  move or re-run its own cursor
 pre_step and post_step only   read its own cursor's scratchpad
 never            act on another cursor's line (write an item to it instead)
```

| situation | what the action does |
|---|---|
| append or move breaks I3, I4, or I6 | refused; raises into the hook, which may catch it |
| a move or a write under `at` outside a boundary | refused |
| a move of another cursor | refused; add an item to its scratchpad instead |
| a hook at a boundary moves its own cursor | applies at once; later hooks at that point see the new `cursor.at` |
| two hooks at one point both move | two real moves, in config order |
| a hook writes a node or a move | no hooks run for it; a step it causes runs hooks as usual |
| a hook opens a cursor, or adds an item to one | that cursor wakes and runs its own cycle |

What a hook *returns* still steers its own hook point:

| hook point | rewrite | answer | refuse |
|---|---|---|---|
| `pre_input` | pass a rewritten input | handled | refuse (a reason) |
| `pre_sample` | return a different tip | yes: a reply | no |
| `pre_reply` | yes, never `stop` | n/a | yes |
| `pre_tool` | yes | yes: `Ok` or `Skipped` | no: answers `Skipped` instead |
| `pre_result` | content only, never the kind (Q18) | n/a | no |
| post hooks, `pre_step`, `post_step` | n/a | n/a | n/a |

## A frontend moves a busy cursor

```
 A is sampling: context tip u1, parts streaming, no hook running
 client laptop: go to b2

 0 refused instead if a hook of A is running, or a question on A is open
 1 abort: auto-step off; cancel the stream (no hook is cancelled)
 2 pre_reply runs on the parts so far; write and move onto them as usual:
      u1 ─ a2 Assistant "Let me…", stop interrupted                    A.at = a2, committed
 3 post_reply(a2), then post_step(aborted): hooks act on the aborted reply, may even move A;
   the base extension returns pending inputs to their clients
 4 resolve to = b2 (fast-forward, I6), write the move; A.at = b2       the go-to wins
 5 the go-to wakes A at b2: pre_step runs; a step follows only if an extension turns
   auto-step on

 in a tools step, the same: each running call is answered Interrupted under its run, moved
 onto in turn, its post_result hooks run; a call whose pre_tool or pre_result is running
 finishes normally; then the claim is released, post_step(aborted), and the move resolves (I3)

 Session.abort(A) is the same without the move: A rests after post_step.
```

## Go to, re-run, and a hook's move

```
                    go to                     re-run                      hook's move
 call               Session.move             Session.rerun               cursor handle: move
 caller             a frontend client        a client, or a hook on its  a hook, on its own cursor,
                                             own cursor at a boundary    at a boundary
 move `by`          frontend(client)         frontend(client) /          extension(name)
                                             extension(name)
 auto-step          turned off               unchanged                   unchanged
 then               wakes the cursor:        a tools step for each call  the hook point finishes; the
                    pre_step at the          unanswered on the new line  cycle carries on from the new
                    destination; a step      (pre_step first, as before  at, per auto-step
                    only if an extension     every step)
                    starts one
 busy cursor        refused while a hook     like a go-to                n/a: a hook moves its own
                    runs or a question is                                cursor only at a boundary
                    open; else abort first
 lands              fast-forwards onto a     exactly where sent          fast-forwards, like a go-to
                    batch or its run
 claim              never taken              taken by its tools step     never taken
 I6                 applies                  applies                     applies
 refused when       a copied batch that is   nothing unanswered there;   as go-to, plus outside a
                    not complete             a copied batch              boundary or on another cursor
 I6 at a batch      deferred to the completing result (`03-move-and-branch.md`)
```

A step's own move (phase 4, and a fast-forward the step makes when it
re-reads a run) is none of these: it is never refused or skipped, and I6
applies only as a deferred restatement written with the move onto a
completing result. A tool's handle cannot move or re-run.

Reads with: `model.md` §5 (move, a frontend's move of a busy cursor, re-run),
§6 (next), §7 (the cycle, hooks, boundaries, hook points, steps, between
cursors, input, the base extension, questions), §8 (abort); §12 (core and
driver); invariants I1, I3, I4, I5, I6, I7; Q15, Q17, Q18, Q20, Q21, Q25,
Q31, Q32, Q34, Q38; `glossary.md` terms cycle, wake, throttle, boundary,
step, busy, locked, hook, hook point, pre_step, post_step, ending, cursor
handle, answer, refuse, handled, pass, abort, base extension, scratchpad,
item, pending input, delivery, delivers, auto-step, claim, re-run, go to,
question.
