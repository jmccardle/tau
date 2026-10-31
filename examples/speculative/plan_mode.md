# plan_mode: friction log

Files: `examples/speculative/plan_mode.py` (105 lines), `tests/examples/speculative/test_plan_mode.py`
(11 tests, passing). Written against HEAD ab490dd.

## Does

`/plan [task]` puts the branch in plan mode. The model is briefed once (a `User` node written by
the extension) and may only call `read` and `grep` (and `bash` for an allowlist of read-only
commands); anything else is refused with an `Err` the model reads. When a reply ends (`stop ==
End`), the person is asked "approve / revise / reject". Approve leaves plan mode, writes "The plan
is approved. Carry it out." and turns auto-step on; revise asks for feedback, sends it as the next
input and stays in plan mode; reject leaves plan mode and stops. The mode is an option
(`extensions.plan_mode.mode`), so it lives in a `Config` node on the branch.

## Easy

- **State is one option, so branches, reload and `go_to` come free** (lines 28-39, 44, 53). A
  `set_options(mode="plan")` is a `Config` node; going back before it leaves plan mode, a second
  cursor is unaffected, a reload keeps it, and `/config plan_mode mode=act` is already "leave plan
  mode" with no code (tests: `plan_mode_belongs_to_the_branch_not_the_cursor`,
  `plan_mode_entered_by_config_still_briefs_the_model`).
- **The whole approve/revise/reject flow is one `post_reply` hook with two `await cursor.ask`**
  (lines 61-83). A question holds the step open, the cursor is busy, abort and decline fall out as
  `None` (`abort_declines_the_question_like_any_other`). No state machine, no pending flag.
- **Ending a plan is an ordinary reply**: `stop == End` (line 66) separates "still researching"
  (Tools) from "here is the plan" with no new concept.
- **Entering plan mode by any route briefs the model** because the brief is level-triggered in
  `pre_sample` (lines 50-58): look at the walk, write the node if it is missing. Nine lines, and
  `/plan`, `/config`, a config file and `go_to` all work.
- **Refusing a call is one string** from `pre_tool` (lines 86-100). The model gets the reason as
  an `Err` authored by `Extension("plan_mode")`, and the real tool never runs
  (`the_model_is_offered_every_tool_and_refused_...`).
- **`/plan` is five lines** (42-47) and the type language gave it `task: str | None` as an optional
  field in the form.

## Fought

### (a) Expressing the behaviour

1. **Offer only read-only tools** → a sample is always given the driver's whole `tools` tuple
   (`driver.py` `SampleRun(... tuple(self._tools.values()) ...)`); no hook, option or `Config` can
   narrow it per cursor. → Refuse after the fact in `pre_tool` (line 86); the model is still shown
   `write` and `bash` and learns by being refused (asserted:
   `provider.requests[0].tool_names == ["read", "write", "bash"]`). → Open, `# WISH` at line 88.
   **Severity: awkward** (works, costs tokens and a wasted turn per mistake; it is also the thing
   plan mode is *for*). This is the main finding: refusal is the only enforcement point, not an
   offering one.
2. **A `list[str]` option with a default** (`allow_tools = ("read", "grep")`) → the options reader
   refuses `default_factory` ("use a plain default"), a list default is a dataclass error, and a
   tuple default is rejected with the misleading "its default () must be array, not object". →
   Two comma-separated `str` options split in the hook (lines 36-39). → Approximation, WISH
   comment at line 33. **Severity: awkward** (the form shows `read,grep`, which is also what a
   `/config` user must type).
3. **"Read-only" is a property of a tool, not a name** → `Tool` has no such flag. → `allow_tools`
   names them. It composes badly: plan mode refused `harness_sense`'s `hook_errors` until the test
   named it (`hook_errors_lists_the_calls_an_extension_refused`). **Severity: awkward.**
4. **Bash restricted to an allowlist** → only the command text. → prefix allowlist plus a regex
   for `; & | < > ` $ ( )` and newline (lines 93-99). It is a guard, not a sandbox
   (`git diff --output=f` writes). → Approximation, said so in a comment. **Severity: cosmetic**
   for this experiment, a real hole for a real plan mode. The honest answer is "plan mode cannot
   promise read-only"; only a tool that knows it is read-only can.
5. **Telling the model it is in plan mode** → system text is a node; the only channel is a `User`
   node. → `pre_sample` writes one (line 57), `by Extension`. Consequence: after `/plan "task"` the
   model reads the task first and the brief after it (asserted in
   `plan_with_a_task_briefs_the_model...`). The input is a pending item delivered *before*
   `pre_sample` runs, so the brief cannot precede it. **Severity: cosmetic** here; for a model that
   acts on the first instruction it would matter.

### (b) Showing state to the user

6. **"The cursor is in plan mode"** → `CursorView` carries `at, busy, auto, questions, items,
   ending, hook_error` and no extension state; options exist only behind `Client.options(cursor,
   ext)`, a call. → A frontend must re-ask `options` whenever a `CursorView.at` changes
   (`shown()` in the test), or fold `Config` nodes out of `Appended` itself. The chat client does
   neither: after the `/plan` reply nothing on screen says which mode the branch is in
   (`a_line_client_lists_plan_...`). → Gap left open. **Severity: awkward** (a mode the person
   cannot see is a mode they forget; the failure shows up as refused calls).
7. **Tell the person it ended** → `cursor.tell` goes to `cursor.client`, "the client of the
   nearest frontend-authored `User` node", which is not necessarily who answered. Used for reject
   (line 80); approve says nothing because the next step's activity is the feedback. **Severity:
   cosmetic.**

### (c) Input from the user

8. **Approve / revise (with text) / reject is two questions, not one** → `ask` expects one hint
   of the type language; there is no record or union (`dict` is refused). → First
   `Literal["approve","revise","reject"]`, then a second `ask(..., str)` only for revise (lines
   68-77). A frontend sees two consecutive questions. **Severity: awkward** (a web form would want
   one dialog: three buttons and a text box).
9. **Who answered?** → `ask` returns the value, not the client. The revise feedback is sent with
   `cursor.send`, which authors it as the *typist* (`_typist` of the walk), not the answerer;
   with two clients attached those can differ. Approve is authored by the extension. → Chose `send`
   for the person's words and `write` for the extension's words (test
   `revise_sends_the_feedback...` asserts both authors). **Severity: awkward**: `write` can never
   say `Frontend`, `send` can only say "the last typist", and neither says "the person who clicked".
10. **Free text on the command line** → `/plan add retry` is parsed as shell words: two positionals
    for one parameter, "takes 1 arguments ['task']". It must be `/plan "add retry"`
    (asserted in the chat test). **Severity: awkward** for the command whose point is a sentence.
11. **A declined question** (no client connected, abort, go-to, an explicit decline) → returns
    `None`. I chose "halt with a reason" (lines 76, 82), which turns auto-step off and leaves plan
    mode on. Nothing distinguishes "nobody there" from "the person said no"; reject is a verdict,
    decline is not. Fine, but the extension had to choose. **Severity: cosmetic.**

### (d) Timing and concurrency

12. **`Client.run` returns when the command returns, not when its step ends.** `Client.run`'s
    docstring and D16 §2 say a `Cursor` command's result arrives "once its step has ended"; the
    code settles the future at command return (`_commands.py` `before`, `_settle(pending.future,
    result)`), and only a *failed* command waits for the step to end. For `/plan "task"` that is
    visible: `run` has returned while the cursor is busy with the question, and an immediate
    `/config` is `Busy` (hit in `harness_sense` tests). Behaviour is the better one (a frontend that
    awaited the step would deadlock on the question), the documentation is wrong. **Severity:
    cosmetic (doc), but it decides whether a line client can type the answer.**
13. **Approve must turn auto-step on itself** (line 72). If the person was stepping by hand,
    "carry it out" would write a `User` node and rest. `set_auto(True)` inside `post_reply` works,
    and the next step starts at once (`auto-step went on to a second sample by itself`). Natural,
    but a plan-mode author has to know that "moved and auto on" is what restarts a step.
14. **Ordering**: `set_options` then `write` in one hook gives `Config`, `User` as two nodes and two
    moves; `next` skips `Config`, so the cursor correctly samples. No surprise, one thing to know.
15. **Input typed while the question is open** waits for the next step (busy cursor). That is the
    right behaviour, but the person who types "no, do it differently" instead of clicking revise
    gets no signal that the question will swallow the turn.

## Frontend view

**Chat line client today** (`examples/chat.py`, exercised in the last test): `/` lists
`/plan [task]`; the question prints as `? Approve this plan? [approve/revise/reject]`; the person
types `approve` (or `revise`, then the text at the next `? What should change?`); an empty line
declines. It prints the command's reply once and never again says which mode the branch is in; the
plan itself is the model's text deltas, so a client that attached after the reply sees nothing of
it (the line client draws no history).

**A TUI or web frontend** would need, for plan mode to render well:
- the mode per cursor, as a badge. Today: `client.options(cursor, "plan_mode")` on every
  `CursorView.at` change, or its own fold of `Config` nodes from `Appended`
  (`json.loads(config.data)["extensions"]["plan_mode"]`), which is core knowledge duplicated;
- the question as one dialog with a text box shown for "revise" (it receives two `OpenQuestion`s
  in sequence and cannot know they are one decision);
- the plan itself next to the question: `OpenQuestion.text` is all it gets, so either the
  extension copies the plan into the text or the frontend finds "the last `Assistant` node of the
  cursor's walk". I chose the second ("Approve this plan?") to avoid duplicating a long text in
  every event; a remote client pays a `walk` call for it;
- which tools were refused: `Results` nodes with `Err` by `Extension("plan_mode")`, rendered
  differently from tool errors (needs the `by` of each outcome, which `Appended` carries).

## Wish

Smallest first.

1. **Fix the docs: `Client.run` returns at command return** (D16 §2, `client.py`). Deletes nothing,
   prevents the deadlock a line client would otherwise build in.
2. **A per-branch tool filter, owned by an extension.** The smallest shape: a `pre_sample` hook may
   set `subject.tools: Sequence[str] | None` (names to offer for this sample; `None` = all). Core
   stays dumb (it filters one tuple); the policy stays in the extension. Deletes the "refused
   after the fact" design: `pre_tool`'s allowlist branch (lines 91-92) shrinks to the bash case,
   the model never sees `write`, and plan mode becomes honest. Also gives `harness_sense.probe` its
   read-only branch for free. Prefer this to a `Config` key: it needs no new persisted concept.
3. **`list[str]` options with defaults** (accept a tuple default, or `default_factory` evaluated
   once at describe time). Deletes the comma-splitting and the `str` workaround (lines 33-39, 91,
   96) and makes `/config plan_mode allow_tools=read,grep,whereami` a real list in the form.
4. **A way to say "read-only" on a tool**, so plan mode needs no name list: either an optional
   `read_only` attribute on `Tool`, or a list on the extension in the style of `tools` and
   `commands` (`read_only = [whereami, tree]`) that other extensions can read. If that feels like
   too much core, the honest answer is that it is the extensions' convention, and `plan_mode` and
   `harness_sense` agree by name (what the test did).
5. **`CursorView` could carry an extension-supplied status**, e.g. `cursor.show("plan")`, one short
   string per extension per cursor, sent whole like the rest. It removes the "re-ask options on
   every `at`" dance and the frontend's duplicate fold; it is the one thing here that is *state
   the user cannot see*. Smaller alternative with no new event field: `Client.options` results
   included in the connect snapshot as `Options(cursor, extension, values)`. Preferring neither
   until a second extension (permissions mode? `/model`?) wants a badge.
6. **`ask` accepting a small record** (`dict[str, T]` with a fixed key set, or a dataclass, the
   same as options already take). Deletes the second question (lines 73-77) and gives frontends one
   dialog. Failing that, the honest answer is "ask twice; frontends group questions that arrive in
   one hook" and a `group` id on `OpenQuestion`.
7. **`ask` returns who answered** (or a `handle.answered_by` after the await), so `send` can be
   authored by the answerer and the extension can `tell` them. Deletes the "typist" guess at
   line 77.
8. **A command parameter that takes the rest of the line**: `task: str` as `Annotated[str, "rest"]`
   or simply: the last `str` parameter swallows the remaining words. The type language already
   knows the parameter list; only `commandline.parse` has to. Deletes the quoting rule from every
   free-text command.
