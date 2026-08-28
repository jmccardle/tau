# Persistence blocks the UI thread, and the fix is a protocol change

**Status: analysed 2026-08-28, deliberately NOT built.** The two sibling items
from `docs/PLAN-0.9.4.md` §8 — the blocking tool call sites and repeat-tool-call
detection — are built (`69c5af2`, `244931c`). This one is not, because unlike
those it cannot be fixed inside τ without changing a contract other people
implement. This document records what was measured, what the options are, and
why the cheap one was rejected.

---

## 1. The symptom

The agent loop runs as an async Textual worker on the app's **own** event loop.
There is no thread between them. So anything synchronous the loop calls freezes
painting and input for exactly as long as it runs.

Five tool call sites had this problem and are now fixed. The sixth is
persistence, and it is the worst of the six for two reasons: it is the largest,
and it gets worse as turns get longer.

`AgentSession._persist_loop_messages` (`agent_session.py:3613`) is a plain
synchronous method. It loops over the turn's messages and calls
`self._session_log.append_message(...)` or `append_custom_message(...)` on each.
With the JMFTS-backed store each of those is a synchronous `httpx` POST. A
ten-tool turn produces roughly 21 messages, so it issues roughly 21 blocking HTTP
round-trips, back to back, on the thread that draws the screen.

`_persist_turn_inputs` has the same shape at the start of a turn, so the freeze
brackets the turn rather than only ending it.

## 2. Why this is not a three-line fix

All three callers of `_persist_loop_messages` are already `async`:

| Caller | Location |
|---|---|
| `AgentSession._run_one_turn` (error path) | `agent_session.py:2967` |
| `AgentSession._run_one_turn` (normal path) | `agent_session.py:2976` |
| `AgentSession.continue_conversation` | `agent_session.py:3203` |

So `await asyncio.to_thread(self._persist_loop_messages, ...)` compiles, is about
six lines including `_persist_turn_inputs`, and moves the freeze off the event
loop today.

It was rejected. `SessionLog` (`session_log.py:42`) is a `Protocol` whose fourteen
append methods are all synchronous, and it has implementors this repository does
not own:

* `InMemorySessionLog` (`session_log.py:340`) — the SDK default
* `BranchView` (`session_log.py:554`) — an in-memory routing wrapper
* `JmftsSessionLog` (`tau-jmfts/.../store.py`) and the catalog wrapper
  (`tau-jmfts/.../catalog.py`)
* anything outside this tree, which is the point of shipping
  `tau_agent_core.testing.session_log_contract` as a contract suite

Calling a synchronous protocol from a worker thread makes **thread-safety a new
requirement of every implementor**. Nothing in the protocol's docstring says so,
nothing in the contract suite checks it, and an implementor that holds an
un-guarded list or a single `httpx.Client` would go from correct to
intermittently wrong with no diagnostic and no version bump. That is a silent
contract change, which is the failure mode the repository's Fail Early rule names
directly. If the call is going to move onto a thread, the protocol has to say so
first — and once it says so, saying it as `async` is both clearer and cheaper for
implementors than saying "must be thread-safe".

## 3. The options, and what each costs

**Option A — `to_thread` the call sites.** ~6 lines. Unblocks the UI now.
Rejected: §2.

**Option B — async `SessionLog` protocol.** The honest fix. `append_message`,
`append_custom_message`, `append_custom_entry`, `append_compaction`,
`append_elide`, `append_navigate`, `append_branch_summary`, `append_at` and the
rest become `async`; `entries()`/`cursor`/`id` stay synchronous reads. Reaches:

1. the protocol (`session_log.py`)
2. `InMemorySessionLog` and `BranchView`
3. `JmftsSessionLog` and `tau-jmfts`'s catalog wrapper, which also need an async
   HTTP client rather than the current synchronous `JmftsClient`
4. `session_log_contract.py`, the published contract suite
5. every `append_*` call site in `agent_session.py`, `rpc/`, `compaction.py` and
   the TUI
6. a release note for external `SessionLog` implementors — this is a breaking
   change for them, not an additive one

**Option C — a write queue behind the session.** Persist by putting messages on
an `asyncio.Queue` that a single background task drains, so the loop never waits.
Keeps the synchronous protocol. Costs a real answer to "when is a turn durable?",
which today is "before `prompt()` returns" and would become "eventually" — and a
crash between the two loses the turn silently. That is a worse trade than the
freeze, unless the queue is drained-and-awaited at the turn boundary, at which
point it is Option B with extra machinery.

## 4. Recommendation

Option B, as its own unit, with the release note written before the code. It is
not a patch to slip into another cycle's commit; it is a breaking change to a
published contract, and the contract suite is what makes it safe to make once.

Until then the freeze is real and known. It is worst with the JMFTS store, which
is the store that does network I/O per append; with a local JSONL store the same
code path is a handful of buffered file writes and the freeze is not visible.

## 5. What is already measured

* The three blocking call sites, and the ~21 round-trips figure, come from the
  §2 investigation recorded in `docs/PLAN-0.9.4.md` §8. Note that §8 also
  attributes them correctly: they were found while investigating the streaming
  slowdown and are **not** its cause. The quadratic `Screen._refresh_layout`
  pass was, and that is fixed (`efae7af`).
* No separate benchmark of the persistence freeze has been run. Doing one is the
  first step of the unit, not a prerequisite for scheduling it — the blocking
  calls are visible in the source and their count scales with tool calls per turn.

## 6. Cross-references

* `docs/PLAN-0.9.4.md` §8 — where this debt is listed, alongside the two that
  are now built.
* `docs/SESSION-TREE-IMPLEMENTATION.md` — the `SessionLog` design.
* `tau_agent_core.testing.session_log_contract` — the contract suite an async
  protocol would have to be re-expressed through.
