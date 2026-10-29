# Generated diagrams of the core

Rule 6: these are generated from code, never drawn. `scripts/diagrams.sh`
rewrites them; `scripts/diagrams.sh --check` fails if a committed `.dot` or
generated `.md` differs from what the code and tests produce now. The `.svg`
files are rendered from the `.dot` files (graphviz `dot`) and are not compared.

| file | what it shows | made from |
|---|---|---|
| `step-positions.svg` (`.dot`, `.md`) | every position one cursor's step passes through, the job each hands out, the feed or event on each edge | the transitions the test suites made |
| `frontend-refusals.svg` (`.dot`, `.md`) | each frontend, extension, hook and driver call against each cursor state: accepted, or the typed refusal | making every call in every state |
| `types.svg` (`.dot`) | fields and variants of `Body`, `Node`, `Session`, `Machine`, `Step`, `Job`, `Fed`, `Outcome`, `Part` | the Rust source, parsed with `syn` |

## 1. Step position graph

Read it top to bottom. Each box is a position of the step (`Pos`), with what it
means in words and the job it hands out to the driver, with the number of
scenarios that saw that job. Each arrow is a change of position; its label is
what caused it, and in brackets how many scenarios made that move.

- Causes starting `feed` are the driver answering a job (`feed Hooks`, a
  `Sampled` reply, a `CallDone`). `feed Hooks(stop)` is a hook that returned a
  string (halts the step); `feed HookFailed` is a hook that raised.
- Causes without `feed` are the core deciding with no job in between: "no input
  to offer", "sample does not apply", "tools apply", "tools do not apply",
  "commit refused" (a draft that would make the tree invalid fails the step).
- Out of `PostStep` the cause is the step's ending (`Done`, `Halted`, `Failed`,
  `Aborted`); only `Done` can start another step (`PreStep`, or `Throttle`
  first when the step committed nothing).
- Black: some named test makes the move. Orange: only a property test
  (`proptest!`, counted once however many cases it runs) does. Dashed red: the
  code allows it and no scenario makes it. A pink box: a position no scenario
  reaches. Today there are none of the red or pink kind.

How it is recorded. `tau-core` has a cargo feature `transitions`
(`crates/tau-core/src/transitions.rs`), off by default and in no wheel. Built
with it, every position change in `step.rs` is written once per scenario to
stderr. The scenario is the name of the test: the `cargo test` thread for the
Rust suite in `crates/tau-core/tests/`, a pytest id for `tests/behavior` and
`tests/core` (run against the same core through the bindings, built with the
feature into `target/diagrams`, so the normal build is untouched). This was
chosen over deriving transitions from the job/feed sequence because the
sequence cannot show the moves with no job in between (`Input` to `PreSample`
with nothing to offer, `PreSample` to `Tools`), and these are the interesting
ones. It adds one small private method (`Step::go`) and one-line calls; with
the feature off they compile to nothing. `PROPTEST_RNG_SEED=1` keeps the property
tests' counts fixed so the text sources are reproducible.

The set of moves the code allows (`ALLOWED` in `scripts/diagrams/positions.py`)
is the one hand-written input: read once from `step.rs`. A move the tests make
that is not in it stops the build, so it cannot go stale in that direction. A
new move that no test makes would not be noticed; the property tests and the
review of `step.rs` are what cover that.

## 2. Frontend-call refusals

Rows are calls, columns are states of a cursor. Every cell was produced by
building a fresh session through the public Python API, bringing the cursor to
the state (for example "calls out, one answered" is reached by running a step
to a batch of two calls and feeding one outcome), making the call once, and
recording what came back. Green: accepted (`ok, cancel` also tells the driver
to cancel the effect in flight). Yellow: refused because of what the cursor is
doing (`Busy`, `Locked`, `HookRunning`, `NotInHook`, `WrongFeed`). Grey: the
cursor or node does not exist. Pink: the session is closed or its store failed.
`frontend-refusals.md` has the same table as text.

## 3. Types

Boxes are the Rust names, one row per field (`+` public, `-` private) or
variant, with the type as written. An arrow means the type is named in the
other's fields. Nothing is added to what the source says. (`Pos`, the step's
position enum, is read by the same program and drawn as diagram 1.)

## Observations

Things to look at; none has been changed.

1. **Every position and every allowed move is reached by a named test or a
   property test.** Nine moves are reached only by the property test
   `invariants_hold_under_any_driver`: a hook halting (`feed Hooks(stop)`) at
   `PreStep`, `PreReply`, `PostReply` or `PostResult`, and a hook raising
   (`feed HookFailed`) at `Input`, `PreSample`, `PreReply`, `PostReply` and
   `PostResult`. No named scenario pins them; they are the orange edges.
2. **`Tools` hands out no job.** It is a pure decision point (`tools apply` or
   `tools do not apply`), as are the other "core decides" moves. It is the one
   position a driver never sees.
3. **A sample that fails after a reply** (`Sampled` with reply and error) goes
   through `PreReply` and `PostReply` and only then to `PostStep` as `Failed`;
   one named scenario covers the `PostReply` to `PostStep` move that follows.
4. **`set_auto(on)` is accepted on a cursor locked by a question** (and wakes
   it), although input, go-to and step are refused there. The model says only
   those three are refused, but a step can then start under an open question.
5. **`answered` is accepted with no question open**, silently. A stray answer
   does nothing; `ask`, which has no caller check either (accepted in any state,
   including with no hook running), can be counted up without limit.
6. **After a failed save, `abort`, `next_job`, `feed`, `ask` and `answered` all
   raise `Failed`.** Only reads still work. So a busy cursor cannot be aborted
   once the store has failed, though the refusal is by design ("open the session
   again").
7. **A save that fails raises after the in-memory change is made** (core
   applies the call, then the binding saves): the caller sees an error for a
   call whose effect is already in memory. Not checked here; worth a test.
8. **Checks run in an order a caller can notice.** `go_to` to a node that does
   not exist on a locked cursor says `Locked`, not `NoSuchNode`; on every other
   cursor state it says `NoSuchNode`. Harmless but inconsistent.
9. **Some calls take no cursor and never say `NoSuchCursor`**: `open`,
   `remove_item`, `remove_label`. Only their own argument is checked.
10. **`abort` in the throttle returns `ok, cancel`** because there is a sleeper
    to cancel; at rest and in hook states it returns plain `ok`.
