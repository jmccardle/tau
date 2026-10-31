# Speculative extensions: what the contracts made easy, and where they fought

Seven exploratory extensions, written against the public API at `ab490dd`, to
stretch the extension and frontend contracts. Each has a module here, a friction
log (`<name>.md`), and tests in `tests/examples/speculative/` (87, all through the
public API, the Harness or `LocalClient`). Nothing under `python/tau_next` was
changed: where the API had no word for something, the code says `WISH:` and the
log records the gap. An independent review checked every load-bearing claim
against the code; the corrections are folded in below.

| extension | lines | idea | verdict |
|---|---|---|---|
| `ralph` | 96 | `while true; cat PROMPT.md \| agent` | needed nothing: the hook model is right |
| `plan_mode` | 105 | read-only planning, approve / revise / reject | works; tools can only be refused, not withheld |
| `router` | 112 | choose the model from the prompt | works; cannot read the current model |
| `bash_guard` | 144 | reject / allow / truncate long-output bash | works; `ask` semantics around abort leak |
| `gamemaster` | 175 | GM cursor drives one cursor per character | works; ~35 lines of plumbing for "ask a cursor" |
| `forest` | 268 | MCTS where the session tree is the search tree | works; abort keeps spending, 13 cursors left open |
| `harness_sense` | 383 | what an agent needs to use and modify its harness | partly; reload impossible, reaches the driver via `attach` |

`ralph` was also run against the local Qwen (llama.cpp, 192.168.1.100:8100): a
prompt asking it to increment `counter.txt` and write `DONE` at 3. Three
iterations, three sibling branches from the root, each with a fresh context, one
haiku per iteration in `log.txt`, stopped by `DONE`. Real model, real tools, no
surprises.

## The extensions

**bash_guard.** `pre_tool` matches the command against patterns (an option, so
`/config bash_guard` edits it) and asks `Literal["reject", "allow", "truncate",
"always"]`; truncate rewrites the call to pipe through `tail -n 200`, `always`
writes the pattern into options. *Easy:* the question is one line with schema,
check, wrong-answer refusal and abort included; a batch of three calls asks three
questions at once with no code; rewriting a call's arguments is one line.
*Fought:* `ask` returns `None` for nobody connected, a decline and an abort alike;
a question asked after its step was aborted keeps the cursor busy until someone
answers; a tool hook cannot write options (only `Cursor.set_options`), so
"always" goes through a `post_result` hook; a list option cannot have a default,
so the built-in patterns are invisible to `/config`; the tree keeps the model's
original arguments, so the rewrite is visible only as a note in the result.

**router.** `pre_input` matches the input (`!name` prefix, or a table of regex →
`Model`), strips the prefix, writes a `Config` node only when the model changes,
and a `Custom` node saying why; option `once` restores the previous model after
the turn. *Easy:* the model changes for this very sample; persistence on the
branch is free. *Fought:* no way to read the current model, so it parses `Config`
nodes itself; no way to list models, so `!thnk` (a typo) goes to the model as
text; in `pre_input` the handle's client is the *previous* typist (none on the
first input), so `tell` needs the draft's author passed by hand.

**gamemaster.** Main cursor = player and GM. `/character Name "persona"` or the
GM's `introduce` tool opens a cursor labelled `char:<name>` from the root with a
persona; the GM's `ask_characters(who, message)` sends to those characters in
parallel and returns their lines; `roll("2d6+1")`; the GM then summarises on the
main thread. Characters not named are never sampled (visible to a frontend:
their `CursorView.busy` never turns true). *Easy:* the cast is labels, so it
survives reload; parallel character steps cost nothing. *Fought:* a tool cannot
send to another cursor and await its reply. It takes an item, a `pre_step` that
writes the GM's message as `Extension("gamemaster")` (because `send` would author
it as the human player), a forced auto-step and a `post_step` that resolves a
future: ~35 lines for one idea. Aborting the GM does not stop the characters.
Characters are offered every tool, including `ask_characters` and `introduce`.

**plan_mode** (the Claude Code / pi idea). `/plan` sets an option; the model is
briefed, `pre_tool` refuses anything not read-only, and a `post_reply` question
asks approve / revise / reject; approve clears the mode and continues under
auto-step. *Easy:* the whole mode is one option, so branches, go-to, reload and
`/config` come free; the approval flow is one hook. *Fought:* the model is
offered `write` and `bash` and learns by being refused; nothing tells a client the
cursor is in plan mode; revise needs two questions in a row (no record type);
`/plan add retry` needs quotes.

**harness_sense** (my own condition: what an agent needs to use and change the
harness it runs in). Tools `whereami` (cursor, node, labels, model, options,
extensions and tools), `tree(depth)`, `context_budget`, `hook_errors`, `note`,
`probe(prompt)` (one step on a scratch cursor, tools refused there) and
`check_extension(path)` (build a throwaway driver with an edited extension).
*Easy:* seven bound methods became seven tools; notes as `Custom` nodes persist
and replay. *Fought:* reload is impossible (a `Driver` fixes its extensions at
build); a handle cannot see the driver, the extension list, the tool list, the
catalog or even its own cursor id from a tool, so the app must call
`attach(driver, ...)`; `hook_error` is cleared when the next step starts, before a
model could ask about it. The review adds two things the log did not: `probe`
types as a frontend client (a false author), and `check_extension` executes any
file the model names inside the driver's process, which nothing that guards
`bash` guards.

**forest.** `/search N question` branches N cursors from here, samples each, has a
judge cursor score each candidate (`Custom` score nodes), expands the best by UCB
under a sample budget, and moves the main cursor to the best leaf; the judge may
itself be searched (sketched). *Easy:* scores as side nodes persist, replay and
reach every client; all of MCTS (visits, means, UCB, cost) is derived from the
tree in ~100 lines with no search state to lose. *Fought:* no handle can await
another cursor's rest (polls `busy` every 10 ms) or abort it, so abort stops new
rounds but in-flight samples run on and are billed; cursors cannot be closed (13
left after a 6-sample search); progress goes only to the client that ran
`/search`. The review found it also writes the judge persona and refine prompts
as the human (via `send`), unadmitted.

**ralph** ("so stupid it might work"). `/ralph PROMPT.md` writes the file under a
fixed root and turns auto-step on; `post_step`, when the model finishes, moves
back to the root and writes the file again, re-read. Stops on `DONE`,
`max_iterations` or anything that turns auto-step off. *Easy:* ~25 lines of loop;
"the cursor never spins" did not fight it, since each iteration commits a node.
*Fought:* while looping the cursor is never at rest, so `/config` is `Busy` for the
whole run; at the root `cursor.client` is `None` (every `User` node is ralph's), so
it remembers who started it; the only status is a `Notice` to that client;
`StepLimit` counts steps, not iterations; and a `post_step` that *raises* leaves
auto-step on, while one that returns a string turns it off.

## Checked against the code (the review's corrections)

- **When `Client.run` returns** for a `Cursor` command depends on the outcome:
  on success, when the command returns (before the step's own hooks run); on a
  raise, when the step has ended and the cursor rests; on abort, immediately.
  D16 §2 reads as if all three wait for the step's end. Two authors misread it; the
  three timings are themselves the problem.
- **`examples/chat.py` deadlocks** when a command asks a question in its own body:
  the prompt loop awaits `run`, so the answer cannot be typed. Questions from
  hooks (plan_mode) work only because success returns early. Also: `main` drops
  empty lines, so "an empty answer declines" cannot be reached in the real
  program; and only the first open question is shown.
- **Doc bugs:** D16 §2's table says a command's `TreeHandle` is "the cursor's
  handle" (it has no cursor and no `walk`, which sent gamemaster down a wrong
  path); model.md §7 promises `handle.sample`, "run a tool outside any step" and
  `show`, none of which exist or have glossary entries.
- **Author misuse, API fine:** plan_mode's brief belongs in `pre_step` (which runs
  before input), not after; router's "nodes left behind when a later gate refuses
  the input" is extension order, the user's choice; ralph's `if cursor.client is
  None` cannot happen while a command runs.

## Themes, and what to do, ordered by value for cost

1. **Command lifecycle (5 of 7 hit it).** One rule, by subtraction: `run` settles
   when the command returns, success or raise. Delete the parked-failure path
   (~12 lines in `_commands.py`, one call in the driver); the step still ends
   `Failed` with `hook_error`, and anyone who wants rest has `Driver.rest` or
   watches `busy`. In chat.py, run a command as a task so a question inside it
   can be answered, and pass empty lines through (~4 lines).
2. **A raising `post_step` leaves auto-step on.** A fail-early bug: a crash is
   treated more gently than a polite stop. One line in `step.rs` plus a row in
   model.md's ending table.
3. **List options with defaults.** Accept a tuple default for `list[T]` (~3 lines in
   `_values`); let `commandline.value` read a JSON array when the text starts with
   `[`, so regexes with commas can be typed. Deletes workarounds in bash_guard,
   plan_mode and router.
4. **The client in `pre_input` is the draft's author.** ~2 lines in the driver;
   deletes router's hand-passed recipient and the first-input trap.
5. **Questions.** (a) A question asked on an aborted step declines at once (~6
   lines); this closes a real "busy until someone answers" hole. (b) "Nobody is
   connected" raises (`LookupError` subclass) instead of returning `None`: it is a
   different fact from a person saying no, and in a tool hook a raise becomes a
   clear error. (c) `OpenQuestion` carries the call it is about, so a TUI can draw
   it beside the call. Leave records and grouping alone.
6. **Handles that orchestrate cursors** (the `tau agents` primitive; gamemaster,
   forest, harness_sense). `await tree.rest(cursor) -> Ending` and
   `tree.abort(cursor)` on `TreeHandle`, refused on the handle's own cursor. Not new
   concepts: they are `Driver.rest` and `Client.abort` in handle form, and model.md
   already assumes a tool can wait on a cursor. Deletes ~25 lines from gamemaster,
   ~15 from forest, and the test-side `Waits` stand-in; makes "abort stops
   spending" expressible. **Authorship:** make `send` refuse outside a command's
   runner (it currently borrows the last typist, a silent lie); the real fix,
   extension-authored input (`ItemData.Input` with an `Author`), touches the log
   format and wants a decision. Until then gamemaster's item + `pre_step` idiom is
   the honest one.
7. **Tool-set shaping** (plan_mode, harness_sense; gamemaster and forest
   unknowingly). A `pre_sample` hook may set `subject.tools` (names; `None` = all);
   ~10 lines in the driver, nothing in core, one new field (glossary). The cheaper
   alternative is today's: refuse in `pre_tool`, at the cost of a turn per mistake.
   Rejected: tool lists in `Config` nodes (log format) and a `read_only` flag on
   tools (an extension convention).
8. **State a frontend can see.** `CursorView.model` and `cursor.model()` (the fold
   the driver already computes) plus `cursor.models()`; deletes router's and
   harness_sense's copies of the fold and lets a typo be refused. Then `show`:
   model.md already promises an unpersisted per-cursor status; three extensions
   (plan_mode, ralph, forest) independently wanted exactly one string per
   extension per cursor that every client sees. It is the one new frontend-visible
   concept here, and it needs a glossary entry.
9. **Command line.** The last `str` parameter takes the rest of the line
   (`/plan add retry`, `/search 3 what is 2+2`). Library only.
10. **App assembly.** Take `NodeHandle.cursor` (one line). Defer reload, a driver
    description and an extension loader: "restart the driver" is the honest
    answer to reload, and the loader belongs with the trust work in D16's open
    items. chat.py should load extensions named on its command line; running
    ralph on Qwen took a 100-line driver script for what should be
    `chat.py --extension examples/speculative/ralph.py`.

Items 1–4 are each a few lines and add no concept. Item 6 is the one that removes
a blocker. Item 8's `show` and item 6's authorship are the two decisions.

## What to keep

- **A typed question is one line** (`await node.ask(text, Literal[...])`): schema,
  check, refusal of a wrong answer, abort, concurrency across a batch, first good
  answer wins.
- **Options as a frozen dataclass folded over `Config` nodes**: plan_mode's whole
  mode, bash_guard's "always", router's restore are all branch-scoped, survive
  reload and go-to, and are checked before anything is written.
- **`Custom` side nodes as extension data**: forest's scores and harness_sense's
  notes need no memory of their own and reach every client.
- **Hooks that move the cursor**: ralph is 25 lines; the never-spins rule holds.
- **Plain functions as tools and commands**, with the type language giving model
  pickers, path and node completion and dropdowns for free.
- **Parallelism for free**: forest's fan, gamemaster's scene and bash_guard's
  batch ran concurrently with no extension code.

## Judgement

gamemaster exposes the one design flaw: "ask a cursor and get its reply" is the
sub-agent primitive model.md assumes, and expressing it took an item, a delivery
hook, a forced auto-step and a future shared across cursors, while abort could
not stop the characters. forest is the same flaw seen through cost. plan_mode is
the clearest evidence of the second gap (tools can only be refused). router,
bash_guard and ralph are conveniences, and ralph is the best evidence that the
hook model is right.
