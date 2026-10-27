# 02 The step

A **step** is one pass of a fixed loop, from `pre_step` to `post_step`. In
between, three **phases** run in order, each only if it applies: input,
sample, tools. Every phase has the same shape: pre hook on the effect, the
effect, pre hook on the draft, **commit under `at`** (the node and the move
onto it, one record), post hook. Hooks change the machine's state (move `at`,
edit the draft, set auto-step, answer an effect); then the machine acts. A
hook returns `None` to carry on, or a string to stop. A cursor is **busy**
from the start of a step until it rests.

## The loop

```
 wake: an item added to its scratchpad · it opens · the session loads · a go-to lands
       · a frontend steps it (Session.step) or turns auto-step on

 ┌▶ pre_step
 │  for phase in [input, sample, tools]:          applies? read at the start of the phase
 │      pre_<effect>     pre_sample | pre_tool                 (input: none)
 │      effect           sample walk(at) | run the calls       (input: none)
 │      pre_<draft>      pre_input | pre_reply | pre_result
 │      commit           the draft under at + the move onto it, one record
 │      post_<draft>     post_reply | post_result              (input: none)
 │  post_step(ending)
 │
 │  after post_step:
 │    auto-step on, and next(walk(at)) != Wait or input is pending ──▶ back to pre_step
 └─── the scratchpad changed since this pre_step started ─────────────▶ back to pre_step,
                                          throttled: ≥ 250 ms if this step committed nothing
    otherwise, or after aborted / halted / failed:  rest
```

| phase | applies when (and auto-step on, or `Session.step`) | pre hook on the effect | effect | pre hook on the draft | commits | post hook |
|---|---|---|---|---|---|---|
| input | input pending | — | — | `pre_input`, per item | one `User` per item, each `delivers` its item | — |
| sample | `next == Sample` | `pre_sample` | provider request | `pre_reply` | one `Assistant` | `post_reply` |
| tools | `next == RunTools` | `pre_tool`, per call | the calls, in parallel | `pre_result`, per call | one `Results` at the join | `post_result` |

`next(walk(at))`: `User` or `Results` → `Sample`; `Assistant` with
`stop == tools` and calls → `RunTools`; anything else → `Wait`. It never looks
below `at`.

```
 ending        when                                                     auto-step after
 ──────────────────────────────────────────────────────────────────────────────────────
 done          the phases ran, or none applied                          unchanged
 halted(why)   a cursor hook returned a string                          off
 failed(err)   a sample failed, or a hook raised                        off
 aborted       an abort, or a frontend's move of the busy cursor        off
 what the step did is in the move records: cursor.moves(since_step=True)
```

Nothing polls. An item an extension leaves in place causes no further step
until the scratchpad changes again.

## Two hook shapes, one return rule

```
 cursor hooks   pre_input  pre_step  pre_sample  pre_reply  post_reply  post_result  post_step
                async def hook(cursor: Cursor, subject) -> str | None
                    may move the cursor, write under at, set auto-step

 tool hooks     pre_tool  pre_result               one chain per call, in parallel
                async def hook(node: NodeHandle, call: CallDraft) -> str | None
                    node = the batch's Assistant node; everything a cursor handle does,
                    except move the cursor or set its auto-step

 return None    carry on
 return "why"   from a cursor hook: HALT. Later hooks at that point don't run, the phase's
                                    draft is discarded, auto-step off, post_step(halted(why)).
                                    pre_input: the input is refused (item removed, a notice
                                    to its client). post_step: only turns auto-step off.
                from a tool hook:   that call's outcome is Err(why). pre_tool: the tool does
                                    not run. pre_result: the outcome is replaced. The other
                                    calls carry on.
 raise          like a string, with the error: failed(err), or Err("hook error") for a call
```

Hooks at one point run in the order listed in `config_at(at)`. Drafts are
mutable; a hook edits one in place.

## Input phase, then sample phase

```
 A.at = r1 (Results)      scratchpad: item 4 { from: {frontend: laptop, cursor: A},
                                               to: A, data: {text: "also check b.rs"} }
 auto-step on; next = Sample; input pending                  ──▶ the input phase applies

 pre_input(cursor, draft{text: "also check b.rs"})
   │  None ..................... carry on (the draft may have been edited)
   │  may append n1 and move onto it: the input then goes under n1 (a "prepend")
   │  "why" .................... REFUSED: item 4 removed, notice(laptop, why), step halts
   ▼
 commit  u2 User "also check b.rs", by frontend(laptop), delivers [4], under at = r1
         + move A: r1 ──▶ u2 (by core)             one record; then item 4 is removed
   ▼
 sample phase: next(walk(u2)) = Sample
 pre_sample(cursor, sample draft)
   │  may move A (to a Config it appended, a reminder, the end of a built line): what is
   │    sent is walk(at) as at stands when the sample starts (I1)
   │  may set draft.reply ...... ANSWER: no request; pre_reply still runs (a cache hit is
   │                             still redacted)
   ▼
 provider.sample( walk(at), config_at(at) )           stream_delta events; the reply draft fills
   ▼
 pre_reply(cursor, reply draft)
   │  may edit parts (guard, redaction), never stop
   │  may move A: the reply is committed under wherever at now stands
   │  "why" .................... HALT: the reply is discarded, nothing committed
   ▼
 commit  a2 Assistant, by provider (extension(name) if answered or rewritten),
         under at + move A onto a2                                 one record
   ▼
 post_reply(cursor, a2)
   │  may append side nodes; may write "continue" (cursor.write) under a2
   │  may move A: that decides where the tools phase looks
   ▼
 tools phase applies only if next(walk(at)) == RunTools at this moment
```

```
 a pre_reply hook moves the reply             a post_reply hook moves where tools look

 u2 ─ x1 ◀ A.at (moved there by pre_reply)    u2 ─ a2 [c3]           A moved to b1 by post_reply
 └ (sampled from walk(u2))                    b1 Assistant [c7], stop tools ◀ A.at
 commit: a2 under x1, A ──▶ a2                tools phase: next(walk(b1)) = RunTools(c7)
 walk(a2) is not what the model saw:          c7 runs; c3 does not
 a hook's choice, in the move records
```

## Tools phase: outcomes in memory, then one `Results` at the join

```
 A.at = a1 (Assistant, stop = tools, calls c3 and c5)     next = RunTools(c3, c5)

 per call, in parallel    pre_tool(node a1, call draft)
                            │ may edit the arguments (sandbox, remote host)
                            │ may set draft.outcome .. ANSWER: the tool does not run
                            │ may ask a person (node.ask): A is locked until answered or declined
                            │ "why" ................ outcome Err(why); the tool does not run
                            ▼
                          run the tool      unless exclusive (Q21)
                            ▼
                          pre_result(node a1, call draft with outcome)    as this call finishes
                            │ may edit the outcome's content, not its kind
                            │ "why" ................ outcome replaced by Err(why)
                            ▼
                          the outcome is fed to core, which holds it in memory

 join (every call has an outcome)
   commit  r1 Results { c3: …, c5: … } in call order, under a1
           + move A: a1 ──▶ r1 (by core)            one record
   post_result(cursor, r1)                          once, with the cursor
 post_step(done)
```

`at` cannot move during the tools phase: tool hooks have no cursor. Nothing
about a running batch is stored, so a crash before the join loses its
outcomes (`06-crash-and-resume.md`). A call
whose `args_raw` does not parse is `Err("invalid arguments")` (by core)
without running. Core-written outcomes (`Err("invalid arguments")`,
`Interrupted`, `Err("hook error")`) skip `pre_result`. Under a reply with any
other `stop`, nothing runs.

```mermaid
sequenceDiagram
    participant C as core (step machine)
    participant D as driver
    participant H as hooks
    participant T as tools
    Note over C,T: A.at = a1, next = RunTools(c3, c5)
    C->>D: job: run calls c3, c5
    par one unit per call, concurrent unless exclusive
        D->>H: pre_tool(c3)
        D->>T: run c3
        D->>H: pre_result(c3)
    and
        D->>H: pre_tool(c5)
        D->>T: run c5
        D->>H: pre_result(c5)
    end
    D-->>C: c5's outcome (it finished first)
    D-->>C: c3's outcome
    Note over C,D: the join: core writes r1 Results under a1 + move A to r1, one record, inside the call
    C->>D: job: post_result(r1)
    D-->>C: None
    C->>D: job: post_step(done)
    Note over C,D: next = Sample; with auto-step on, another step starts at pre_step
```

## What a hook may do

```
 any hook        read     the tree, every cursor's at, labels, move records (cursor.moves())
                 append   side nodes anywhere, detached roots
                 items    add / update / remove, in any cursor's scratchpad
                 people   ask (locks the cursor)  show  tell
                 also     open cursors · set and remove labels · sample a walk · run a tool
 cursor hooks    cursor.move(to) · cursor.write(body, delivers=[…]) · cursor.auto_step = …
 pre_step and post_step   read its own cursor's scratchpad
 never           move another cursor (add an item to its scratchpad instead)
```

| situation | what happens |
|---|---|
| a cursor hook moves its cursor | applies at once; later hooks at that point see the new `cursor.at` |
| two hooks at one point both move | two real moves, in config order; `cursor.moves(since_step=True)` shows both |
| a hook writes a node, a move, or an item | no hooks run for it on this step; an item may wake another cursor |
| a hook opens a cursor, or adds an item to one | that cursor wakes and runs its own step |
| a tool hook tries to move the cursor | it cannot: its handle has no move |
| a move to an id not in the session | refused (I3); raises into the hook, which may catch it |

## A frontend moves a busy cursor

```
 A is sampling under u1, parts streaming, no hook running
 client laptop: go to b2

 0 refused instead if a hook of A is running, or a question on A is open
 1 abort: auto-step off; cancel the stream (no hook is cancelled; no later effect starts)
 2 the reply draft has the parts so far, stop interrupted: pre_reply, commit under at, move
      u1 ─ a2 Assistant "Let me…", stop interrupted                    A.at = a2
 3 post_reply(a2), then post_step(aborted): hooks act on the aborted reply, may even move A
 4 the move: A.at = b2, by frontend(laptop)                             the go-to wins
 5 the go-to wakes A at b2 with auto-step off: pre_step, post_step(done), no phase

 during tools, the same: running calls are Interrupted, a pre_tool or pre_result that is
 running finishes, the join commits Results, post_result and post_step run, then the move
 Session.abort(A) is the same without the move: A rests after post_step.
```

## Go to, step, and a hook's move

```
                 go to                       Session.step                hook's move
 caller          a frontend client           a frontend client           a cursor hook, own cursor
 move `by`       frontend(client)            —                           extension(name)
 auto-step       turned off                  unchanged                   unchanged
 then            wakes: pre_step, post_step, one step's phases run       later hooks see the new at;
                 no phase (auto-step off)    whatever auto-step is       phases read it when they start
 busy cursor     refused while a hook runs   refused (already stepping)  n/a
                 or a question is open;
                 else abort first
 lands           exactly on `to`             —                           exactly on `to`
```

Re-running a batch is a go-to its `Assistant` node, then `Session.step`: the
tools phase runs and commits a sibling `Results` (`03-move-and-branch.md`). A
tool's handle cannot move a cursor.

Reads with: `model.md` §5 (move, a frontend's move of a busy cursor), §6
(next, the machine, the phase table, endings, waking, throttle,
`max_steps`, where things are committed), §7 (two shapes, the return value,
hook points, handles, input, questions, between cursors), §8 (abort), §12
(core and driver); invariants I1, I3, I4, I7; Q15, Q18, Q20, Q21, Q25, Q34,
Q39, Q43, Q44; `glossary.md` terms step, phase, draft, effect, commit,
busy, locked, hook, hook point, cursor hook, tool hook, halt, pre_step,
post_step, ending, cursor handle, node handle, abort, scratchpad, item,
input, delivers, auto-step, go to, question.
