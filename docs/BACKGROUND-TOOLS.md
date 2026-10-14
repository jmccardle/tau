# Backgrounding is a property conferred on a tool

Status: **built 2026-09-25, revised 2026-09-29, made cursor-correct 2026-10-05.** A
built-in extension, `tau-agent-core/src/tau_agent_core/extensions_builtin/background.py`,
tests in `tau-agent-core/tests/test_background_extension.py` and
`test_background_cursors.py`. Core additions: `ExtensionContext.session_id`,
`ExtensionContext.cursors`, and `ExtensionAPI.submit(cursor=)`. Unmerged, on
`exp/background-cursors` (from `feat/background-tools`); John merges it and
writes the release note. The 2026-10-05 experiment is
`experiments/background-cursors/RESULTS.md`.

Requested as: "build backgrounding as a generic τ extension that can be applied
to any tool. Not per-tool plumbing added to bash, then to web search, then to
recall. A wrapper any tool can be handed to, so backgrounding is a property that
can be conferred rather than a feature each tool implements separately."

Load it by path like any file extension, and name the tools in config:

```json
"extensions": {"background": {"tools": ["bash", "grep"]}}
```

## 1. Why an extension, and not a core tool

Backgrounding is a general τ faculty, not something built for one agent. It
lives in an extension because of what it does at a session boundary.

A backgrounded call can finish after the conversation that started it has ended.
When that happens its result is delivered into whatever conversation is current,
labelled with where it came from (§6). That is non-deterministic, cross-session
behaviour: whether the result lands in the same conversation depends on timing
the model never sees. Retrieval already works this way. A memory extension brings
material from other sessions into this one, and the extension is what carries
the attribution.

A native core tool doing the same would break the harness's semantics. The agent
loop is a pure function of the tree path it is given (the tree-as-truth
invariant). A core tool whose results can arrive in a different conversation
would make that loop reason about cross-session timing. The extension justifies
that behaviour and contains it. It enters the conversation through the one door,
`submit`, as an attributed extension submission, and the loop sees an ordinary
user message.

## 2. Why this is not per-tool plumbing

A tool that implements its own backgrounding owns four things that have nothing
to do with what the tool does: a handle namespace, a task lifetime, a delivery
route back into the conversation, and a cancellation story. So the unit is a
wrapper, and the wrapped tool never learns it was wrapped. `backgroundable` strips
the flag before delegating, so a call without it is indistinguishable from the
original.

Wrapping a built-in works because `AgentSession._build_turn_tools` resolves
extension tools last. Registering a tool of the same name confers the property. A
built-in named in config is constructed with `create_all_tools(ctx.cwd)`, so it
works in the session's directory rather than the process's.

## 3. The model elects it; nothing promotes

There is no time threshold. A threshold decides for the model using evidence the
model did not supply. The judgement it would replace, "can I keep going without
this answer?", belongs to the caller of the tool. So the judgement is written
where the model reads it, as the added parameter's description, and
`BACKGROUND_PARAM` is the entire policy:

> Background it when you can keep going without the answer; block on it when your
> next step depends on it.

The schema gains exactly one boolean. `_schema_with_background` deep-copies the
original, and **raises** if the tool already declares that name.

## 4. Delivery is a steer at the issuing cursor

A completed call is delivered with
`api.submit(..., multitask_strategy="steer", correlation=..., cursor=<cursor>)`.
τ's `steer` splits on whether that cursor is running a turn:

| State of the cursor when the result lands | What `steer` does |
|---|---|
| A turn is in flight | Queued on that cursor. The loop drains it immediately before its next provider call, after the current tool results. |
| No turn in flight | It takes that cursor's turn slot and runs an ordinary turn. This is the wake-up. |

Which cursor, and what happens when the queue does not keep its promise, is §6.

`api.submit` stamps `source="extension"` and `submitter=<the extension's name>`,
and neither is a parameter, so the attribution cannot be forged. `correlation`
carries `background_handle`, `tool`, `args`, `started_at_ms`, `origin_session_id`,
`origin_cursor_id`, `origin_leaf`, `route`, `held_over` and `crossed_session`.
**A host may receive an `agent_end` for a turn it never prompted**, on a cursor it
may not be watching. That is the one thing an integrator has to be told.

## 5. Submission depth, which is a trap

`DRIVING_SUBMISSION_DEPTH` is a `ContextVar`. A task spawned inside a tool's
`execute` inherits it, so without intervention the depth climbs one per completion
that triggers another call, and the eleventh submission raises against
`MAX_SUBMISSION_DEPTH = 10` and kills the session. A backgrounded call is not
self-submission, so `BackgroundRegistry._supervise` sets the variable to `None` in
the spawned task. The chain test drives `MAX_SUBMISSION_DEPTH + 1` links and
asserts depth 0 inside each one.

## 6. A result goes to the cursor that issued it

Since docs/CURSORS.md every writer is a cursor, and a session can hold several:
the head's, a sub-agent's, one per client under `tau serve`. Each job records the
cursor of the turn that issued it (`ctx.cursor`, which inside a tool is the
running turn's, through `TURN_CURSOR`) and that cursor's leaf. Before 2026-10-05
`api.submit` took no cursor, so every result went to the head's cursor, whoever
asked (measured: issued on a second cursor, delivered on the head's path, 0 on
the issuer's).

| The issuing cursor at completion | Where the result goes | Label |
|---|---|---|
| Live, the call on its path | that cursor: a steer if busy, a turn if idle | `[background bg-x-1] x finished:` |
| Live, moved off the call's branch | that cursor | "started on a branch this conversation has since left (at entry …)" |
| Closed, a live cursor up its owner chain (a finished sub-agent) | the nearest live owner | "from closed cursor <id> (<label>)" |
| Closed, its tree is not the head's (the head swapped trees) | the head's cursor | "from an earlier session" (§6.1) |
| Closed, no live owner, same tree as the head (a client closed it) | **parked**: kept in the registry, reported with `ui.notify`, returned by `bg` | — |
| A `bg wait` was waiting when it finished | returned by that tool call, not delivered as a message | — |
| Cancelled by `bg cancel` or shutdown | nowhere | — |

**Nothing is delivered elsewhere unlabelled, and nothing is dropped.** The parked
row is the one place no message is sent: closing a cursor says nobody consumes
that position any more, and picking another client's cursor would be choosing a
recipient at random. Parking is an open decision (RESULTS.md).

### 6.1 A result outlives its session, labelled

Answered 2026-09-29 (Kevin Question Log, Q21): when the conversation ends while
work is outstanding, **deliver the result into the new one, labelled with where it
came from.** Waiting holds a session swap hostage to a slow tool; cancelling
throws away work the model asked for. Under cursors this is the "tree is not the
head's" row. A swap retires the old head cursor, so that row is what a swap
produces.

The first version's check never fired on a real swap: it kept an epoch bumped on
`session_start`/`session_shutdown`, and `new_session`, `fork` and `switch_session`
fire only `session_before_switch`. Its tests passed because they modelled a swap by
calling the shutdown hook. `TestRealSessionBoundary` drives a real
`AgentSessionRuntime.new_session`.

Outstanding work is cancelled only on `session_shutdown`, which fires on quit,
disable and reload.

### 6.2 The steer queue is followed, not trusted

A queued steer used to be lost three ways, none of them reported:

- `abort()` clears the cursor's steer queue, including the one a swap sends a busy
  cursor;
- `rollback` clears it, and also moves the leaf off a turn that already read it;
- **a turn that ends between the steer's busy check and its append** strands it:
  the steer stays queued on an idle cursor until something else starts a turn.
  This needs no hook. A result that lands after a turn's last provider call and
  before its lock is released does it.

Measured on the pre-2026-10-05 module (500 seeded runs, 12 results each, every
run aborting or rolling back a busy head turn): 2,337 of 6,000 results never
reached the head's path, and 5 were left stranded.

`CursorDelivery` therefore follows a queued result. It waits until the target
cursor's turn lock is free and no FIFO waiter takes it, then:

1. still in the steer queue → it was stranded; take it out and submit again (a
   turn, since the cursor is idle);
2. on the cursor's path → delivered;
3. otherwise it was cleared or rolled off. If the cursor is live, the result goes
   onto its `next_turn_queue` labelled "held over", so it rides the next prompt
   rather than starting a turn the user just stopped; if the cursor closed
   meanwhile, it is routed again by the table.

Same harness after the change: 6,000 of 6,000 on the path exactly once, none
stranded. Matching is by the delivery's text prefix, `[background <handle>]` or
`[background <handle>,`; an `input` hook that rewrites that prefix defeats it.

This keeps `abort` semantics that tectum relies on (it still clears the queue),
which is why the fix is in the extension and not in core. The general core fix,
provenance on queue items and a reported clear, is still the ROADMAP item.

## 7. The `bg` tool

`register_backgrounding` also registers `bg(action, handle?, timeout_s?)`:

- `status` lists every job (handle, tool, state, issuing cursor, age, note), or one.
- `wait` blocks up to `timeout_s` (default 30, at most 600) and returns the result.
  A result that finishes while a `wait` is waiting is **claimed**: it is not also
  delivered as a message. One that had already finished is returned with its
  delivery state.
- `cancel` trips the job's own signal and cancels its task. A job whose work
  finished is not cancelled, because its delivery is under way.

A job runs under its own `AbortSignal`, not the issuing turn's, and gets no
`on_update`. Before this a backgrounded `bash` was killed by an abort of the turn
that started it, and survived an abort of any later turn. The registry keeps every
job for the session's life; there is no eviction yet.

## 8. Thread safety

Every job is an `asyncio.Task` on the session's loop. The wrapped tool may hop to
a worker thread (`read`, `grep`, `find`, `edit`, `write` use `asyncio.to_thread`;
`bash` is an asyncio subprocess), but it returns to the loop before the registry,
a cursor's queues or the log are touched. The only object shared with a worker
thread is the job's `AbortSignal`, which holds a `threading.Lock`.

So the registry is **loop-confined**, and `BackgroundRegistry` enforces it:
`start`, `wait` and `cancel` raise from any other running loop. That matters on
free-threaded CPython. Measured: eight threads minting 50,000 handles each through
the unguarded counter produced 0 duplicates on 3.11 with the GIL and about 330,000
duplicates on 3.14t with the GIL off.

Under the GIL the races are interleavings at `await` points, not data races, and
§6.2 lists the ones found. A wrapped tool whose `execute` is synchronous runs on
the loop thread and blocks it, so electing `background` does not help it; that is
not detected.

## 9. What this does not do

- **Wrapping another extension's tool needs its definition.** `register_tool`
  raises on a duplicate name (H3), and `api.get_all_tools()` returns `ToolInfo`
  without the executable. `definitions=` takes the definition directly, which
  works when the two extensions are written together. A public way to look up
  another extension's tool is proposed separately. It would extend the command
  namespace (`docs/EXTENSION-NAMESPACE.md` §8 left tools open).
- **Two wrappers on one tool is not expressible**, for the same H3 reason.
- **No head renders the handle.** The provenance rides `details` and
  `correlation`, and nothing displays either yet.
- **One live run only.** 2026-10-05, `claude-haiku-4-5`: issued on a second
  cursor, `sleep 5; echo …` was backgrounded, the turn ended in 2.4 s, and the
  result woke that cursor 5 s later; the head's path was untouched. Everything else
  is proven against a real `AgentSession` with a stubbed provider.
- **Not tried under `tau serve` end to end.** The routing reads only the session's
  cursor registry, which the daemon shares, but no client drove it.
- **A held-over result is lost if its cursor is closed before its next turn.**
  `close_cursor` does not check `next_turn_queue` (core); a swap does deliver it.
- **The registry is not persisted.** A process exit loses running jobs and parked
  results.
- **`submit_threadsafe` takes no cursor.** Only `submit` gained one.
