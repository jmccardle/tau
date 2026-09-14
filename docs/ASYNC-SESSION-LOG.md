# The SessionLog protocol becomes async

**Status: built 2026-09-14.** This is the Option B that
`docs/BLOCKING-PERSISTENCE.md` §3 recommended on 2026-08-28 and did not build.
Read that document first: it holds the measurement, the symptom, and why options
A and C were rejected. This one holds what the change actually reached, the
three consequences it forced that §3 did not anticipate, and what breaks for
whom.

---

## 1. What changed

The eight `SessionLog` appenders are coroutines:

| Member | Before | After |
|---|---|---|
| `append_message` | `def` | `async def` |
| `append_custom_message` | `def` | `async def` |
| `append_custom_entry` | `def` | `async def` |
| `append_compaction` | `def` | `async def` |
| `append_elide` | `def` | `async def` |
| `append_navigate` | `def` | `async def` |
| `append_branch_summary` | `def` | `async def` |
| `append_at` | `def` | `async def` |
| `id` / `cursor` / `entries()` | `def` | **unchanged** |

The three reads stay synchronous. Every shipped store already holds its entries
in memory, and making `entries()` a coroutine would push `await` into
`ConversationTree` and into every caller that merely inspects a session.

`append_model_change`, `append_thinking_change` and `append_session_info` also
stay synchronous. They are not on the Protocol — `AgentSession` never calls them
— and they run inside `Session.create`, which has no loop to await on.

## 2. An async signature is not by itself a thread hop

Saying `async def` moves nothing off the event loop on its own. Each store meets
the contract its own way, and the three shipped ones answer differently:

| Store | How it satisfies `async` | Why |
|---|---|---|
| `InMemorySessionLog` | runs to completion, awaits nothing | RAM; there is nothing to wait for |
| `Session` (file) | runs to completion, awaits nothing | one buffered append to a local file; `BLOCKING-PERSISTENCE.md` §4 measured this freeze as invisible, and a thread hop per entry costs about what the write does |
| `JmftsSessionLog` | `await asyncio.to_thread(self._append_now, …)` | a synchronous `httpx` POST per entry — the freeze §1 measured |

This split is the load-bearing part of the design, and it is the answer to §2's
objection. §2 rejected `to_thread` at the CALL SITES because that would have made
thread-safety a new, unstated requirement of every `SessionLog` implementor,
including ones outside this tree. Here the thread hop is inside one store that
owns its own transport, and the three facts that make it safe are local to that
store: `httpx.Client` is documented thread-safe, the Protocol's stated
precondition gives a conversation exactly one writing process, and each caller
awaits one append before issuing the next, so the in-memory mirror
`_append_now` maintains is touched by one thread at a time. None of the three
holds for a foreign implementor — which is precisely why the obligation is this
store's and not the contract's.

Each store keeps a private synchronous core (`_append_at_now` / `_append_now`).
The coroutine is a wrapper over it. That is what lets `_init_state` write a new
session's opening entries from a synchronous `create`, where there may be no
running loop at all.

## 3. Three consequences §3 did not name

### 3.1 Three public `ExtensionAPI` methods break too

`BLOCKING-PERSISTENCE.md` §3 listed five reaches and a release note. It did not
list the extension API, and that is the larger break: `SessionLog` implementors
outside this tree are hypothetical, while extension authors are the documented
audience of `examples/`.

`api.append_entry`, `api.send_message` and `api.request_user_action` append, so
all three are now coroutines. An extension that calls one must `await` it. All
three were already reachable only from hook handlers, which are already
coroutines, so the change at a call site is one keyword.

### 3.2 The construction-time `agent_spec` is now queued, not written

`AgentSession._record_agent_spec` (W2) ran from `__init__` and from `set_model`.
Both are ordinary functions that no head can await, and a Python constructor
cannot be one. So the method now BUILDS the record and appends it to
`_pending_agent_specs`; `AgentSession._flush_pending_agent_specs` writes the
queue, at the top of every method that appends and before that method's own
first write.

**A list, not one slot.** The first draft held one pending record and the
existing `test_set_model_appends_a_second_agent_spec_record` caught it: a session
constructed, re-modelled and then prompted wrote ONE record where it used to
write two, because the swap overwrote the constructor's. Each `set_model` is a
separate swap and each record is a separate answer to "what was the frame here",
so dropping one is the swallowed gap the Fail-Early rule names — in the node
whose whole job is to say what the frame was.

Relative order is unchanged — the `agent_spec` records still precede the turn
they describe, in the order they were recorded. What changed is when they become
durable: the first turn, rather than the constructor. `AgentSession.start()` is
the awaited door for a caller that reads the tree before it prompts, and
`AgentSessionRuntime._apply_swap` awaits it after a `new_session` / `fork` /
`switch_session`, which is the one path that hands a host a tree it has not
appended to. `start` is triaged `NOT_EXPOSED` in the RPC capability audit for
that reason: the server has already called it.

**The queue belongs to `AgentSession`, not to the log.** Appending straight to
`session.session_log` drains nothing. That is deliberate — the log is a
storage seam and knows nothing about provenance — but it is a sharp edge for a
test or a tool that writes through the log directly.

Three alternatives were considered and rejected:

- **Drive the coroutine to completion from `__init__`** (`coro.send(None)`, or
  `asyncio.run`). Works for the two stores that never suspend and raises for the
  JMFTS store, so constructing a session against a shipped store would fail.
  Fail-Early does not extend to manufacturing a failure (CLAUDE.md's own reading
  of the rule).
- **Make `__init__` async.** Not a thing.
- **Make `set_model` async and move the constructor's record into an async
  factory.** `create_agent_session` is a synchronous public SDK entry point with
  40 examples behind it; `set_model` is reached from the RPC verb and from
  `ctx.set_model`. Two more public breaks to avoid one slot.

### 3.3 The RPC cursor invariant has to be re-established, not inherited

`RPCHandler._stamp_agent_end_cursor` reads the session cursor at DEQUEUE time
and relies on the writer task not being scheduled until the turn's persistence
has finished. `handler.py:579-587` states the guarantee it depends on: no
`await` between `_forward_event`'s `put_nowait` and `_persist_loop_messages`.

That window is still await-free. The guarantee is nevertheless weaker than it
was, because persistence itself can now suspend part-way through: each append is
awaited, and a store that really suspends hands the loop back mid-turn. For
`InMemorySessionLog` and the file store nothing changes — a coroutine that
awaits nothing never yields — but `JmftsSessionLog` thread-hops per append, so
the writer CAN be scheduled between two of a turn's appends.

`test_agent_end_wire_event_carries_the_post_persistence_cursor` cannot ask that
question: it runs on `InMemorySessionLog`, whose appends do not yield, so it
passes whatever the ordering is. That is the "gate that cannot fail" ROADMAP.md
recorded on 2026-09-13 after the reverted `to_thread` attempt.
`test_agent_end_cursor_is_still_post_persistence_when_appends_suspend` is the
gate it asked for: an `InMemorySessionLog` subclass that sleeps 20 ms per append,
driven through the same real `_write_stdout` path.

**Measured, and it failed.** With a suspending store the wire carried
`cursor: null` where the post-turn tip belonged. Two sizes were tried and the
difference matters for anyone writing a test like this: `asyncio.sleep(0)` per
append does NOT reproduce it — one loop iteration is not enough for the writer
task to be scheduled, dequeue and write — and it leaves both tests green even
with an `await asyncio.sleep(0)` injected straight into `_run_one_turn` between
the enqueue and persistence. 0.2 s fails both. The suspension has to be a
plausible network round trip or the test is theatre.

**The fix states the ordering instead of inheriting it.**
`AgentSession.persistence_settled` is an `asyncio.Event`, cleared before
`loop.run` (which is what emits `agent_end`) and set in a `finally` after both
persistence calls, on the error path as well.
`RPCHandler.await_outbound_prerequisites` — a new async pre-step the writer runs
before the synchronous `prepare_outbound` — waits on it for an `agent_end`, and
only when the item's captured `_cursor_log` is still the session's live log (a
swap that landed since holds `turn_lock`, so that turn is already persisted, and
waiting on the current session would be waiting on an unrelated turn).

`prepare_outbound` stays synchronous on purpose: its contract is that it cannot
suspend between reading the cursor and framing it. The awaiting half is a
separate method that runs first. It blocks the whole writer for as long as
persistence takes, which is not a new cost — the queue is FIFO, so everything
behind an `agent_end` was already behind it. It cannot deadlock against the T3
credit pool, because persistence emits no events of its own and therefore needs
no credit that the blocked writer would have to release.

## 4. What a fork has to do

A `SessionLog` implementor outside this tree:

1. add `async` to the eight appenders;
2. decide, per §2, whether the implementation needs a thread hop or merely a
   coroutine signature;
3. re-run `tau_agent_core.testing.session_log_contract`, whose tests are now
   `async def`. The suite needs `asyncio_mode = "auto"` in the fork's pytest
   config, or a `@pytest.mark.asyncio` on each case.

An extension author: `await` the three `ExtensionAPI` methods in §3.1.

**The synchronous core is not an implementation detail — it is part of the
port.** `SessionCatalog.create`, `create_ephemeral` and `fork` are NOT coroutines
on the ABC or in either shipped store, and they all write entries. So does a
scene fixture, and so does any test double standing in for a catalog. Every one
of those needs a way to write without an `await`, and in this tree that is the
store's own `_append_now`. Four test doubles and one fixture were rewritten that
way during this change; the alternative each had reached for first — making
`create`/`fork` a coroutine — breaks the ABC, and `asyncio.run` fails outright
where a caller is already inside a loop (`scenes.stage_scene` is entered from
`open_scene`, which is async).

So a store that implements this Protocol should keep its write primitive
callable from both worlds, and the sync half should be the one that does the
work.

## 5. One thing it fixed by accident

`docs/library/reference/` rendered every coroutine with the same signature line
as an ordinary function, because `docs_build._signature` never asked. Adding the
`async` prefix (read from griffe's labels, which carry it) changed **48 entries**
across the reference — and only eight of them are this change's own appenders.
The other forty are `prompt`, `submit`, `run`, `stream_chat`, `emit`,
`load_extensions` and the rest: coroutines the reference has been documenting as
callable without `await` since it was generated.

That was worth fixing here rather than filing, because this change's whole
subject is which methods must be awaited, and a reference that cannot say so
would have shipped a new lie alongside the old ones.

## 6. What is still not measured

`BLOCKING-PERSISTENCE.md` §5 recorded that no benchmark of the freeze had been
run, and said doing one is the first step of this unit. It was not run. The
change is justified by the source — the appends are synchronous HTTP in a
coroutine on the head's loop — and not by a number, and this document should not
pretend otherwise. The verifier that caught the earlier `to_thread` attempt
(2026-09-13) reproduced the RPC cursor regression by slowing an in-memory append
to 2 ms, which is evidence about ordering, not about the freeze's size.

## 7. Cross-references

* `docs/BLOCKING-PERSISTENCE.md` — the analysis this builds; §2 is the argument
  §2 above answers.
* `docs/SESSION-TREE-IMPLEMENTATION.md` — the `SessionLog` design.
* `docs/NODE-ADDRESSABLE-AGENTS.md` — Decision 6, the one-writer precondition
  §2 above leans on.
