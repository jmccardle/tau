Experiment record (2026-10-05). Branch `exp/background-cursors`, ported from
`feat/background-tools` onto master `7303954`. Design record:
`docs/BACKGROUND-TOOLS.md` (revised here).

# Background tools under cursors

John's questions: "Can the background task tool be updated for cursor
compatibility? Are there going to be thread safety issues with that
functionality?"

**Yes, and it is built.** A job now remembers the cursor whose turn issued it, and
its result goes back to that cursor. A model-facing `bg` tool adds status, wait and
cancel. Thread safety holds because the registry is confined to one event loop,
and the registry now enforces that. The defects that mattered were not thread
races. They were three ways the steer queue lost a result under the GIL, and one
misroute. All four are fixed in the extension, without changing `abort` semantics.

Claims are marked **ran**, **read**, **inferred** or **assumed**.

## 1. The port

`git rebase master` of `c19fcbb` and `67b87fb`:

| Conflict / moved API | Resolution |
|---|---|
| `examples/ext_kit/__init__.py`, both commits: master renamed `active_cursor` to `active_path` (`9b0095d`, the Cursor refactor), and the branch edited the same `__all__` lines | Took master's file. The second commit moves backgrounding out of `ext_kit` anyway |
| `AgentSession._pending_steer_messages` is gone; the steer queue is `Cursor.steer_queue`, one per cursor | Test updated (the pinned-loss test was the only reader) |
| `api.submit` acts on `AgentSession.cursor`, the head's, not on `TURN_CURSOR` | This is the misroute, §3 |
| A swap now calls `deliver_queued(old)` (CURSORS.md §8), but aborts a busy cursor first, which clears its steers | Still lost on the port; fixed by §4 |

After the textual rebase, 31 of 32 branch tests passed unchanged (**ran**). The
one failure read the removed attribute. Rebased shas: `f655e9b`, `dd0595a`.

## 2. What changed

Core (`extension_types.py`), two additions:

- `ExtensionAPI.submit(..., cursor=None)` passes `cursor` to `AgentSession.submit`.
  `None` keeps the old behaviour, the head's cursor.
- `ExtensionContext.cursors`, the live cursors, so an extension can tell whether a
  cursor it holds has been closed.

Extension (`extensions_builtin/background.py`):

- `BackgroundCall` records `origin_cursor_id`, `origin_leaf`, the cursor object,
  a state, and the result. It keeps its own `AbortSignal`.
- `CursorDelivery` routes each result (§3) and follows a queued one until it is
  settled (§4).
- The `bg` tool: status, wait, cancel.
- A backgrounded call runs under the job's own signal and gets no `on_update`.
  Before this, an abort of the turn that started a backgrounded `bash` killed
  it, while an abort of any later turn did not (**read**: `bash.py` polls the
  signal it is handed, and each turn gets a fresh one). Its progress updates also
  went to a tool call that had already returned.
- A tool that raises is delivered as a failed result, which is what a blocking
  call shows. Before this, the exception went to `on_error`, which by default
  re-raised inside the task, where asyncio logs it and nobody sees it.
- A job's state and result are kept after it finishes, so `bg` can report it.
- Registry methods raise when called from a second event loop.

## 3. Delivery semantics

| Issuing cursor at completion | Goes to | Label | Test |
|---|---|---|---|
| Live, idle, call on its path | that cursor; starts a turn | `[background bg-x-1] x finished:` | `test_an_idle_issuer_is_woken_and_the_head_is_untouched` |
| Live, busy | that cursor; steer, same turn | same | `test_a_busy_issuer_gets_it_as_a_steer_in_the_same_turn` |
| Live, moved off the call's branch | that cursor | "…started on a branch this conversation has since left (at entry E)…" | `test_a_moved_issuer_is_told_the_call_is_not_on_its_branch` |
| Closed sub-agent, live owner | nearest live owner | "…from closed cursor C (label)…" | `test_a_finished_sub_agent_routes_to_its_live_owner_labelled` |
| Closed, its tree is not the head's (a swap) | head's cursor | "…from an earlier session…" (Q21) | `test_a_result_landing_after_new_session_is_delivered_there_labelled` |
| Closed, no live owner, same tree | **parked**: registry, `ui.notify` warning, `bg wait` returns it | — | `test_a_closed_unowned_cursor_parks_reports_and_bg_returns_it` |
| Queued, then `abort` | same cursor's `next_turn_queue` ("held over"); rides the next prompt | "(held over: …)" | `test_abort_while_queued_holds_it_over_to_the_next_prompt` |
| Queued, then `rollback` | held over, or delivered by the rollback's turn | same | `test_rollback_while_queued_does_not_lose_it` |
| Queued, then a busy swap | old cursor's next turn (`deliver_queued` runs it on the old tree), or the new head labelled; which one depends on timing | as routed | `test_a_result_queued_into_a_turn_the_swap_aborts_survives` |
| Queued after the turn's last provider call (stranded) | taken out of the queue and re-submitted as a turn | plain | `test_a_turn_ending_inside_the_input_hook_does_not_strand_the_steer` |
| A `bg wait` was waiting | that tool result only | — | `test_wait_returns_the_result_and_claims_it` |
| `bg cancel`, shutdown | nowhere; the job's signal is tripped | — | `test_cancel_trips_the_jobs_own_signal_and_delivers_nothing` |

Misroute before the change (**ran**, `misroute.py`): a job issued on a second
cursor was delivered to the head's path (1) and not to the issuer's (0). After:
issuer 1, head 0.

## 4. Thread safety

**Where jobs run (read).** Each job is an `asyncio.Task` on the session's loop,
created inside the tool call. It therefore inherits `TURN_CURSOR`, which is how
the issuing cursor is known. Wrapped tools may use worker threads: `read`,
`grep`, `find`, `edit` and `write` call `asyncio.to_thread`. `bash` uses
`asyncio.create_subprocess_shell` and needs no thread. Every one of them returns
to the loop before the registry, a cursor's queues or the log are touched. The
only object a worker thread shares is `AbortSignal`, which takes a
`threading.Lock`. Session-log appends happen on the loop, through
`Cursor.append_at`. CURSORS.md §3 makes every store safe under concurrent async
appends, and the daemon is the single writer.

**(a) GIL build (3.11.2).** No data races, because nothing leaves the loop. The
real races are interleavings at `await` points, and four were found (**ran**):

1. **Misroute**: `api.submit` had no cursor (§3).
2. **`abort` clears a queued result.**
3. **`rollback` clears it**, or moves the leaf off the turn that read it.
4. **Lost wake-up.** A result arriving after the turn's last provider call, but
   before the turn lock is released, stays in the steer queue of an idle cursor
   until a human types. No hook is needed for this. An awaiting `input` hook
   widens the window, and the deterministic test uses one.

The concurrency test is `TestStress` in `test_background_cursors.py`, 25 seeds in
the suite, and `stress.py` for long runs. Each run starts 12 jobs, holds a head
turn busy, and completes the jobs at random points while the turn is aborted or
rolled back at a random point. It then counts each result on the head's path.

| Module | Python | Runs × results | On path once | Never arrived | Stranded |
|---|---|---|---|---|---|
| before (`dd0595a`) | 3.11.2, GIL | 500 × 12 | 3,663 | **2,337** | 5 |
| after | 3.11.2, GIL | 500 × 12 | **6,000** | 0 | 0 |
| before | 3.14.8t, GIL off | 300 × 12 | 2,198 | **1,402** | 5 |
| after | 3.14.8t, GIL off | 300 × 12 | **3,600** | 0 | 0 |

**(b) Free-threaded 3.14t (ran).** The suite's 74 background tests pass with
`-X gil=0`, and the stress numbers do not change, because everything still runs on
one thread. What free threading changes is the cost of breaking loop confinement.
`threads.py` makes eight threads mint 50,000 handles each through the registry's
unguarded counter:

| Python | Duplicate handles per 400,000 |
|---|---|
| 3.11.2, GIL | 0 (5 trials) |
| 3.14.8t, GIL off | 315,057 – 339,920 (5 trials) |

So the registry asserts its loop (`_check_loop`; `TestLoopAffinity`) instead of
taking locks. A lock would make it look thread-safe while a cursor's queues, which
it reads and writes, would still be loop-only.

**Not covered (inferred).** A wrapped tool whose `execute` is synchronous runs on
the loop thread, blocks it, and gets nothing from `background`; that is not
detected. `submit_threadsafe` has no `cursor=`.

## 5. Tests

| Run | Result |
|---|---|
| `test_background_extension.py` + `test_background_cursors.py`, 3.11.2 | 74 passed (**ran**) |
| same, 3.14.8t `-X gil=0` | 74 passed (**ran**) |
| Whole suite, `pytest -rf --tb=line` | 6,519 passed, 157 skipped, 6 deselected, 1 failed in 679 s. The failure was `test_agent_docs.py::test_checked_in_reference_is_current`: the run started before the reference docs were regenerated. Re-run after regenerating, together with `test_no_host_addresses.py` and both background files: 98 passed (**ran**) |
| Gates: ruff check, ruff format --check, mypy (5 trees, one call), comment-block scan | clean (**ran**) |

**Live (ran, claude-haiku-4-5, `live_haiku.py`).** On a second cursor (not the
head), Haiku was asked to background `sleep 5; echo done-marker-42`. It called
`bash` with `background: true` and got `bg-bash-1`, and the turn ended at 2.4 s.
At 7.4 s the result woke that cursor with `[background bg-bash-1] bash finished:
done-marker-42`, and Haiku answered with the marker. The head's path held only
the shared prefix. One observation about the model rather than the harness: in
its first reply Haiku wrote the marker before it had the result, guessing it from
the command. One run of three short model calls. The cost is
inferred at under one cent; tokens were not metered.

## 6. Open decisions for John

1. **Parking.** If the issuing cursor is closed, has no live owner, and is on the
   head's tree (a `tau serve` client closed its cursor), the result is parked
   and reported instead of sent to some other cursor. The alternative is to open
   a fresh cursor at the call's leaf and run an unattended turn there, which is
   what `deliver_queued` does for a swapped cursor.
2. **Abort means hold over, not wake.** A result cleared by `abort` rides the
   cursor's next prompt rather than starting a turn the user just stopped.
   Tectum's idle `abort` to drop a stranded utterance no longer drops a
   background result.
3. **A closed sub-agent's result goes to its owner, labelled,** rather than
   continuing the sub-agent's branch.
4. **Q21 is kept for a tree swap only.** Under `tau serve` there are no swaps, so
   "deliver into the current conversation" never picks among client cursors.
5. **Core vs extension.** Following the queue is extension code that knows how
   cursors work inside (the lock, the FIFO waiter, the queues). The general fix is
   still the ROADMAP item: provenance on queue items and a reported clear.

## 7. Remaining gaps

- `close_cursor` drops a non-empty `next_turn_queue` silently (core, **read**), so
  a held-over result is lost if its cursor is closed before it runs another turn.
- The registry is in memory only. A daemon restart loses running jobs and parked
  results.
- The registry never evicts finished jobs.
- Delivery is matched by text prefix. An `input` hook that rewrites
  `[background <handle>]` makes a delivered result look discarded, and it would be
  delivered twice.
- `tau serve` was not driven end to end. The routing uses only
  `AgentSession.cursors`, which the daemon shares (**inferred**).
- CODE-MODE.md §6.5 asked for model-facing await, poll and cancel before handles
  could outlive a script. `bg` is a proof of concept of all three. Claiming a
  result through `wait` is the semantics a code mode would need.
- Small, found on the way: `create_agent_session(model="claude-…",
  provider="anthropic")` builds a `Model` with `api="openai-completions"`, and
  the first request raises. The live script builds the `Model` by hand.
