# Extensions walkthrough — a composed plan → implement → evaluate run

> **Capstone narrative for the E0 → E4 chain.** This doc has **no new code**: it
> ties the five shipped `examples/` demos together into one story — how a
> gatekeeper veto, a reminder bank, and a budget guard *wrap* a delegate-driven
> plan → implement → evaluate loop, with the context surgeon as the agent's own
> hand on the conversation. It is the reader's map from the per-demo docstrings
> (each demo is self-documenting) to the **method** they collectively implement:
> pi's `docs/pi_planning_implementing_evaluating.md` and
> `docs/pi_orchestration_patterns.md`. The mechanism each demo rides is specified
> in `docs/EXTENSIONS-IMPLEMENTATION.md` (§8 steps S9, S15–S17, S22); the
> extension API those steps wired is in `docs/extensions.md`.

The one idea underneath all of it (pi's planning doc, §opening): **message
injection is feedback control.** The reference signal is the spec / frozen
interface / failing test; you observe state through the agent's lifecycle events;
when error crosses a threshold you either *steer* (inject a corrective message) or
*interlock* (veto the action outright). The five demos are the three controller
types plus the machinery they govern:

| Demo file | Entry point | Plane | Controller role |
|---|---|---|---|
| `examples/20_delegate.py` | `delegate_extension` | orchestration | the **plant** — spawns isolated `tau -p` subagents that do the work |
| `examples/22_gatekeeper.py` | `gatekeeper_extension` | agent-facing | **hard interlock** — a `tool_call` veto (fail-CLOSED) |
| `examples/21_reminders.py` | `reminders_extension` | agent-facing | **steering** — bang-bang `<system-reminder>` injection with cooldowns |
| `examples/24_budget.py` | `budget_extension` (`make_budget_extension`) | agent-facing | **cost ceiling** — warn-then-abort on running spend |
| `examples/23_context_surgeon.py` | `context_surgeon_extension` | agent-facing | **session control** — agent-callable compact / summarize / fork |

The tests that prove each demo behaves are `tau-agent-core/tests/test_gatekeeper.py`,
`tau-agent-core/tests/test_reminders.py`, and `tau-agent-core/tests/test_budget.py`
(the delegate and surgeon are exercised as headless smoke tests per S9/S22).

---

## The scenario

You are pointing the agent at a small feature: *implement it to a green test
suite, without letting it cheat, sprawl, or run up an unbounded bill.* This is
exactly the pipeline pi's planning doc lays out — big models do bounded judgment
on a distilled brief, small models do mechanical fill inside a frozen interface,
and the loop is fenced so "make the tests pass" cannot degenerate into reward
hacking (hardcoding, editing the tests, breaking the grader).

The agent that *drives* the pipeline is a single session. It does not do the work
itself; it **delegates** each stage to a fresh subagent with the right-sized model,
reads the structured result back, and moves on. The three guard extensions ride on
that one driving session and constrain everything the driver and its children can
do. When the driver needs to reshape its *own* growing context to keep going, it
reaches for the surgeon's tools.

### The stage → model routing (pi planning doc §1)

The delegate demo is what makes per-stage model routing real: each `delegate` call
names a `--model`, so the driver can send architecture and the frozen-interface
freeze to a big model, the test suite to a medium model, and the make-green grind
to a small/local model — without ever loading three models into one context.

| Stage | Delegated to (model tier) | Hard constraint carried into the child |
|---|---|---|
| Architecture + library choice | big (`--model` = plan tier) | fed the brief only; `--tools` empty / read-only; one short session |
| Frozen interface (stubs + types) | big | signatures + docstrings only, no bodies |
| Acceptance criteria + **test suite** | medium | ratified against the spec; tests read-only afterward |
| Implement to green | small / local | mechanical fill inside the frozen interface |
| Make-green loop | small / local | capped rounds; **stop** on thrash, no takeover |
| Code review | medium | read-only |
| Consult (only on thrash) | big | hermetic (`--no-tools`), tiny budget, diagnosis not takeover |

---

## How one turn flows through all five demos

Compose the run by handing the five entry points to `create_agent_session(...,
extensions=[...])` (the SDK signature in `tau-agent-core/.../sdk.py`), or drop the
modules into `~/.tau/extensions` aliasing each entry point to `register`. Order
matters only for the mutating hooks, which chain in load order (E2 semantics,
`docs/EXTENSIONS-IMPLEMENTATION.md` E2). The guards register *first* so their veto
and steering wrap every subsequent tool call:

```text
gatekeeper_extension        # tool_call veto  (interlock — must run first)
reminders_extension         # tool_call / tool_result / context  (steering)
budget_extension            # message_end / context  (cost ceiling)
delegate_extension          # registers the `delegate` tool  (the plant)
context_surgeon_extension   # registers compact_now / summarize_history / fork_session
```

Now trace a single driving turn — the agent decides to spawn the "implement to
green" stage — through each seam it touches:

1. **The driver calls `delegate`.** The `delegate` tool (`examples/20_delegate.py`,
   registering `DELEGATE_TOOL`) is about to spawn a child. Before it executes, the
   loop runs the `tool_call` hook chain.

2. **Gatekeeper inspects the call (interlock).** `gatekeeper_tool_call`
   (`examples/22_gatekeeper.py`) sees the delegate — but the gatekeeper's teeth are
   really for the *filesystem* tools the driver and, transitively, the children can
   reach. A `write` / `edit` whose `path` does not resolve under a prefix in
   `.tau/scope.txt` is denied; any `read` / `ls` / `grep` / `find` into
   `tests_heldout/`, or a `bash` command naming `tests_heldout`, is denied. The
   hook returns `{"block": True, "reason": ...}` and the loop converts it into an
   error tool result whose text is exactly `reason` (fail-CLOSED: an absent or empty
   scope file denies *every* write — the gate exists to refuse undeclared writes,
   not to wave them through). This is the hard interlock from pi planning doc §3:
   the guarantee against cheating that reminders alone cannot give.

3. **Reminders update their state and may steer.** `ReminderBank`
   (`examples/21_reminders.py`) advances on `tool_call` / `tool_result` and, on the
   next `context` call (which fires before *every* LLM request), drains any tripped
   rule into an ephemeral `<system-reminder>`. Its four rules with cooldowns
   `3 / 4 / 2 / 1` are pi's coding-discipline bank (`pi_planning_implementing_\
evaluating.md §2`): **tests-readonly** (don't edit the oracle),
   **root-cause-after-2-failures** (the same tool erred twice — diagnose, don't
   retry), **scope-guard** (an edit resolving outside `ctx.cwd`), and
   **no-new-deps** (an installer `bash` command or a write to a dependency
   manifest). Reminders *steer*; they do not *block* — that division of labour is
   the whole point of pairing them with the gatekeeper.

4. **The child runs, hermetically.** The delegate spawns a separate process:
   `tau -p --mode json --no-session --no-extensions [--model M] [--tools ...]
   [--append-system-prompt <tmp>] "Task: ..."`. `--no-extensions` is what makes
   recursion safe — a child never re-loads `delegate` and cannot fork-bomb. In
   **parallel** mode (`MAX_PARALLEL_TASKS = 8`, `MAX_CONCURRENCY = 4`,
   `PER_TASK_OUTPUT_CAP = 50 KB`) `_guard_parallel_tools` *raises* on any
   write-classified tool (`WRITE_TOOLS = {write, edit, bash}`) — a hard code-guard,
   never a silent strip (Fail-Early, plan decision D-parallel). Per-child limits
   (`max_turns` / `max_seconds` / `max_usd` / stuck-detection) are enforced by
   reading the child's `--mode json` stream live; a tripped limit kills the child
   and stamps a `stop_reason` from the `_STOP_REASON` taxonomy.

5. **Usage lands; the budget accumulates.** As the driving session's own
   completions return, `BudgetGuard` (`examples/24_budget.py`) accumulates on the
   notify `message_end` event. It runs in **USD mode** when a per-model `cost` block
   is supplied (`completion_cost_usd` = the collapsed pi `calculateCost`,
   `sum(price[k] / 1e6 * tokens[k])`) or **token mode** when no price is known — and
   it *raises* rather than fabricate a `$0` for an unpriced model (the subtle
   Fail-Early trap: a real free model and an unknown-cost model must read
   differently). On the **next** `context` call it sees the freshly added spend, and
   past the ceiling it injects a one-shot `<system-reminder>` warning and then calls
   `ctx.abort()` — stopping the loop at the next turn boundary. The delegate's own
   `max_usd` fences each *child*; the budget guard fences the *driver's whole run*.

6. **The context hook fires (shared seam).** Both reminders and the budget guard
   are `context`-hook consumers, so on every LLM call the driver's outgoing payload
   is the place tripped rules and the budget warning are injected — on a deep copy,
   before the request goes out (E2 `context` semantics). This is the single seam
   where all steering becomes visible on the wire.

7. **The driver reshapes its own context (the surgeon).** After several delegated
   stages the driver's transcript is long. It calls one of the surgeon's tools
   (`examples/23_context_surgeon.py`): **`compact_now`** records a *turn-deferred*
   compaction (a tool cannot compact mid-loop, so `ctx.compact(defer=True)` applies
   exactly once at the tail of `prompt()`, the same drain site as auto-compaction);
   **`summarize_history(from_entry)`** appends a `branch_summary` so abandoned
   siblings drop out of context; **`fork_session(entry_id)`** exports the
   conversation to a new session file via `ctx.fork(mode="export")` and can hand the
   follow-up to a *fresh* delegate while the fork preserves the branch point. These
   agent-callable mutation tools are only safe **because** the gatekeeper veto
   (step 2) fences what the tools — and the delegate children they spawn — can
   touch; that is exactly why `examples/23_context_surgeon.py` re-exports the demo-22
   hook as `context_surgeon_gatekeeper` and tells you to wire the two together
   (plan decision 2: extensions may expose fork/compact *as tools* precisely
   because E2 is the safety).

---

## Why the composition holds together

- **Two planes, cleanly split** (`pi_orchestration_patterns.md §the two planes`).
  The delegate is the *orchestration plane* — subprocesses metered from outside.
  The gatekeeper, reminders, budget, and surgeon are the *agent-facing plane* —
  thin shims the running agent triggers itself. Neither reaches into the other's
  job.

- **Steering vs. interlock are not interchangeable.** Reminders drift-correct a
  small model that wanders (they *nag once, then cool down*); the gatekeeper is the
  hard guarantee that a wander cannot become a cheat (edit the tests, escape scope,
  read the held-out set). pi's planning doc is explicit that access controls — not
  reminders — cut reward hacking to near zero; τ makes that a `tool_call` veto.

- **Cost is fenced twice, at two scales.** `max_usd` per delegated child bounds a
  single stage; `BudgetGuard` bounds the driver's entire run and can abort it. A
  runaway make-green loop hits the child limit; a runaway *driver* hits the budget.

- **The surgeon closes the loop.** A long plan → implement → evaluate run outgrows
  its context; the surgeon lets the *agent itself* compact, summarize, and fork
  without a human — the payoff of exposing E3-ctx session control as
  model-callable tools, made acceptable only by the veto standing in front of it.

---

## Running the demos

Each demo module is self-contained and self-documented; read its top-of-file
docstring for the exact field contract and constants. The composed run is just the
five entry points registered on one session (see the wiring block above); the
per-demo tests (`test_gatekeeper.py`, `test_reminders.py`, `test_budget.py`) show
each guard firing in isolation against the `fake_llm` harness, and the delegate and
surgeon smoke tests spawn real `tau -p` children against the in-repo fake provider.
Start from any single demo, then add the next guard and watch the composed
behaviour — the seams are additive by construction.
