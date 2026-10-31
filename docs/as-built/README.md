# As-built review of the core, Phase 3d

Master at `48248d7`, 2026-10-29. This page is the summary John signs off from.
The detail is in three sub-reports:

- [metrics/](metrics/README.md): size, complexity, coverage, dependencies, and the rule 7 ratchet
- [diagrams/](diagrams/README.md): the step graph, the refusal table, and the types, all generated from code
- [traces/](traces/README.md): one turn timed across Rust and Python ([summary.md](traces/summary.md))

`scripts/check.sh` was run on this commit and is green: rustfmt, clippy
(pedantic, `-D warnings`), `cargo test` (property tests included), rules,
ratchet ("9 allowed violations remain"), core IO check, stubtest, ruff,
pyright, and pytest (273 passed, 3 skipped). `scripts/diagrams.sh --check` is
green too ("up to date").

## 1. Verdict

**The core is in shape to build on.** It is small (7,100 lines of Rust against
99,000 lines of τ 0.15 source). The complexity tail sits in 9 known places,
and every one has a concrete fix (§3). Every step position and every allowed
move is reached by at least one test. Core line coverage is 97–98%. The core
does no IO, and that is checked on every run. **Three things block sign-off:**

1. **A real bug.** When saving to disk fails, the call raises an error, but
   the change is already in memory. Until the session is reopened, reads show
   state that disk does not have. The repro is below (§4.5).
2. **Rulings John has to make** (§8a). The most important: may a frontend
   turn auto-step on while a question is open? Today it can, and steps then
   run under the question.
3. **The ratchet's own guard.** Two allowlisted numbers were *raised* at the
   wave-1 merge, although the allowlist says it only shrinks (§7). Until CI
   enforces that, "ratchet on" is a promise, not a check.

The recommendation: make the rulings, run wave 3 (§8b), and sign off when its
acceptance criteria pass. None of the findings calls the design into question.

## 2. Numbers vs τ 0.15

| | Trusty `crates/*/src` | τ 0.15 `src` |
|---|---:|---:|
| lines | 7,119 | 99,233 |
| functions | 315 | 3,301 |
| typical function (CC p90 / p99) | 8 / 21 | 8 / 21 |
| worst function (CC max) | 31 | 73 |
| functions over the CC 15 limit | 5 (1.6%) | 85 (2.6%) |
| longest function, lines | 102 | 336 |
| files over 800 lines | 1 | 32 |
| largest class, methods | 47 | 216 (`AgentSession`) |
| classes over 25 methods | 3 | 22 |
| line coverage | 94.8% (core 97–98%) | 92.1% (statements) |

**What it means.** A typical function is exactly as complex as in τ 0.15:
p90 and p99 are equal. The difference is in the tail. τ 0.15 has 85 functions
over the limit and a 216-method class. Trusty has 5 and 47. Two cautions:

- **The scope differs.** τ 0.15 includes its providers, TUI, and frontends.
  Trusty's ~2,000 lines of Python driver are not counted here (Phase 4
  measures them).
- **Coverage is "same ballpark", not a delta.** LLVM counts lines and
  coverage.py counts statements.

The CC max is 31, not the 30 in the metrics report. See §7.

## 3. Ratchet violations (rule 7)

Rule 7 sets three limits: CC ≤ 15 per function, ≤ 25 methods per class, ≤ 800
lines per file. Here are the 9 places that break them today. For each one: what
it does, why it grew, and the fix. **The shared acceptance test for every
split:** the generated diagrams do not change (`scripts/diagrams.sh --check`
passes without regenerating), all tests stay green, and `scripts/ratchet.py
--tighten` deletes the line.

### 3.1 `Step::feed` (CC 31) and `Step::job` (CC 29): the step machine

**What they do.** `job` answers "what should the driver do next?" for one
cursor. Each box in the [step graph](diagrams/step-positions.svg) is one
branch. `feed` takes the driver's answer and moves the cursor to the next box.
Each arrow labelled `feed …` in the graph is one branch.

**Why they grew.** Each function is one big `match` that names every position,
and all the special cases sit inline. Examples: a hook halting, a hook
raising, a hook that already delivered the input, a sample error with or
without a reply, the throttle.

**Fix for `feed`: sort by what came back first, then by where the step is.**
The machine already refuses a feed that does not answer the job that is out
(`answers`). So the kind of answer alone tells which positions are possible:

- `HookFailed` and a hook string (`Hooks(stop)`) are the same at every
  position: drop the draft and end the step. The exceptions: at `post_step`
  they only turn auto-step off. At `pre_input` a string also removes the item
  and sends the client a notice. These become `on_hook_failed` and
  `on_hook_stop`. Together they are the 9 orange (property-test-only) edges in
  the graph.
- A plain `Hooks` answer, by position, becomes `on_hooks`. `pre_step` goes to
  input. Input and `pre_reply` commit the draft. `pre_sample` goes to sample.
  `post_reply` goes to tools, or fails if the sample had an error.
  `post_result` goes to `post_step`. `post_step` calls `after`.
- `Sampled` becomes `on_sampled` (reply, error, both, or neither).
- `CallDone` becomes `on_call`, and `Waited` becomes `on_waited`.

After this, `feed` is a six-way dispatch (CC ≈ 7), and each handler stays
under 10.

**Fix for `job`: one method per kind of position.** Five positions only hand
out a hook job: `PreStep`, `PreReply`, `PostReply`, `PostResult`, and
`PostStep`, and the last three sync first. A small `Pos::hook()` lookup
collapses them into one arm. Then three methods carry the real decisions:
`input_job` (build the queue once, offer the next item still pending, or hop
to `PreSample`), `sample_job`, and `tools_job`. Each returns "a job" or
"hopped, look again". `job` becomes a loop over a seven-arm match (CC ≈ 9).

### 3.2 `tau-py Session`: 47 methods, the Python face of the core

**What it is.** It is the one Python class (`tau.Session`) through which
Python reaches the core. Each method is a 3–10 line pass-through: take the
lock, call the core, save, and convert the result. Its size is the API's
size: 4 lifecycle calls, 6 frontend calls, 4 extension calls, 3 hook calls,
2 question calls, 2 label calls, 5 driving calls, and 17 reads.

**Two options:**

- (a) **Split it into several Python classes**, for example a read-only
  `session.view` and a writing `Session`. That adds a shared lock object, an
  extra hop per call, and churn in `driver.py` and `handles.py`. In return,
  nothing becomes easier to read, because the logic lives in the core. The
  Python layer already gives users the organized surface: `Driver`, `Cursor`,
  and `TreeHandle`.
- (b) **Change the rule for binding classes.** A `#[pymethods]` class is gated
  per method instead of by count: every method a thin delegation (CC ≤ 3,
  ≤ 15 lines). That catches the real risk, logic creeping into the bindings,
  which a count does not.

**Recommendation: (b).** The count stays a good rule for core types, where
methods carry logic. For a mirror it only measures the API's width. §3.3
already shrinks that width by about 12 methods' worth of branching. Needs
John's ruling.

### 3.3 `tau-core Machine`: 40 methods, every call a frontend or extension can make

**What it is.** It is the session plus every cursor's step. It takes the calls
in the "frontend", "extension", "hook", and "question" rows of the
[refusal table](diagrams/frontend-refusals.md).

**Why it grew.** Six calls (`open`, `add_item`, `remove_item`, `append`,
`set_label`, `remove_label`) each exist three times: a public version that
syncs, a `hook_*` version that rides the hook's sync, and a private `*_from`
that both use. That accounts for 18 of the 40 methods. The Python binding then
branches on `hook` to pick one of the two, so the same choice is made twice.

**Fix.**

1. **Collapse each triplet into one method** that takes the
   `hook: Option<(cursor, serial)>` the binding already has. That removes 12
   methods, and the binding stops branching.
2. **Move the question lock (`ask`, `answered`, `locked`, and the `asking`
   map) into a small `Questions` type** owned by the machine.

The result is about 25 methods or fewer. No behavior changes, and the refusal
table must regenerate identically. `Questions` needs a glossary entry
(rule 2).

### 3.4 `tau-core Session`: 36 methods, the data

**What it is.** It is the tree (nodes, children, walk, config, LCA, `next`),
the cursors and their moves, labels, the scratchpad, the record outbox, and
replay from disk (`load`, `replay`, `reconcile`).

**Fix.** Split along the data it owns.

- **A `Tree` type** holds the append-only node structure and the reads over it:
  `node`, `walk`, `config_at`, `lca`, `children`, `node_count`, `next`,
  `deciding`, plus the private `push` and `check`. That is 10 methods.
- **Replay** (`load`, `replay`, `replay_move`, `reconcile`) **moves to free
  functions in a `replay` module.** Those are 4 methods, and replay is a
  separate concern: the I7 property tests target it.

`Session` keeps cursors, labels, the scratchpad, and the outbox: about 22
methods. This also takes `tree.rs` (752 lines, near the file limit) out of
danger. `Tree` needs a glossary entry ("the tree" is a glossary section, not
yet a type).

### 3.5 `Read::run` (CC 24): the `read` tool

**What it does.** It reads lines `offset..offset+limit` of a file. It refuses
a directory, a pipe, a device, or a binary file, and it appends a "continues
at offset N" note when the output is cut.

**Fix.** Three pieces come out:

- `open_regular` (open without blocking, then refuse a directory, an irregular
  file, or a binary file);
- the argument defaults;
- `continuation_note` (the two wordings of the "shown in part" note).

Each piece ends up at CC ≤ 8. The behavior is pinned by `tests/tools/` and by
the inline tests.

### 3.6 `add_all` (CC 22): registers the 22 exception classes

It is a flat list of 22 `m.add(...)?` lines. The tool counts each `?` as a
branch, while cognitive complexity is 0. **Fix:** loop over a table of
`(name, type)` pairs, which gives CC 2. Do not change the rule. `?` is an
early exit, and counting it is right everywhere else.

### 3.7 `Running::read_output` (CC 16): `bash` reads its command's output

It reads the pipe until the end, keeping the tail. It stops reading once the
shell exited more than a grace period ago, because a process that escaped the
group can hold the pipe open forever. **Fix:** move "poll the pipe once"
(ready, retry on EINTR, or fail) into `wait_readable`. That leaves CC ≈ 9.

### 3.8 `bash.rs`, 867 lines

The code is 517 lines. The other 350 are the inline `mod tests`. **Fix:**
move the tests that go through the tool's public API (most of them) to
`crates/tau-tools/tests/bash.rs`, and keep only the ones that need private
helpers inline. Keep counting whole files: a file is what a reader opens.

## 4. Behavior findings

Each finding below comes from the generated
[refusal table](diagrams/frontend-refusals.md) or the
[step graph](diagrams/step-positions.md), checked against
[model.md](../design/model.md) and the [glossary](../design/glossary.md).

| # | finding | class | ruling |
|---|---|---|---|
| 4.1 | `set_auto(on)` accepted under an open question, and it wakes the cursor | **spec gap** | **John** |
| 4.2 | `ask` accepted with no hook running | as designed | none |
| 4.3 | `answered` silent with no open question | **bug** (fail early) | **John** (error type) |
| 4.4 | after a failed save, a busy cursor can't be aborted | as designed, given 4.5 | folded into 4.5 |
| 4.5 | a failed save raises after the in-memory change | **bug** (repro'd) | **John** (which fix) |
| 4.6 | `go_to` on a locked cursor to a missing node says `Locked` | spec gap | plain fix |
| 4.7 | `open`/`remove_item`/`remove_label` never raise `NoSuchCursor` | as designed | plain fix to the table |
| 4.8 | `abort` in the throttle returns "cancel" | as designed | plain fix to model.md |
| 4.9 | 9 moves reached only by the property test | test gap | plain fix |

**4.1 `set_auto(on)` under a question.** model.md §7 says a question locks
the cursor and refuses "input, go-to, and step". It is silent on auto-step,
yet turning auto-step on is listed as a wake (§6). So a frontend can start
steps on a locked cursor. A concrete way to get there:

1. A `pre_tool` hook asks for approval.
2. The user presses Esc. Abort leaves the question open, and the batch's
   calls become `Interrupted`.
3. The cursor rests at a `Results` node with `next = Sample`, still locked.
4. `set_auto(on)` now samples again while the approval question is still
   open. The eventual answer then applies to a call that has already ended.

**Recommendation:** refuse `set_auto(on)` with `Locked`, and keep
`set_auto(off)` allowed (like abort, it only stops things). Add the case to
model.md §7.

**4.2 `ask` with no hook running.** This is as designed, and the core *has*
to accept it. A `pre_tool` approval asks while the `RunCalls` job is out. The
core sees no cursor hook then, because tool hooks run inside the driver's
call unit (model.md §5, Q34). The guard lives where it can work: a retired
handle raises `NotInHook` on `ask` (glossary, "cursor handle"). The count
can grow without limit, but each count is a real open question in driver
memory. Nothing to do.

**4.3 `answered` with no open question.** The core silently does nothing.
The driver's own `answer` raises `LookupError` first, so today's code cannot
reach this. But the core is the second line of defense, and it hides a
mismatch between the driver's question list and the core's count. That is
exactly what fail-early forbids. **Recommendation:** refuse it with a new
typed error, `NoQuestion` (a `LookupError`, plus a glossary entry). The
heavier alternative: `ask` returns a question id, and `answered(id)` refuses
an unknown or already-answered id. That would also catch a double answer
while a second question is open, which the count alone cannot. Recommended
only if Phase 5 permissions needs ids anyway.

**4.4 and 4.5 A failed save.** Read in `crates/tau-py/src/session.rs`
(`Session::write`) and `crates/tau-store/src/lib.rs` (`Store::save`). Every
changing call runs the core first and then saves. If the save fails, the
error is raised, but the core change stays in memory. The store then marks
itself failed, and every later changing call (including `abort` and
`next_job`) is refused with `Failed`. Reads keep working. This is documented
("open the session again", glossary "Python session"), but the reads now
show a state that disk does not have.

**Repro** (public API only; `/tmp/trusty_3d/failed_save_repro.py`). A
directory placed where the store writes its temporary scratchpad file makes
the save fail. That is the trick `tests/core/test_session.py` already uses.

```
before: items [] auto False busy False nodes 2
send raised: IsADirectoryError Is a directory (os error 21)
after (memory): items [(0, Input { client: "term", text: "hi" })] auto True busy True
abort raised: Failed
next_job raised: Failed
set_auto(off) raised: Failed
reloaded: items [] auto False dropped 0
```

So `send` raised, yet memory says the message was sent, auto-step is on, and
the cursor is busy. That busy cursor can no longer be aborted. After a
reload, the message is gone. Two smaller points:

- The first failure surfaces as a raw `OSError` subclass, and only later
  calls raise `Failed`. A caller has to catch two types.
- The log write and fsync succeeded. Only the scratchpad rewrite failed. So
  the log and the scratchpad file are both valid, and the reload is
  consistent (I7 holds on disk). The lie is in memory only.

**Options:**

- (a) **A failed save kills the session.** Every call, reads included, raises
  `Failed` from then on. The first failure raises `Failed` too, with the
  `OSError` as its cause. The driver stops, cancels its own in-flight
  effects (it cannot ask the core), and the user reopens the session.
- (b) **Roll back the in-memory change.** This needs an undo for every core
  call, or a copy of the session before each call. That is far too costly
  for a rare event.
- (c) Leave it as is and document it.

**Recommendation: (a).** It is honest, it is small (one check in `read`, one
mapping in `write`), and it settles 4.4 too. Nothing can be aborted because
nothing can be called, and the driver cancels its own effects.

**4.6 Which refusal wins.** `go_to` checks the lock before it checks that the
node exists. So the "go_to (not a node)" row says `Locked` in S14/S15 and
`NoSuchNode` everywhere else. **Plain fix:** check existence (cursor, node)
before state (`Locked`, `HookRunning`, `Busy`). A caller's mistake then gets
the same answer in every state. Write that order into model.md §5 as the
rule for all calls.

**4.7 No `NoSuchCursor` from `open`, `remove_item`, `remove_label`.** These
calls take no cursor. They take a node, an item, and a label, and they refuse
those with `NoSuchNode`, `NoSuchItem`, and `NoSuchLabel`. The "ok" in column
S16 just means "the call does not involve that cursor". **Plain fix to the
generator:** print `n/a` there, so the table does not suggest a missing
check.

**4.8 `abort` in the throttle.** It returns "cancel" because the driver
holds a sleeper that must be cancelled. The glossary ("abort") and model.md
§6 ("Abort during the throttle cancels the restart") say so. Only model.md
§8 still lists the cancellable effects as "`Sample` or `Calls`". **Plain
fix:** add the throttle there.

**4.9 Moves only the property test makes.** Nine moves have no named test: a
hook *halting* at `PreStep`, `PreReply`, `PostReply`, or `PostResult`, and a
hook *raising* at `Input`, `PreSample`, `PreReply`, `PostReply`, or
`PostResult`. The property test proves they keep the invariants, but nothing
pins *what the user sees*: the ending, auto-step off, the draft discarded,
and at `Input` the item still pending after a raise (model.md §7). **Plain
fix:** add one parametrized behavior test over hook point × {string, raise}.

## 5. Performance (from the traces)

A turn in memory takes 0.83 ms untraced. A real model call takes hundreds of
milliseconds to seconds. Nothing here is user-visible today.

| finding | act now? | why |
|---|---|---|
| Core is 2% of a turn (30 µs) | no | It is the goal of D11 and is met. |
| Bindings cost ~15× the core | **no, Phase 4** | Mostly the trace's own cost. About 250 spans at ~2 µs each make the gap between the traced turn (1,437 µs) and the untraced one (829 µs). The real binding cost is a fraction of what the table shows. Measure it untraced in Phase 4. |
| Driver polls: 49 `cursors()` + 33 `busy()` per turn | **Phase 4** | `_dispatch_ready` lists every cursor and asks each whether it is busy on every pump tick. That is cheap now (~150 µs) but grows with cursors × ticks. Fix: one core call that returns the cursors with a job ready. It is a driver change. |
| fsync is 54–81% of a stored turn, 8 fsyncs | partly, see below | It is the expected number. |

**Is 8 fsyncs right? Yes, by D11/D12.** The traced turn is: send, then
sample, then a `read` call, then its result, then a second sample. Counting
each save:

| save | log fsync | scratchpad rewrite (file + directory) |
|---|---:|---:|
| `send` returns (new input item: must be durable before the wake, D12) | 1 | 2 |
| sync before `post_reply` #1 (`User` + `Assistant`; the input item is now delivered) | 1 | 2 |
| sync before `post_result` (`Results`) | 1 | – |
| sync before `post_reply` #2 (final `Assistant`) | 1 | – |
| **total** | **4** | **4** |

The syncs before `post_step` and at the step's end find nothing new, so they
cost nothing. A plain turn with no tools costs 6. D11 says "a plain turn syncs
twice", which is right for log syncs. D12's "about ten fsyncs" is stale. Fix
the wording.

Two fsyncs are avoidable:

- **The `send` log fsync syncs nothing.** Only the scratchpad changed, so the
  log gets an empty `Sync` record and an fsync. If nothing but the scratchpad
  changed, the store can skip the log fsync: the log is already durable, and
  the scratchpad is never newer than it. That saves 1 fsync per input. It is
  cheap and safe, guarded by the I7 property tests. Optional in wave 3.
- **The rewrite after delivery.** On load, delivered items are dropped anyway
  (`reconcile`). So removing a delivered item never needs to be durable on
  its own. That saves 2 fsyncs per turn. It changes when the scratchpad file
  is rewritten, which D12 describes. **Phase 4**, with a D12 amendment.

## 6. Coverage

Rust tests plus pytest through the native module give 94.8% of lines. Gaps
that matter:

- **The core is fine.** `machine.rs` is at 97.8%, `step.rs` 98.4%, `tree.rs`
  97.1%, and `tau-store` 98.4%. No core file is below 95%. Most of what is
  missed is error formatting and refusals that the property test hits through
  the Rust API.
- **`tau-tools/src/session.rs`, 67.9% (17 of 53 lines missed).** This is the
  code that finds and kills every process a `bash` command started, by
  scanning `/proc`. Reading the code, the missed lines are its failure paths:
  `/proc` unreadable, a kill refused for a reason other than "already gone",
  and processes still alive after 200 sweeps. This is inferred from the
  code; the coverage data has no per-line detail. These paths are what stands
  between a hung command and a user's machine, and none is tested.
  **Fix:** pull the `/proc/<pid>/stat` parsing out as a pure function and
  test it with odd process names (spaces, parentheses, a zombie). Accept the
  OS-failure paths as known-uncovered and list them in the file's doc.
- **`tau-py/src/values.rs` 86% and `job.rs` 89%** (half of their functions
  never run). These are mostly `__repr__`/`__eq__` on value classes that no
  test prints or compares. Low risk. Tests come with Phase 4.
- **`bash.rs` 90.6% and `limit.rs` 92.2%**: error paths (poll failure, an
  overflow file that cannot be written). Acceptable for now.
- **Not measured:** the Python package (`python/tau`, ~2,000 lines).
  That is Phase 4's job by the plan.

## 7. Process gaps

1. **The allowlist can be raised at merge.** The wave-1 branches were built
   before the ratchet existed. At merge, `Step::feed` went 30 → 31 and
   `add_all` 21 → 22, and the allowlist was edited up to match. Its own
   header says "never raise a number … fix the code". The ratchet compares
   code to the file, and nothing checks the file. **Fix:** a CI step diffs
   `scripts/ratchet-allow.toml` against the target branch and fails on any
   raised number or added line. Rebasing a branch onto master before merge
   makes the ratchet run against current limits. The two raised entries need
   no special handling, because wave 3 removes both lines.
2. **The diagrams check is not in `check.sh`.** CLAUDE.md says `check.sh` is
   "everything except parity, in CI order". The diagrams check runs only as a
   separate CI job. It takes 5 s with a warm build. **Fix:** add
   `scripts/diagrams.sh --check` to `check.sh`.
3. **The step graph's `ALLOWED` move list is hand-written.** A move the tests
   make that is not in it stops the build, so it cannot go stale in that
   direction. The other direction is silent: a listed move that no test makes
   is only drawn dashed red. **Fix:** make `--check` fail on red edges and
   pink boxes. Then `ALLOWED` is a reviewed spec with drift caught both
   ways. Generating it from `step.rs` is not worth it: `self.go(self.pos)` is
   not statically readable.
4. **Plan items not done as written.** Both are recorded in the metrics
   report and are acceptable:
   - "`guppy` for 'core depends on no IO'": a `cargo metadata` and text-scan
     script does the same job.
   - "dead code denied": clippy `-D warnings` already fails on unused private
     items. Unused `pub` items have no tool. The plan should say so.
5. **Stale numbers.** The metrics report says CC max 30, but the code is now
   31. D12's fsync count is stale (§5). The allowlist header comment appears
   twice. `cargo machete` flags `grep-matcher` as unused. Each is a one-line
   cleanup.
6. **Phase 2's coverage list** (plan.md) still has unticked items: unknown
   tool, bad args, a single-client question declined when the client
   detaches, abort during a `pre_tool` awaiting approval, `pre_tool` string →
   `Err`, branch across a config node, and others. This is not a 3d exit
   criterion, but it is the next place behavior can hide.

## 8. Wave 3

### 8a. Needs John's ruling first

| # | question | recommendation |
|---|---|---|
| R1 | After a failed save: (a) the session is dead, reads too; (b) undo; or (c) leave and document? (§4.5) | **(a)**, and raise `Failed` (caused by the `OSError`) on the first failure as well |
| R2 | May `set_auto(on)` run while a question is open? (§4.1) | **No:** `Locked`. `set_auto(off)` stays allowed |
| R3 | `answered` with no open question: new error `NoQuestion`, or question ids? (§4.3) | **`NoQuestion`** now; ids only if Phase 5 permissions needs them |
| R4 | Binding class method limit: split `tau-py Session`, or gate binding methods per method (CC ≤ 3, ≤ 15 lines) instead of by count? (§3.2) | **Change the rule** for `#[pymethods]` classes |
| R5 | New internal types for the splits, `Tree` and `Questions` (rule 2: glossary entries) (§3.3, §3.4) | **Approve** |

### 8b. Work items, in order

Process first, so the rest cannot drift. Then behavior fixes, while the code
is still in its reviewed shape. Then the refactors, whose safety net is
"diagrams unchanged".

1. **Allowlist guard.** CI fails when `ratchet-allow.toml` raises a number or
   adds a line against the target branch. *Accept:* a test branch that raises
   one number fails CI. A branch that lowers one passes.
2. **Diagrams in the gate.** `check.sh` runs `diagrams.sh --check`, which
   fails on red edges and pink boxes. *Accept:* `check.sh` output has a
   "diagrams" step. Deleting a test that alone covers an edge makes it fail.
3. **Failed save (R1).** *Accept:* the repro becomes a pytest in
   `tests/core/`. After the blocked save, `send` raises `Failed` (with an
   `OSError` cause). Every later call, reads included, raises `Failed`. The
   driver, given such a session, stops and cancels its in-flight effects. The
   refusal table's S18 column shows `Failed` in every row, `at` included.
4. **`set_auto(on)` under a question (R2).** *Accept:* refusal table cells
   S14 and S15 of `set_auto(on)` read `Locked`, and `set_auto(off)` stays
   `ok`. There is a behavior test for the abort-then-auto-on scenario of
   §4.1. model.md §7 lists auto-step on.
5. **`answered` with none open (R3).** *Accept:* it raises `NoQuestion`.
   There is a glossary entry. stubtest is green.
6. **Refusal order.** Existence before state, for every call. *Accept:* the
   "go_to (not a node)" row reads `NoSuchNode` in S1–S15. model.md §5 states
   the order.
7. **Named tests for the 9 property-only moves.** *Accept:* step-positions.md
   has no "property tests only" rows. The `Input` raise test asserts the item
   is still pending.
8. **Split `Step::feed` and `Step::job`** (§3.1). *Accept:* both are at CC
   ≤ 15. `diagrams.sh --check` passes with no regeneration. `--tighten`
   drops both lines.
9. **`Machine`: collapse the `hook_*`/`*_from` triplets, add `Questions`**
   (§3.3, R5). *Accept:* ≤ 25 methods. `tau-py` `open`, `add_item`,
   `remove_item`, `append`, `set_label`, and `remove_label` no longer branch
   on `hook`. The refusal table is unchanged.
10. **Core `Session`: extract `Tree`, move replay to free functions** (§3.4,
    R5). *Accept:* ≤ 25 methods. `tree.rs` ≤ 700 lines. The I7 property
    tests are green. Bench `depth` is within noise. `types.svg` shows `Tree`.
11. **`Read::run`, `Running::read_output`, `add_all`** (§3.5–3.7). *Accept:*
    each at CC ≤ 15. `tests/tools/` is unchanged and green.
12. **`bash.rs` tests out** (§3.8). *Accept:* `bash.rs` < 800 lines. The
    moved tests are in `crates/tau-tools/tests/bash.rs`, and the test count
    is unchanged.
13. **`tau-py Session` per R4.** *Accept (rule change):* `ratchet.py` gates
    `#[pymethods]` methods at CC ≤ 3 and ≤ 15 lines. The count line goes, and
    rule 7 in CLAUDE.md says so. *Accept (split):* each class ≤ 25 methods.
14. **`session.rs` coverage** (§6). *Accept:* the stat-line parser is a pure
    function with tests for odd names and zombies. The file is ≥ 85% covered,
    or its untested OS-failure paths are listed in its doc comment.
15. **Cleanups.** model.md §8 gets the throttle. D12 gets the fsync count (6
    plain, 8 with one tool call). The refusal table prints `n/a` for calls
    that take no cursor. The `grep-matcher` dependency is removed (after
    confirming no trait import needs it). The allowlist header is
    deduplicated. The metrics report is regenerated. *Accept:* `check.sh`
    green and `ratchet.py` reports 0 allowed violations, or only what R4
    leaves.
16. *(Optional)* **Skip the empty log fsync** (§5). *Accept:* a stored
    one-tool turn makes 7 fsyncs in `trace_turn.py` (was 8). The I7 and
    torn-tail property tests are green.

**Later (Phase 4):** one "cursors with a job ready" call instead of polling.
Untraced binding cost. Lazy rewrite of the scratchpad on delivery (a D12
amendment). Measuring `python/`.

## 9. Exit criteria (plan.md §3d)

| criterion | status |
|---|---|
| Property tests green | **met.** `invariants_hold_under_any_driver` (I1–I5), `reload_at_any_sync_equals_the_state_at_that_sync` (I7), and `a_torn_tail_opens_as_the_last_sync_before_it` all pass in `cargo test` |
| Clippy gate green | **met.** Pedantic, `-D warnings`, all targets |
| Ratchet on (rule 7) | **met with a gap.** It runs in `check.sh` and in CI with 9 allowlisted violations, but the allowlist itself is unguarded (wave 3 item 1) |
| 3d review signed off | **pending.** This report. Needs rulings R1–R5, then wave 3 |
| Diagrams generated and diffed in CI (rule 6) | **met.** Separate CI job. Joins `check.sh` in wave 3 item 2 |
| Metrics comparable to τ 0.15 | **met.** Same distributions, radon baseline reproduced (3,301 functions, 222 over CC 10) |
| Traces across Rust and Python | **met.** `turn.json`, `turn-stored.json` |
