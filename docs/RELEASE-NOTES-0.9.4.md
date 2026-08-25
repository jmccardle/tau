# v0.9.4 — the session that got long

Written from the commits between `v0.9.3-fullhistory` and master, so the release
commit, the GitHub release body and the site all say the same thing.

0.9.3 asked whether a stranger could install τ and get a working conversation.
This release is about the hour after that. `docs/PLAN-0.9.4.md` opened with seven
items that all work on a five-message conversation and stop working on a
five-hundred-message one — an 800-message transcript took over four minutes to
redraw, streaming throttled to a few tokens a second, `Esc` discarded the turn,
and the tree editor became unreadable at exactly the size that makes it
necessary. All seven are built.

Three things nobody planned turned up while building them, and each is a case of
τ being quietly wrong rather than slow: the system prompt τ composes had never
reached a model through the TUI, the token counter re-counted every earlier turn
on every turn, and a reasoning model's thinking was streamed into a widget that
was never on screen.

---

## The prompt τ builds is the one the model gets

τ has two system-prompt builders and the wrong one won. `TauBackend` composes the
real prompt — the base text, the project's `AGENTS.md` / `CLAUDE.md` context, and
the `Available tools:` list. But `action_new_chat` in the TUI and `run_print` in
`tau -p` each composed a SECOND string from `config["system_prompt"]` and stored
it as the session's first message. That stored message is what goes on the wire,
and it takes precedence, so everything the builder composed was discarded.

The TUI's copy also substituted `"You are a helpful assistant."` when the config
named no prompt. That stand-in meant the stored message was never absent, so on
the TUI path the built prompt was never used at all: **no `AGENTS.md` context and
no tool list has ever reached a model through `tau`**, and a default install was
told it was a chat assistant rather than a coding agent. The tools were still
offered on the wire — the model simply had no idea that acting was its job, which
is what a report of "0 tools" was actually describing.

`tau -p` was correct by accident. With no `system_prompt` key it stored nothing,
so the built prompt survived; setting the key broke print mode the same way. The
two frontends therefore disagreed about what one `config.json` means.

**This is the other half of 0.9.3's "The system prompt and project context
files".** That release taught `TauBackend` to discover `AGENTS.md` / `CLAUDE.md`
and compose a real prompt. It did not notice that the frontends were still
writing a competing first message over the top of the result, so the builder ran
and its output was thrown away.

Every frontend now folds the configured prompt onto the **model entry**, where
`TauBackend` reads it as `custom_prompt` and composes the context files and the
tool list around it. The session stores what the backend built. `rpc_mode`
already worked this way and is the precedent.

Three smaller things came with it. `Backend.system_prompt` is declared, so "the
backend builds the prompt" is written down rather than implied.
`append_system_prompt` moved to `tau_agent_core.sdk`, beside the builder whose
base-text slot it augments. And `/system-prompt` opens its editor on
`BASE_SYSTEM_PROMPT` rather than on the stand-in, because that key REPLACES the
base text and seeding the editor with anything else offers a starting point the
agent was never given.

---

## `{{fields}}` in a system prompt

`custom_prompt` replaces τ's base text wholesale, so a user who wanted their own
voice had to accept wherever the builder chose to put the `Available tools:` list
and the project context. The builder now fills `{{field}}` slots in whatever base
text it is given:

| field | what it inserts |
|---|---|
| `{{base_prompt}}` | τ's own base text, rendered — lets a custom prompt WRAP τ's voice instead of replacing it (custom prompts only) |
| `{{project_context}}` | the `<project_context>` block |
| `{{tools}}` | the `Available tools:` block |
| `{{tool_names}}` | the names alone, for a one-line mention |
| `{{cwd}}` | the directory the tools run in |
| `{{model}}` | the model id going on the wire |

Three rules keep it honest:

* **Substitution runs on the template only, never on the assembled prompt.** A
  project's `AGENTS.md` is user content, and a `{{tools}}` inside it reaches the
  model as written rather than being rewritten by a field that shares its name.
* **Naming a section moves it rather than copying it**, so the prompt does not
  carry the tool list twice. A template naming neither section composes exactly
  as it did before, which is what makes this invisible to a prompt that does not
  ask for it. `{{tool_names}}` is a mention rather than the section, so using it
  does not suppress the block.
* **An unknown field raises `SystemPromptFieldError`.** A misspelled `{{tols}}`
  rendered literally would reach the model as four characters, say nothing, and
  look exactly like a prompt that worked.

`{{env.*}}` is deliberately not a field: prompts are persisted to the session log
and sent to a server.

A slot alone on a line whose section is empty takes its line — and the blank line
above it — with it, so a repository with no `AGENTS.md` does not get a prompt
with a hole in it.

`BASE_SYSTEM_PROMPT` is new text and uses four of the slots, in the positions the
builder would have appended them to anyway. The composition is unchanged, and now
legible to anyone who copies the default into `config.json` as a starting point.

---

## `Esc` no longer throws the turn away

Pressing `Esc` to stop a model mid-answer could discard the whole turn — the
user's own prompt, the assistant message, and every tool result that had already
completed — and report the loss as a `JSONDecodeError` traceback rather than as a
sentence. An abort is a normal gesture, bound to a key the user is expected to
press.

The loss needed one more condition: the abort had to land while a tool call's
`arguments` were still streaming. Then the finalizer joined a buffer it knew was
truncated and parsed it strictly, which raised; the raise became a provider
`ErrorEvent` and then a `RuntimeError`; and `AgentSession` persists a turn as one
batch AFTER `loop.run(...)` returns, so the raise killed the local list and every
append site was skipped. The traceback was not a side effect of the loss. It was
the cause.

Three changes. Each closes a different part, and any one alone leaves a defect:

* **The finalizer reads `stop_reason`.** On `stop_reason == "aborted"` a tool
  call that cannot be finished is **dropped** — not repaired, and not defaulted
  to `{}`. A half-streamed `{"path": "/etc/pas` turned into *something* is a call
  the model never issued, run against arguments it never finished choosing, after
  the user asked for the turn to stop. `usage.extra["dropped_partial_tool_calls"]`
  records how many, and is present only when it is non-zero. The same unfinishable
  buffer on a COMPLETE stream still errors: strictness there is load-bearing, and
  is why `docs/TOOL-CALL-PARSING-BUG.md` exists.
* **A failed run carries what it finished.** The loop attaches its completed
  messages to the exception on the way past, and the session persists them before
  re-raising. The persist block is now shared by the success and the failure path,
  which is why an aborted turn used to lose the user's own prompt as well as the
  reply.
* **Every outstanding `tool_call_id` is answered.** An abort during tool
  execution used to leave an assistant message whose calls no result ever
  answered. Each outstanding call now gets an `Operation aborted` result. The
  guard sits above both executors rather than inside them, because the parallel
  one has no abort check of its own and would otherwise have run the whole batch.

A test that asserted the data loss as correct — `session.messages == []` after a
provider error, under a comment reading "The turn never completed, so nothing was
persisted either" — now asserts the user's message survives. A 503 mid-turn is
not a reason to discard what the user typed either.

Verified live against llama.cpp, which emitted **12 `arguments` fragments** for
one two-key tool call. That is the aggressive fragmentation the bug needed, and
the thing a single-chunk cloud response hides. The buffer at the abort was
`{"path": "/etc` — truncated inside a value, and dropped rather than repaired.
25 new tests, one of them sweeping every SSE line position, because the bug's
window was narrow enough that a single-position test passes with it still there.

---

## Splice anchors record where they came from

A `compaction` entry recorded its summary, its resume point and the context size
before it. It did not record which model wrote the summary or what that cost — and
`AgentSession._summarizer()` can route a compaction through a different model than
the conversation (a `local_summarizer` compaction policy), so "which model wrote
this" was a real question with no answer in the log. Both numbers existed in scope
at the write and were dropped there.

An `elide` recorded less still: only its resume point. A compaction at least
carried `tokensBefore`; an elide recorded no size at all, and the token figure is
the one number that cannot be recomputed from the tree afterwards.

Both anchors now carry provenance, measured at write time:

* `summarizerModelId`, `summaryUsage` — compaction only. Which model wrote the
  summary, and what generating it cost.
* `coveredEntries`, `coveredTokens` — both kinds. The span the anchor removes from
  the fold.
* `agentSpecId` — both kinds. The id of the `agent_spec` node in force over that
  span, found by ancestry from the anchor's parent. The id only; the system prompt
  is still stored as a digest and never as text.

Nothing reads these back into model input. They are a record, and the fold is
unchanged — `ConversationTree` was not touched.

Reference: `docs/TREE-BROWSER-AS-EDITOR.md` §8 (the four gaps and the decision),
§11.3 (why the signatures widened rather than the payload alone).

---

## The 50-turn ceiling is gone, and the number is now yours

`AgentLoopConfig.max_turns` defaulted to `50`, and 50 was a number nobody could
change. `create_agent_session` took no such parameter, no CLI flag set it, and no
config key was read — the only ways in were `AgentSession(max_turns=…)` and
`ctx.spawn_branch(…, max_turns=…)`, both Python-level. So every TUI session and
every `tau -p` run stopped after 50 LLM calls whether or not the work was done,
and stopped **silently**: reaching the ceiling falls out of the `while` and emits
an ordinary `agent_end` with `is_error=False`.

A ceiling the operator cannot see, cannot raise, and is not told about is not a
safeguard. The default is now `None` — no ceiling — which is also pi's position:
`agent-loop.ts:155-275` exits on error, on no-more-tool-calls, or via a
host-supplied `shouldStopAfterTurn`, and has no turn bound at all.

Three new ways to state one, highest first:

```bash
tau -p --max-turns 12 "…"                      # this run
```
```json
{ "max_turns": 30 }                            // ~/.tau/config.json, standing
{ "models": { "gpt-4o": { "max_turns": 12 } } }  // one model entry
```
```python
create_agent_session(model="gpt-4o", max_turns=12)   # the SDK, which had no way to say this
```

`--max-turns` is run-level, re-applied at every `create_backend`, so a mid-session
`/model` switch cannot change how long the agent may work. `--max-turns 0` is
refused at the argv boundary: "no ceiling" is spelled by omitting the flag.

What bounds a runaway run when nobody states a ceiling: an extension's budget
guard tripping the abort signal (`max_usd`/`max_seconds`), a `terminate`-ing tool,
or Escape in the TUI.

**Not fixed here:** a stated ceiling is still reached silently. Nothing in the
event stream distinguishes a truncated run from a finished one. See
`docs/CLI-PLAN.md`, "`--max-turns` and the ceiling that used to be unreachable".

---

## A terminating tool in sequential mode did not end the turn

Found by removing the ceiling above, which is the whole argument against it.

`AgentLoop._execute_sequential` tracked a terminating tool result and used it to
skip the rest of the batch, then returned `_build_batch_result(all_results)` —
dropping the flag. `_execute_parallel`, two functions down, always passed
`terminate=terminated`. So `ToolBatchResult.terminate` stayed `False` in
sequential mode, `run`'s `if batch.terminate: break` never fired, the model was
consulted again, called the same terminating tool again, and the run went to
`max_turns`.

With the old ceiling that was 50 wasted turns, ending silently — it read as a
slow turn. With no ceiling it does not end at all, which is how it surfaced: the
test suite was killed by the OOM killer. Affects any tool declaring both
`execution_mode="sequential"` and `terminate=True`. `nats_bus`'s `speak` verb is
documented against this exact failure mode, observed live running to turn 28; its
own path is parallel, so it was not hit.

The test that should have caught it asserted only that the rest of the batch was
skipped — the half that worked. It now counts LLM calls, where one turn and fifty
look different.

---

## A gateway can now be told to speak, without τ guessing that it does

A second field report against the same OpenAI-compatible gateway 0.9.3 hardened
against (`docs/PLAN-0.9.3.md` §4.2) turned up three things. One was a τ bug, one
was a τ diagnosis that pointed the wrong way, and one was a gateway defect τ can
now be told to work around.

**A present-and-null key is not an absent key.** The streamed-frame reader used
`.get(key, default)`, whose default applies only when the key is missing.
Gateways send these keys present and null. A delta carrying `"tool_calls": null`
reached `enumerate(None)` and raised `TypeError: 'NoneType' object is not
iterable`, killing the turn for every model on that gateway whether it called a
tool or not. `choices`, `delta` and `tool_calls` now read null and absent the
same way. The Azure content-filter preamble frame that the report blamed was
already handled; this was the real cause. The streaming path also gained the
buffered path's guard against a non-object tool-call entry, so that shape is a
named error instead of an `AttributeError` reported as "the model said nothing".

**The nameless-tool-call error now says which of two failures it is.** 0.9.3
made τ refuse a tool call with no `function.name` and name the gateway. There
turned out to be two ways to arrive there, and they need different answers. If
the name is absent from every frame, nothing client-side can recover it and the
message says so, as before. If the call arrived in the Anthropic tool_use shape
— a top-level `name`/`input` and no `function` object, which is a gateway
leaking its upstream schema — then the name IS on the wire and the message now
says that instead, along with the config key that reads it.

**`compat.tool_call_schema` is that key.** Set it to `"anthropic"` on a model
whose endpoint returns tool calls in the Anthropic shape, and τ translates them
into the OpenAI schema before reading them:

```json
"asksage/gpt-5.5": {
  "backend": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.asksage.ai/server/openai/v1/",
  "reasoning": true,
  "stream": false,
  "compat": { "tool_call_schema": "anthropic" }
}
```

Three things it deliberately does not do:

* **It is never detected.** The other two `compat` fields are inferred from the
  base URL because guessing wrong produces a 400 naming the field. Guessing this
  one wrong would rewrite a tool call τ was handed correctly, so an operator
  states it or it does not happen — and the config entry becomes the record of
  which endpoint is broken.
* **It translates; it does not repair.** A call whose name is blank, whose
  `input` is not an object, or that carries no argument payload at all still
  raises. A tool that takes no arguments sends `"input": {}`; τ will not
  substitute one for a call that sent nothing.
* **It is a stopgap.** The fix belongs on the gateway. Setting the key makes τ
  usable against a non-compliant endpoint while the report is open; it does not
  make the endpoint compliant.

The gateway's *streamed* responses for the same models are a different defect
and no compat field reaches it: `function.name` is empty on the first tool-call
delta and never populated afterwards, so there is nothing to translate. Pair
`tool_call_schema` with `"stream": false` on such a model.

---

## Token counts stopped counting the same prompt again every turn

`Usage.total_tokens` is one completion's prompt PLUS its completion, and the
prompt is the whole conversation so far. Two surfaces summed it across a
conversation anyway: the header's aggregate and the exchange badge. Both were
quadratic — an N-turn chat counted turn 1's text N times. A real 17-message
session on disk read **192.9k tokens** for a conversation that was 22.6k tokens
long and had generated 10.5k.

There are two numbers now, and they are never added together:

* **`ctx`** is the prompt the lane last SENT. It is carried as a replacement
  rather than a sum, because prompt N contains prompt N−1 in full.
* **`out`** is what the lane generated, which really does sum.

An exchange reads `2 tools · 1.9k ctx · 550 out`. The header follows pi's footer:
cumulative ↑input ↓output, with the context as its own separate figure.

**Underneath it, a second defect: the cached span was billed twice.** Both OpenAI
and Google report the cached tokens INSIDE the prompt count. τ copied them to
`cache_read_tokens` without taking them back out of `input_tokens`, so the two
overlapped and every consumer of the pair counted the cache twice — including
`compute_cost_usd`, which charged it at the input rate and again at the cache-read
rate. Both providers subtract now, as pi does.

---

## `message_start` brackets one message again

`message_start` and `message_end` bracket ONE assistant message. The agent loop
emitted `message_start` from its text branch alone, so it fired once per text
delta: a 2137-delta answer produced 2137 of them, each carrying a longer copy of
the same partial message. The same branch structure meant a completion that
produced only reasoning, or only a tool call, was never bracketed at all.

It is emitted once per completion now, on the first content event of any kind,
and a completion that yields no delta still gets its bracket from the terminal
event. pi does the same, on its stream's `start` event.

The TUI never read `message_start`, so nothing rendered wrong. What this affects
is `tau -p --mode json`, the RPC event stream, and any SDK consumer that pairs
the two events.

---

## Resuming a long conversation is no longer a four-minute wait

Loading a saved chat back into the TUI was quadratic in its length. Every message
mounted a dozen widgets, every awaited mount let Textual re-arrange the entire
widget tree, and the tree kept growing — so an 800-message session took **over
four minutes** to redraw, and it redrew on every resume, `/compact`, tree
navigation and elide. The same mounted tree throttled the next turn's streaming
to a couple of tokens per second, which is what "thinking and response text
accumulates but doesn't display" looked like from the outside.

| transcript | reload before | reload after | streaming after the reload |
|---|---|---|---|
| 50 messages | 1.7 s | **0.28 s** | 13.0 → **22.3 tok/s** |
| 200 messages | 21.0 s | **0.28 s** | 3.6 → **22.4 tok/s** |
| 800 messages | 251 s | **0.24 s** | **22.4 tok/s** |

The chat view now mounts the last **4 user turns or 50 messages**, whichever is
reached first, and writes a `⋯ N earlier · click to show them` row above them.
Click that row, or run **Show earlier messages** from the command palette
(`Ctrl+P`), to mount the whole conversation.

This bounds what is *drawn* and nothing else. The whole conversation is still
loaded, still in the session log, and still what the model is sent. Nothing was
truncated.

---

## Reasoning text reaches the screen

A reasoning model's thinking accumulated into a widget that had never been
mounted, so none of it painted. `MessageBox.ensure_reasoning` assigned the region
to `self._reasoning` and THEN mounted it into a slot that `compose()` creates. If
`compose()` had not run yet the mount raised `AttributeError` — but the
assignment on the previous line had already happened, so every later call took
the "it already exists" branch and handed back a widget that was never on screen.

This is not a rare race. `asyncio.Queue.get()` on a non-empty queue returns
without yielding, so the agent loop consumes stream events back to back with no
event-loop tick between them, and on a reasoning model the first event is a
reasoning delta. Measured: a burst of 8 deltas followed by 20 paced ones showed
**0 of 28 reasoning tokens**, with 135 characters buffered in an unmounted region.

`add_tool_call` had the same bug, so a turn whose first event was a tool call
raised too. Both route through one path now, which buffers a child created before
`compose()` ran and mounts it in `on_mount`. `ToolBox` buffers a result body
written before it mounts, for a related reason: `Markdown.update()` on an
unmounted widget keeps the title, keeps the display flag, and **silently drops
the text**.

The error was also misattributed. `EventBus.emit` caught it and routed it through
the extension error surface, so the user saw `extension error in notify handler
(message_update): …` once per turn. It was not an extension.

The test suite could not see any of this: it awaited a pause after every event,
on the written assumption that "the real backend never produces a synchronous
burst". That assumption was wrong, and it is why a deterministic bug shipped
untested. The harness has an unpaced cadence now, with six tests on it.

**If you see no reasoning at all, check the model entry before τ.** A
`chat_template_kwargs: {"enable_thinking": false}` under a model's `extra_body`
rides on every request, constrained or not, and the server then reports no
reasoning to report. τ sets that flag by itself for `grammar`- and
`choices`-constrained calls, so it does not need to be stated as a standing model
default. `scripts/reasoning_trace.py` separates the three cases — it reached the
bus, it was produced but never streamed, or the server reported none.

---

## A running turn now says what it is doing

Between the moment you pressed Enter and the moment answer text started
arriving, nothing on screen changed. A turn spent reasoning, or waiting on a
tool that takes thirty seconds, looked exactly like a turn that had died.

The exchange line counts while it runs:

```
Working… · 0:03                                 thinking
Working… · 812 out · 0:19                       a tool is running
Working… · 812 out · ~143 chunks · 0:47         answering
```

The two numbers are different kinds of thing and are never added together:

* **`812 out`** is measured. It is the real `output_tokens` the server reported,
  summed over the completions that have finished, so it steps forward at each
  tool call. It is absent until the first one reports — a provider that sends no
  usage figures shows no token count rather than a zero that looks like a
  working counter.
* **`~143 chunks`** is the completion still in flight, and there is no measured
  token count for it: the usage block arrives on a stream's last chunk. So this
  counts stream events. On most OpenAI-compatible servers one chunk is one
  token, but nothing guarantees that, which is why it says *chunks*.

Each lane counts on its own line, so a forked sub-agent or a bus submission
running alongside your turn has its own readout.

---

## The tree browser: indentation counts forks, not messages

The layer under the turn groups below. One `parentId` level used to be one widget
level, so at guide depth 4 and 100 columns a 25-message conversation drove the
label width to zero and the tree grew a horizontal scrollbar. A single-child run
is a sibling now and only a fork opens a level, so indent depth is bounded by the
forks on a path rather than by the conversation's length. (A user message also
opens a level — see the next section — and the two rules compose.)

**A compaction row states its span.** It used to show only the first line of its
summary, so which entries it covered was invisible.

**A branch summary is drawn as a pair with the branch it looks back on.** The
summary and its immediately preceding sibling share one hue and differ by weight,
so a relation between two rows is visible as one. Pairing with the immediately
preceding sibling — rather than with every sibling that is not a summary — is
what makes a branch point that was abandoned twice read correctly.

**Hovering a row traces where it leaves your path.** The shared half of the chain
is underlined and the divergent half is coloured, so a hover says where that node
stops agreeing with where you are. A node on the cursor's own path reports no
divergence at all, rather than highlighting a shared prefix that would read as
one.

**An elide entry is searchable.** It projected empty content, so no JMFTS query
could find where history had been folded. It projects a text naming its resume
point now. The span count is deliberately left out: a fabricated N in a search
index reads as a measurement forever.

**Keys.** `Enter` commits, so a click selects rather than jumping out of the
browser. `←` collapses and `→` expands, because giving `Space` to marks removed
Textual's only expand gesture.

---

## The tree browser reads as turns now

Four things, from working the browser against a real forked session.

**Every row was one or two cells too wide.** The labels were sized against a
width that did not subtract the vertical scrollbar, so the rows overflowed and
the tree grew a horizontal scrollbar showing two cells of nothing — which then
cost a row of height as well.

**A turn folds now.** Your message owns the turn it started: the assistant's
reply, every tool call and every tool result are its children, and it starts
folded. The next thing you asked is its sibling, not its child, so a hundred
turns is a hundred rows rather than a hundred levels of indentation. The turn
you are currently in opens, so `◀ current` is on screen. `→` opens a folded
turn, `←` folds it again.

**`navigate` rows are gone.** They record that the cursor moved and carry no
message, so the turn forked after one now hangs off the answer it was forked
*from* — which is what happened. Two exceptions keep their row: a `navigate`
that is the current node, and one with more than one child, which is a real
branch point. The entry stays in the log and on the ancestry either way; only
the row is gone.

**`Enter` on one of your own messages now forks from its parent.** Continuing
from below a user message would put two user turns in a row, which a conversation
cannot have. So the cursor moves to that message's parent and the message itself
comes back in the input box for you to edit — send it and you have a second
branch beside the first, with the original still in the tree. On an assistant or
tool row `Enter` is unchanged: continue from below it.

**Only a row with something under it has an expand arrow.** Textual draws that
arrow whether or not a node has children, so every assistant and tool row carried
one that clicked, toggled, and revealed nothing. Those rows also get the two
cells back for their preview.

**Rows say what they are, in colour.** The `user:` / `assistant:` / `toolResult:`
tag at the start of each row is painted on its own, in the same hue that role
wears in the transcript; the preview after it stays neutral, so the left edge of
the tree is scannable without reading it. Bookkeeping entries — a compaction, a
branch summary, an elide, a model change — take a quiet grey instead of a
conversation colour, because they are not turns.

**The detail pane folds away.** `Ctrl+D`, or a double-click on its border, gives
its half of the body back to the tree; one row stays behind saying so, and
clicking that row brings it back. Until now the only thing that could hide the
pane was a terminal shorter than 20 rows. (`Ctrl+M` would have been the better
name for it, but a terminal sends the same byte for `Enter` and `Ctrl+M` and
`Enter` is the commit key here.)

`docs/TREE-EDITOR-MANUAL.md` is updated to match.

---

## Eliding is one key inside the browser

`Elide a span ending here…` was a fourth button on the action chooser, and
choosing it re-opened the whole tree browser to ask for the second node. If the
pair turned out to be illegal you learned about it after both screens had closed,
as an error over a conversation you could no longer see the shape of.

It is **`Ctrl+E`, in the browser**, and the mode chooser has three buttons now.

To drop old history and carry on, put the cursor on the oldest entry you want to
keep and press `Ctrl+E`. To choose both ends, mark one with `Space`, move the
cursor to the other, and press `Ctrl+E`. **You do not have to remember which end
you picked first** — the two ends of an elide are always an ancestor and a
descendant of each other, and the conversation always continues from the deeper
one, so the tree decides.

While exactly one row is marked, every row that cannot be the other end is greyed
out. A refusal names its reason and leaves the browser open on the row you were
looking at.

**The two ends bracket what is KEPT.** Given `[1, 2, 3, 4, 5, 6]`, pairing `2`
with `4` leaves `[2, 3, 4]` — not `[1, 5, 6]`. An elide is the summary-less form
of the compaction anchor, so the kept region is always one contiguous run ending
at the deeper of the two nodes; cutting a span out of the middle is not something
it can express. If that deeper node is not the current row, the conversation also
moves back to it and everything newer leaves the context.

The line under the tree says all of that before you press anything:

```
ctrl+E: keep this span, drop the other 3 entries, and move back to it
```

It used to read `elide 37 messages`, which invites exactly the wrong reading —
and undercounted, because it measured the fold at the anchor and so could not see
the entries the cursor move abandons.

---

## `Ctrl+C` asks before it quits, and `Esc` `Esc` opens the tree

`Ctrl+C` was bound straight to quit, so one mistimed press ended a session with
whatever you had typed still in the input. It now does four things depending on
where you are:

1. Generating — stops the turn, the same as `Esc`.
2. Something typed — clears the input.
3. Nothing typed — writes `press ctrl+C again to exit` into the header.
4. Nothing typed and that offer still standing — quits.

`Esc` with nothing generating used to do nothing at all. It now writes `press Esc
again to view the tree`, and a second press within three seconds opens the tree
browser. `Esc` still means "cancel this response" while a turn is running.

Pressing either key withdraws the other one's offer: there is one header line,
and an offer you can no longer see should not still be answerable.

---

## Four themes, swappable live

τ's colours are a theme now rather than a stylesheet. `parley.tcss` names 25 role
variables and contains **zero colour literals**; a theme supplies the palette, and
three tests hold the split open in both directions.

| theme | what it is |
|---|---|
| `mocha` | the default — Catppuccin Mocha, byte-identical to 0.9.3's look |
| `latte` | Catppuccin Latte, light |
| `gruvbox` | warm, higher-contrast dark |
| `ansi` | your terminal's own 16 colours, and no backgrounds at all |

Three ways to choose one. A standing setting in `~/.tau/config.json`:

```json
{ "theme": "gruvbox" }
```

One run:

```bash
tau --theme latte
```

And `Ctrl+P` → `Theme: <name>`, which swaps live and saves the choice.

Write your own as `~/.tau/themes/<name>.json`, with `extends`, `palette` and
`textual` keys; a file named after a built-in replaces it. A light-terminal user
who wants `ansi` needs two lines:

```json
{ "extends": "ansi", "textual": { "dark": false } }
```

**A broken theme file no longer stops τ from starting.** It raises one error
toast naming the file, and τ runs in `mocha`. A broken file named after a
built-in leaves the built-in standing, so the fallback target cannot be removed
by the failure it is falling back from. A *swap* to an unknown name reports it
and keeps the theme you were already running, rather than taking your colours
away over a typo. A clean start says nothing at all.

`ansi` is the one theme that can fit a terminal τ has never seen. It paints no
backgrounds, because `ansi_black` is a black sidebar on a light terminal and
invisible on a dark one, and the theme cannot know which it is in. It costs
contrast: 9 distinct colours against mocha's 14, and the six-step text ramp
collapses to three.

`docs/tau-coding-agent.md` documents the four themes, the three selection
surfaces, the failure behaviour and the user-theme format.

---

## `--fun` reaches an installed τ

The random startup taglines were on in one artifact and off in every other.
`FUN_DEFAULT` was `False` in the source so a developer's snapshots stay
deterministic, and `package.sh` flipped it with a `sed` — but `package.sh` builds
only the `tau-<version>.tar.gz` attached to the GitHub release. The PyPI wheels
and sdists come from `python -m build` run straight against the source tree and
never went through it. `pip install` is how essentially everyone gets τ, so the
flip had never reached a real user.

The default is `True` in the source now, and no build path rewrites anything. The
deterministic surfaces — the snapshot suite, `testing.scenes`, `devshot` — ask
for `fun=False` by name instead of inheriting it. Two tests keep the gap from
reopening: no build script may mention `FUN_DEFAULT` outside a comment, and no
deterministic surface may inherit it.

`--no-fun` gives the fixed tagline.

---

## `pip install ffwf-tau` works

τ shipped four distributions and none of them was called `ffwf-tau`, so the
shortest guess returned "could not find a version that satisfies the requirement"
— which reads as a wrong interpreter or a wrong index and is neither. The name
was also already a console script this tree installs, so one string meant two
different things depending on where it was typed.

`ffwf-tau` is a metapackage now: no functional code, one dependency on
`ffwf-tau-coding-agent[tui]` at the same version. `[tui]` and not the bare
package, because someone who types the shortest name wants the program, and `tau`
cannot start the TUI without it. `[jmfts]` is deliberately absent — it needs a
running server, and a default install should not answer a question nobody asked.

Five distributions now.

---

## Upgrade notes

**If you set `system_prompt` in `~/.tau/config.json`.** The key still means "use
my text as the base prompt", but it is no longer stored as the session's first
message — it is folded onto the model entry and the backend composes the project
context and the tool list around it. Two things change for you. The prompt the
model receives is now longer than what you wrote, because the context and tools
are appended where they were previously discarded. And `{{field}}` slots in your
text are now substituted, so a literal `{{` in a prompt that was passing through
unchanged will either be moved or will raise `SystemPromptFieldError`. Nothing
else has to change; a prompt naming no field composes exactly as it did.

**If you read token figures off a transcript written before this release.**
`input_tokens` used to include the cached span that `cache_read_tokens` also
reported, so the pair overlapped. Reading them as `input + cache` gave roughly 2×
on a cached turn, and `compute_cost_usd` billed it that way. Figures written from
this release on do not overlap. `total_tokens` is the server's own number and was
never affected, which is why `total_tokens - output_tokens` reads an old
transcript correctly and summing the prompt fields does not.

**If you consume `message_start`.** It now arrives once per completion instead of
once per text delta. A consumer that counted them, or that used each one as a
"new message" signal, was previously seeing one event per token of answer text.
Two shapes are new rather than changed: a completion that produces only reasoning
or only a tool call is now bracketed (it emitted no `message_start` at all
before), and a completion that yields no delta gets its `message_start` from the
terminal event so that `message_end` never closes a bracket nothing opened.

**If you script `tau` and depend on a fixed tagline.** Random taglines are the
default in an installed τ now. Pass `--no-fun`.

**If you implement `SessionLog`.** `append_compaction` and `append_elide` have
grown keyword-only parameters with **no defaults**, so an existing implementation
will not type-check and an existing caller will raise `TypeError`. The new
signatures are:

```python
def append_compaction(
    self,
    summary: str,
    first_kept_id: str,
    tokens_before: int,
    *,
    summarizer_model_id: str,
    summary_usage: dict[str, int],
    covered_entries: int,
    covered_tokens: int,
    agent_spec_id: str | None,
) -> str: ...

def append_elide(
    self,
    first_kept_id: str,
    *,
    covered_entries: int,
    covered_tokens: int,
    agent_spec_id: str | None,
) -> str: ...
```

An implementation has to persist the five (respectively three) new values and give
them back through `entries()` as `summarizerModelId`, `summaryUsage`,
`coveredEntries`, `coveredTokens` and `agentSpecId` — camelCase, like
`firstKeptId`/`tokensBefore`. Nothing needs to interpret them; the payload keys are
part of the entry algebra, not each store's choice. The contract suite in
`tau_agent_core.testing` has three new cases that check the round trip, including
through `reload()` for a store that has a durable form.

An elide takes three of the five, not five. It generates no summary, so there is no
summarizer model and no summary cost — a parameter whose only admissible value is a
placeholder is the gap this change exists to close, not a symmetry worth having.

`agent_spec_id` is typed `str | None` and still has no default. `None` is a real
answer — a pi-imported log, or a store driven without an `AgentSession`, has no
`agent_spec` node — and the absence of a default is what keeps it distinct from a
caller that never looked. `tau_agent_core.session_log.agent_spec_in_force(entries,
leaf_id)` computes it; `tau_agent_core.compaction.estimate_span_tokens(entries)`
computes `covered_tokens` on the same basis as `tokens_before`.

**If you call `append_compaction` or `append_elide`.** Same change from the other
side: name the values. They were all available where the call is made, which is
why a default was rejected — a defaulted `None` records "unknown" for a value the
caller was holding.

**If you relied on the 50-turn ceiling.** Nothing stops a run at turn 50 any
more. If you were depending on it — knowingly or not — state it: `--max-turns 50`
for one run, `"max_turns": 50` in `~/.tau/config.json` for all of them, or
`max_turns=50` to `create_agent_session` / `AgentSession`. `AgentLoopConfig`
accepts `int | None` where it took `int`, so an implementation reading the field
must handle `None`.

**If you implement a `SessionLog` reader against `ConversationTree`.**
`ConversationTree` gained `message_text(entry_id)` — an entry's message flattened
to plain text, `""` for an entry that has none. Additive; nothing was removed or
renamed.

**If you resume a long conversation.** The chat view opens on the last 4 user
turns or 50 messages and says how many it left off screen. This is a rendering
bound: the whole conversation is loaded and is what the model is sent. Click the
`⋯ N earlier` row, or run **Show earlier messages** from `Ctrl+P`, to mount the
rest — on a long session that deliberately takes the time the cap avoided.
