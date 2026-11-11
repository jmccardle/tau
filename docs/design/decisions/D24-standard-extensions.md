# D24: Standard extensions

Status: accepted 2026-11-02. Phase 8, stage 8E, "Standard extensions" (`docs/plan.md`). Amends D3
(the summary's sample; the cold summary), D13 (`ModelConfig` gains `context_window` and `price`),
D15 (D3: retries ship in `tau`; normalized usage gains `written`; `cached` can be absent), D19
(a built-in may be off by default; built-ins are built with the loaded config; `load` also holds
the key `retry`) and model.md §7–§8 (an abort ends its hooks' `rest` waits, M7). D19 §5 stands:
`--no-extensions` skips the discovered sources and affects no built-in. The 8A `_BARE` special
case, which made it turn off `base_tools`, is removed.

## Context

The plan ships eight extensions built in: compaction, permissions, sub-agent, prune, retry,
skills and prompt templates, `@file` attachments, and usage. 0.15 had most of these as features,
so their absence is a regression (`docs/from-0.15.md` §2.4–2.10). Permissions and sub-agents were
examples only in 0.15, and prune had no 0.15 counterpart (§9 defines it).

What exists today, and is enough for most of them:

- `StepLimit` (`_step_limit.py`) is the model built-in: a class with a `name`, an `Options`
  dataclass and hooks. `_BUILTINS` builds each built-in with no arguments, and built-ins load
  first, sorted by key, which is also their hook order.
- Hooks at every point; `pre_sample` may move the cursor and set `subject.tools`.
- Commands, typed questions (`ask`), `show` (a status line per extension per cursor), `tell`
  (a notice), options folded over `Config` nodes, `save_options` to the user's file.
- `open`, `send`, `await rest`, `abort`, `close_cursor` and `reopen` on a handle: the sub-agent
  primitive (D17, Stage B).
- Every reply whose server reports counts has `meta.usage` `{input, output, cached}` (D15). `examples/retry.py` holds
  `RetryProvider`.

The question for each extension is what it does, and what it needs that is not there. §10 lists
those missing pieces in one table: that is the main result of this ADR.

## Decision

### 1. What they share

- Each is a built-in like `step_limit`: a class in `tau`, its key is its `name`, it can be turned
  off with `load: {KEY: false}` or `tau ext disable KEY`, and it is configured under
  `extensions.KEY` and with `/config KEY`. None of them has a config file of its own (`skills`
  reads its template and skill files, §6). Retry (§4) is not an extension: it has the `load` key
  `retry` and no options. `--no-extensions` affects none of these, `base_tools` and `step_limit`
  included: it skips the discovered sources only (D19 §5). Only `load` turns a built-in off.
- **The prefix of a walk** is its root `Config` and the `System` nodes before the first other
  message. Compaction keeps it, and a sub-agent starts from it. It holds the system prompt, the
  context files and the skills list, so it is also the prompt-cache prefix every cursor shares.
- Hook order is load order (sorted keys): `agents`, `attachments`, `base_tools`, `compaction`,
  `permissions`, `prune`, `skills`, `step_limit`, `usage`. No extension here depends on running before
  another, except that `attachments` must expand an input before any user extension's `pre_input`
  reads it, which load order already gives.

### 2. Compaction

**What the user sees.** When a branch nears its model's context window, the next sample is
preceded by a summary. The cursor shows `compacting…` while it runs, then a notice:
`compacted 182k → 23k tokens (nodes 4–418 summarized as node 431)`. The old nodes stay in the
tree: going back to one shows the conversation as it was. `/compact [focus]` compacts now, with
optional text the summary should keep in mind. `/config compaction auto=false` turns the
automatic trigger off (0.15's `/autocompact`).

**Key and options.** `compaction`, on by default.

| option | default | meaning |
|---|---|---|
| `auto` | `true` | compact by itself, at the soft limit and at the hard one; `false` leaves both to `/compact` |
| `reserve` | 16384, or a quarter of the window if less | tokens kept free for the reply and for the summary itself |
| `hard` | window − `reserve` | count above which a turn is interrupted and compacted (Ruling 14) |
| `soft` | 80% of `hard` | count above which the end of a turn compacts (Ruling 14) |
| `keep` | unset (20000, or half the soft limit if less) | the most tokens of the newest nodes copied verbatim |
| `model` | none | the model that writes the summary; none means the branch's own |

**Trigger** (two limits, Ruling 14). The **hard limit** is a `pre_sample` hook: if `auto` is on,
the model has a `context_window` and the count of `walk(at)` is above `hard` (default
`context_window − reserve`), it compacts, interrupting the turn, then the sample runs on the new
line. The **soft limit** is a `post_step` hook: when a step ends `Done` with the cursor on a reply
that asks for no tool (the end of the agent's turn), it compacts if the count is above `soft`
(default 80% of `hard`). The turn before that runs uninterrupted, so the summary is built from a
walk ending in the assistant's reply, and the next user message starts on the compacted line. A
model with no `context_window` gets no automatic compaction on either limit, and the cursor shows
`local:qwen has no context_window` so the user knows why. `/compact` still works.
`soft` above `hard` is a `ConfigError`.

**Counts** (amended by D26, stage 8E-6; the first draft counted characters over four). Every figure
here is a `Count` (`tau.Count`, D26 §1) from `Cursor.count`: the newest reply that reported its usage
and is not a copy gives the request's own bill, `input + output`; a span between two reports that
the server proves is a prefix of the next (`cached` equal to the previous `input`) gives its nodes'
cost; what no report covers is a **ceiling**, a node's UTF-8 bytes plus 16 tokens. Nothing is
estimated from characters, and the compaction works with no counting extension installed. 0.15's
calibrated per-class counter stays dropped; D26 adds an optional one (T2 and later).

**Algorithm** (D3's shape, now specified):

1. **Cut** (amended by D26). The tail is the longest suffix that starts at a `User` or
   `Assistant` node outside a proved span and costs at most `keep` tokens by the nodes' upper
   counts, or the newest turn alone if that costs more; a `Results` node always stays with its
   batch. The head is everything between the prefix and the tail. An empty head, or a head of
   nothing but an earlier summary, means `nothing to compact yet`; the sample then runs as it is,
   and may fail. Before a summarizer is opened the line must have room: the prefix and the
   request's overhead, the tail and the summary allowance, `min(reserve / 2, 8192)`, under the
   limit that called. After the summary, the tail is cut again to what fits beside it.
2. **Summary.** The extension opens a **summarizer cursor** at `at`, labelled `compaction:N`
   (with a `Config` naming `model` under `at` first, if `model` is set), `send`s it the summary
   prompt and the focus text, and `await rest`s. The summary is the text of its reply. The
   summarizer is offered the same tools as the branch, so its request is the branch's request
   plus one message, which keeps the prompt cache warm. A call it makes anyway is refused by
   compaction's own `pre_tool` (`summarizing: no tools`). An earlier summary is just part of the
   head, and the prompt says to carry it forward, so 0.15's separate update prompt is dropped.
   Afterwards the summarizer cursor is closed. Its reply stays in the tree, so `usage` counts it.
   **Compaction's `pre_sample` fires on the summarizer cursor too**, and must not compact it. The
   extension remembers the ids of the cursors it opened, and its `pre_sample` returns at once
   for those; it forgets an id when it closes the cursor. Prune (§9) opens no cursor, but its
   automatic trigger is also a `pre_sample`. It acts only when the latest `User` node was written by
   a person (D17's `Author`), so a summarizer cursor, whose input compaction wrote, is left alone
   without prune knowing compaction's labels.
3. **New line.** Under the prefix's last node: copies of the head's `Config` nodes, so the model
   and options at the cut stay the same; then the summary as a `User` node by
   `Extension("compaction")` with `derived_from` = the summarizer's reply (8E-6; it was the last head node: the reply carries the
   summary's reported count, which the meter reads); then a copy of every tail
   node, `Config` nodes and `Results` included, each with `copied_from`. The hook moves the cursor
   to the last copy (D3, Q25), and the sample sends the new walk (I1).

**Failure.** A summarizer that fails, halts, or replies with no text fails the hook, so the step
fails with that reason. Nothing is written, and no summary is invented. If the summarizer's own
request is too long for its model, the user can set a `model` with a larger window, or go back.
Esc during compaction aborts the summarizer and writes nothing. This needs the missing piece (d)
below.

**D3's cold summary is amended.** A cheaper model is just `model`. It sees the same walk through
its own provider, not a text rendering on a detached root. That is one path instead of two. The
cost: switching provider in the middle of a tool loop can be refused (D15, the Gemini 3 note), and
that is a visible failure.

**Needs:** (a) `context_window` on `ModelConfig`; (b) `copied_from` and `derived_from` built
(Rust, in core); (c) a handle read of the resolved model (`resolved_model`); (d) abort ends a
hook's `rest`.

**Dropped from 0.15:** the split-turn prefix summary, the read and modified file
lists appended to the summary, `/autocompact`, the calibrated counter, the summarizer
window check, and `compaction.summarizer` (now `extensions.compaction.model`).

### 3. Usage

**What the user sees.** After each reply, the cursor's status line:
`12.3k in · 96% cached · 812 out · $0.014 · ctx 41% · $1.27 session`. The dollar figures appear
only for a model with a `price`, and `ctx` only for a model with a `context_window`: a missing
price shows no cost, never `$0`. `/usage` prints one row per model for the whole session (samples,
input, cached share, cache writes, output, cost) and a total. Summaries and sub-agents are
included, since their replies are in the tree; copies are not counted twice.

**Cache notice.** (Built only if `cached` can be absent, Needs (c): the notice must tell a miss
from an unreported count.) After a reply that reports `cached: 0` with at least 4,096 input tokens, when the
previous reply on the walk (not a copy) used the same model less than 300 s earlier, the extension
`tell`s every client, once per model per run: `anthropic:sonnet read nothing from the prompt cache
(18.2k tokens sent at full price). Through a proxy's OpenAI endpoint nothing marks the cache; try
provider "anthropic".` The first sample after a compaction is never compared, because its previous
reply is a copy.

**Key and options.** `usage`, on by default. One option, `cache_notice` (default `true`).
**Hooks:** `post_reply` (the status line and the notice) and the command `/usage`, which takes a
`TreeHandle` and runs at once.

**Cost** is computed when shown, never stored, from the price now in the config:
`(input − cached − written) × input + cached × cache_read + written × cache_write + output ×
output`, with prices in USD per million tokens. A reply is priced by the model its branch was on
at that node.

**Needs:** (a) `price` on `ModelConfig`; (b) `usage.written`; (c) `cached` absent when the server
does not report it, made so only after checking what LiteLLM sends; (d) `resolved_model(name)`
and `model(at=node)` on a handle; (e) `copied_from`,
so copies are skipped.

**Dropped from 0.15:** the tokens-per-second readout, the per-exchange title (a TUI concern,
D23), RPC `get_session_stats` (`/usage` runs over the wire like any command), and the
`prompt_cache_dialect` suggestion.

### 4. Retry on provider errors: a provider wrapper, not an extension

A retry has to happen inside the sample, because only there can abort cancel the wait, and
only there is it known that nothing has been shown yet. A hook cannot do it: `post_step` sees
the failure after the reply is written, and abort never cancels a hook (model.md §8). So retry
stays what D15 made it, a wrapper `Provider`, and D15 D3 is amended to say it ships in `tau`.

- `RetryProvider` moves from `examples/retry.py` into `tau`, and `start_session` wraps every
  provider in it unless `load` turns `retry` off, so `tau`, `tau -p` and `tau serve` all retry
  by default. A program that builds its own
  `Driver` decides for itself.
- The policy is fixed: 0.15's (`tau_llm/retry.py`), plus the example's 500-with-`Retry-After`.
  It retries only while nothing has been yielded, on 408, 429, 502, 503, 504, 529, a 500 that
  carries `Retry-After`, and a transport error before any response. It never retries a plain 500
  (llama.cpp's deterministic errors). It makes at most 3 retries, waiting `Retry-After` if given,
  else 1, 2 and 4 s. A `Retry-After` over 60 s fails at once, with the wait in the message.
  `examples/retry.py` differs today and changes to match: it omits 504, makes 3 attempts (2
  retries), and caps a long `Retry-After` at 60 s instead of failing.
- **What the user sees.** Before each wait the wrapper yields a live `Retrying` delta (attempt,
  attempts, seconds, reason). The TUI shows `HTTP 529 overloaded: retry 1 of 3 in 2 s`, and Esc
  cancels, since the wait is part of the sample. `tau -p` watches its cursor and writes each wait
  to stderr as `tau: HTTP 529 overloaded: retry 1 of 3 in 2 s`, with or without `--verbose` (as a
  `Notice` is: D22 §3). A sample that runs out of retries fails with `... (still failing after 3
  retries)`. A `Retrying` delta does not count as output for
  the "nothing yielded yet" rule.
- **One switch, no options.** `load: {retry: false}` (or `tau ext disable retry`) turns the
  wrapper off, for a proxy that already retries or a provider being debugged. `tau ext list`
  shows `retry` beside the extensions. The policy has no options. `load` is read from the config
  files only (D19 §3), so there is no per-run switch: `--set load.retry=false` is a `ConfigError`
  that says so and names `tau ext disable retry`.

**Needs:** the `Retrying` delta (D15's delta list), and `load` accepting the key `retry` (M8).
**Dropped from 0.15:** jitter, and the code-only `options["retry"]`.

### 5. `@file` attachments

**What the user sees and types.** `explain @src/main.rs` sends the line with the file's text
attached after it:
`<attachment path="src/main.rs">…</attachment>`. A file over the limit, not UTF-8, or
unreadable becomes `<reference path="/abs/src/big.log" size="2.1 MB" reason="over the 10 kB
limit"/>` (the size is in bytes when it would read like the limit), so the model can `read` the
part it needs. A token is `@` followed by non-spaces, at the start of the line or after
whitespace, with trailing punctuation retried stripped; `@"a b.txt"` or `@a\ b.txt` (backslash escapes the next character, as in a shell) names a path
with spaces. A
`</attachment>` in a file's text is written `<\/attachment>`. `~` expands, and a relative
path resolves against the driver's working directory, as `complete` does. A word that names no
regular file, or names a directory, stays as text. A file named twice is attached once. A word
that looks like a path (a `/`, an extension, or `:LINE`) but attaches nothing is told to the typing
client by a `Notice` (`cursor.tell(..., to=client)`: a toast in the TUI, a stderr line under `-p`);
plain words (`@alice`) and code in backticks are silent.

**Key and options.** `attachments`, on by default. One option, `limit`, in bytes (default 10240).
**Hook:** `pre_input` rewrites the draft's text. The `User` node therefore holds exactly what
the model saw (I1). **Only input a person typed is expanded**: a draft authored by a `Frontend`
(D17 §1). What a tool or an extension wrote (`send`, authored `Extension`: a sub-agent's task, a
template's body) is never expanded (8E-5: except the arguments a person typed after a template's
command, which `skills` attaches itself, after the whole text, through `Cursor.options_of`), because the model writes the `agent` task and `@secret.txt` in
it would get past a `read` deny rule.
**Completion:** the TUI calls `Client.complete(cursor, "path", prefix)` for an `@` word; that
already works over the wire. `path` offers directories and regular files only (through links), so
no dangling link and no FIFO, and the TUI writes a name with spaces as `@"a b.txt"`.

**Needs:** nothing.
**Dropped from 0.15:** images (deferred, D15), the attachment bar and the transcript fold (D23's),
the `tau -p "@file"` raw-inline rule, and the no-match popup line.

### 6. Skills and prompt templates

**What the user sees and types.** Each template `NAME.md` and each skill `NAME/SKILL.md` is a
command, `/NAME args`, or `/skills:NAME args` when another extension also has `/NAME`. The
command's help is the file's `description`, which defaults to the first body line for a
template; a skill requires one. Running it sends the expanded text to the cursor as input
(`send`, authored `Extension("skills")`), so the `pre_input` gates see it (`attachments` does not
expand it: it is not a person's typing, §5), and the cursor samples.

- **Template expansion:** `$ARGUMENTS` is the whole argument text and `$N` (any number of digits)
  its words, split on whitespace with `"` grouping and `'` literal. A missing `$N` is empty text.
  `${N}` is `$N` before a digit. `$$` is a literal `$` (the `make` spelling: a backslash is an
  ordinary character in the arguments, and apostrophes are common in prose). A body with no placeholder gets the arguments appended after a blank
  line. 0.15's `${N:-default}`, `${@:N:L}` and `$@` are dropped. (8E-5: it was `$1`…`$9` as a
  shell splits them, and a missing one failed the command.)
- **A skill** sends `<skill name="NAME" path="/abs/…/SKILL.md">body</skill>`, the line "A relative
  path in a skill starts from the directory of its file: /abs/…", then the arguments.
- **Skills for the model:** when a session's first step begins (its walk has only the prefix) and
  a `read` tool is offered, `pre_step` writes one `System` node listing each skill's name,
  description and path, and saying "read the file to use it". Skills marked
  `disable-model-invocation: true` are left out. The list is then part of the prefix, which keeps
  the cache stable. A skill added later reaches new sessions only, as with `AGENTS.md` (D20 §1).

**Files.** `$TAU_HOME/prompts/*.md`, `$TAU_HOME/skills/*/SKILL.md`, and the same under the
**trusted** project's `.tau/`, where trust **pins** them as it pins `.tau/SYSTEM.md` (D20,
D19 §4): their hashes are in the trust record, they are read from the approved bytes, and a
changed file asks for `tau trust` again. They are read when extensions load, and again before a
command of the extension is listed or run when a stat says a file was added, removed or edited
(8E-5; no timer, no watcher; the skills list for the model stays written once). For one name the
project wins over the user, by D19's nearest-row rule. A template and a skill with one name in
one directory are a load error naming both. A name must obey the extension-name rule (letters,
digits, `_`, `-`). Frontmatter is `key: value` lines between `---` (`description`, `name`,
`argument-hint`, `disable-model-invocation`), with no YAML library.

**Key and options.** `skills`, on by default. One option, `advertise` (default `true`): write the
list for the model.

**Needs:** built-ins built with the `LoadedConfig`. Commands are fixed when the driver is built,
so the files must be read at load, and that needs the cwd, `$TAU_HOME` and the project's trust.
`_BUILTINS` factories take no arguments today.

**Dropped from 0.15:** `--prompt-template`, `--skill`, `--no-prompt-templates`, `--no-skills`,
the `prompt_templates` and `skills` config lists (use `load` or `-e`), `{{skills}}` placement,
and the `/skill:NAME` spelling.

### 7. Permissions

**What the user sees.** A tool call that a rule says to ask about holds the cursor with a
question: `bash: git push origin master` → `yes | always | no`. `yes` runs it. `always` runs it
and appends the exact rule (`bash:git push origin master`) to `allow` with `save_options`, so it
holds in every session (bash_guard's mechanism). `no` gives the call
`Err("the user refused this call")`, which the model reads. A declined question (abort, go-to) is
a refusal too. The questions of a batch are asked at once, and a sub-agent's are asked on its own
cursor.

**Key and options.** `permissions`, on by default, asking nothing until configured: that is what 0.15 users and their `-p` scripts had, and
turning it on is one `/config ... --save`, which the TUI's first-run text can mention.

| option | default | meaning |
|---|---|---|
| `ask` | `[]` | rules that ask |
| `allow` | `[]` | rules that run without asking (`always` adds here) |
| `deny` | `[]` | rules refused without asking |

A rule is `TOOL` or `TOOL:GLOB`. The glob matches `bash`'s command or the path argument of the
base tools, and an extension's tool takes only the bare form. `deny` is checked first, then
`allow`, then `ask`; a call no rule matches runs. `/config permissions ask=bash,write,edit --save`
is the one line that turns on asking. Protected paths are `deny: ["write:/etc/*",
"edit:/etc/*"]`.

**Scope (decided 2026-11-07).** Permissions is an example guardrail for the built-in file tools
(`read`, `write`, `edit`, `grep`, `find`). It catches accidents; it is not a security boundary,
and `bash` is out of scope: a `bash` rule is a pattern over the command text and nothing more.
Real enforcement belongs below the agent, at the OS level: a separate user, mount namespaces, or
Landlock.

**Hook:** `pre_tool`, asking through `node.ask`. **Headless:** `tau -p` answers with `--answer`
rules (D22) or declines, so the call is refused and the run never hangs.
**Not a sandbox:** a later extension's `pre_tool` can change the outcome (model.md §7). It
guards against the model, not against code the user loaded.

**Needs:** nothing in core. Under `-p` it needs D22's `--answer`.
**Dropped:** 0.15 had only examples. bash_guard's `truncate` answer and its unattended timeout
are not carried over: `--answer` covers the unattended case.

### 8. Sub-agent

**What the user sees.** The model gets a tool, `agent(task, to=None)`. Each call opens a cursor
labelled `agent:N`, which the TUI shows working beside the main one. The call's result is
`agent:N answered:` and its final reply. With `to="agent:N"`, the call reopens that agent (D17)
and sends it a follow-up, so it remembers its earlier work.

**How.** The tool opens the cursor at the end of the session's prefix: a fresh context with the
same system prompt and context files, which also shares the cache prefix. If the `model` option
is set it first appends a `Config` naming that model. Then it `send`s the task, `await rest`s,
and reads the reply. An ending other than `Done` is a `ToolError` naming it. A finished agent is
closed, and abort cancels the tool, whose `finally` aborts its cursors (model.md §7).
`pre_sample` offers the agent's cursors the `tools` option's tools, or every tool except
`agent`, so there is no recursion and no depth counter. Several `agent` calls in one reply run
at once. `StepLimit` bounds each agent, and `usage` counts each one.

**Key and options.** `agents`, **off by default**: the tool would be in every request,
and 0.15 had it only as an example. It is the first thing cut if 8E runs long (§11). Options: `model` (none: the branch's own) and `tools`
(default `[]`, meaning all but `agent`).

**Needs:** a built-in that is off by default, which D19 deferred to "Phase 8's first". It would be
`load: {agents: true}`, or `tau ext enable agents`, and `tau ext list` would show
`off (default)`. Optional: `opened_by` (not spiked) would let a frontend draw who opened whom;
labels serve for 0.16.0.
**Dropped from 0.15's `delegate` example:** child processes, chain mode, `tasks[]` (several calls
do the same), `max_usd` and `max_seconds`, and forcing parallel children read-only.

### 9. Prune: compaction without a model

Prune has no 0.15 counterpart. Phase 0's plan and Phase 1's model.md defined it ("post-expiry
deterministic rewrite: old tool outputs, old thinking, branch from the LCA"); that text was lost
when the docs were cut to size. John's design (2026-11-02) makes it concrete. It is one way of
many, picked to start.

Prune rewrites the walk deterministically, by the user's turns. A turn is a `User` node and
everything under it up to the next `User` node. Number the turns `0..N`, where N is the latest:

- **Turns 0 to N-2** become a transcript. Each turn is its user text, a count of what was left
  out, and the agent's last reply in that turn. Agents usually sum up their work before they
  stop, so that last reply serves as the summary. The transcript is **prepended to the user text
  of turn N-1**, in one derived `User` node, so two user messages never sit side by side (some
  providers reject that or merge it silently):

  ```
  <pruned>
  User: let's do task one
  [pruned: 10 messages, 44 tool calls]
  Assistant: Done. I did task one. Summary: ....
  User: let's do task two
  [pruned: 14 messages, 52 tool calls]
  Assistant: Finished. Here's what was changed: ...
  </pruned>
  ```

- **Turn N-1** keeps the agent's messages and its tool calls, with each call's outcome reduced
  to ok or error. It drops the reasoning text and the content of the tool results.
- **Turn N** keeps its tool calls and their results. It drops the reasoning text and, as in
  N-1, long call arguments. The result (and `edit`'s diff) carries what changed.
- **In turns N-1 and N**, any string argument of a call over 200 characters becomes `[pruned: N
  chars]`, so a long `edit` or `write` call shrinks. Short ones (`path`, `pattern`, a one-line
  `command`) stay. This needs no list per tool and works for extension tools too.

The result is a new line built as compaction builds one (§2): copied nodes carry `copied_from`,
and rewritten ones carry `derived_from` pointing at the node they replace. The cursor moves onto
the new line. The old line stays in the tree.

**Key and options.** `prune`, on by default. One option, `auto` (default `false`, below).

**Triggers.**
- `/prune` is the command. It is for switching tasks, and it is just as useful with a local
  model, where nothing is cached.
- Automatic pruning, when the provider's prompt cache has gone cold, is an option and off by
  default; it waits for a way to know the cache's state, so 8E ships `/prune` only. Before the cache expires, rewriting the prefix throws away a cache that is still
  paying for itself. After it expires, the next request costs the full price anyway, so that is
  when a shorter prefix is free.

**Needs:**
- M1 (`copied_from` and `derived_from`), which compaction needs too.
- A way to know the cache has expired, for automatic pruning only. The candidates are the
  provider's `cache_state(walk)` (planned in Phase 5, never built) or the time since the last
  reply compared with a per-provider cache lifetime. `/prune` needs neither.
- No model, no `context_window`, no token estimate.

### 10. What is missing today

| # | piece | where | for |
|---|---|---|---|
| M1 | `copied_from`, `derived_from` built in core: node fields, the log record (missing fields default, so old logs load), `append` and `write` take them, the codecs, the wire schema. No store `VERSION` or `PROTOCOL` bump: both mark released formats and 0.16.0 is the first release of each (D12, D21 §8) | Rust core, store, bindings, `_protocol` | compaction; usage skips copies |
| M2 | `ModelConfig.context_window` (optional positive int) | `_config.py`, D13 | compaction, usage `ctx` |
| M3 | `ModelConfig.price` `{input, output, cache_read, cache_write}`, USD per million, all four required | `_config.py`, D13 | usage |
| M4 | `handle.resolved_model(name=None) -> ModelConfig` (the resolved model, as at a sample) | `_handles.py` | compaction, usage |
| M5 | `cursor.model(at=node)`: the model at any node, the fold the driver already has | `_handles.py` | usage |
| M6 | normalized `usage.written` (cache writes; Anthropic's `cache_creation_input_tokens`) now; `cached` absent when the server reports none (today `0` in `openai.py` and `anthropic.py`, which a cache notice cannot tell from a miss; Gemini omits zero counts, so there an absent count stays `0`) only after checking what LiteLLM sends | providers, D15 §11 | usage |
| M7 | abort of a cursor ends its hooks' `rest` waits, returning a distinct result, not `None` (the type is fixed when it is built), as abort declines their questions | driver, `_handles.py` | compaction's Esc |
| M8 | the live delta `Retrying`; `RetryProvider` public; `start_session` wraps unless `load` has `retry: false` (`load`, `tau ext list`, `tau ext disable` know the key) | providers, D15 | retry |
| M9 | built-in factories take the `LoadedConfig` | `_extensions.py`, D19 | skills |
| M10 | a built-in that is off by default | `_extensions.py`, D19 | agents |
| — | `--answer` | D22 | permissions under `-p` |

Only M1 touches Rust or the log's record shape (M6 changes what providers put in `meta`, M8 adds
a `Live` delta kind to the wire schema). Everything else is Python, and none of it adds a hook
point or a node kind.

### 11. Build order and cuts

1. **retry** (M8): the smallest, and every user is helped by it.
2. **attachments**: needs nothing.
3. **skills** (M9).
4. **usage** (M2–M6): this lands the `ModelConfig` and provider changes compaction also uses.
5. **prune** `/prune` (M1): it lands the provenance fields and the new-line building that
   compaction reuses, with nothing about the result left to a model, so the line building is
   tested on its own.
6. **compaction** (M7, on prune's line building): the largest.
7. **permissions**, once D22's `--answer` exists.
8. **agents** (M10).

**Cuts.** Automatic pruning on a cold cache waits for a way to know the cache's state. If 8E
runs long, agents is the first cut: 0.15 had only an example, and gamemaster and forest already
show the pattern. The other six cover regressions 0.15 users would notice.

## Alternatives

| option | why not |
|---|---|
| The summary from `handle.sample(walk)`, a one-off call outside any step | A new handle API (model.md §7 lists it as not built). A summarizer cursor needs none, is visible to every client, and is counted by usage. |
| Copies recorded in a `Custom` map node instead of `copied_from` | Every reader (usage, the TUI, export, the estimate) would rebuild the map. D3 already accepted the fields. |
| Retry as a `post_step` extension | It sees the failure after the reply is written, and its wait cannot be aborted (hooks are never cancelled). |
| A `retry` key on `ModelConfig` | No one has asked for a different policy. Add it when someone does. |
| Retry always on, with no switch | A proxy that already retries, or a provider being debugged, needs it off. `load: {retry: false}` is the switch every built-in already has. |
| Cost stored in `meta` at sample time | Stored data that is derived. A price fixed in the config re-prices the whole session, which is what a user editing a price wants. |
| Prompt templates as one `/prompt NAME` command | Typing `/prompt review` instead of `/review` is worse than 0.15. Generated commands cost nothing. |
| `@file` expanded by the frontend | Each frontend would do it again, and a remote frontend would read its own disk, not the session's. |
| Permissions as a mode (`ask`/`allow`/`auto`) | Rules cover a mode (`ask: [bash, write, edit]`) and also say which commands. |
| Sub-agents with the parent's context (open at `at`) | It costs the whole context on every call, and the model writes better tasks for a fresh one. Open at `at` with your own extension if you want it. |

## Rulings (2026-11-02)

Decided as the drafts recommended, except where noted (the changes are in the sections above).

1. **The summary's sample** is a summarizer cursor, not `handle.sample` (§2).
2. **`copied_from` and `derived_from`** are built in core (M1), additive, with no store `VERSION`
   or `PROTOCOL` bump.
3. **Permissions** ask nothing until rules are configured (§7).
4. **Agents** ship off by default and are the first cut (§8, §11).
5. **Retry** is on by default with an off switch, `load: {retry: false}` (§4). Not "no key".
6. **Price** is USD per million tokens, all four fields required; a model that writes no cache
   repeats its input price for `cache_write`; a missing field is a `ConfigError`.
7. **Project prompts and skills are pinned by trust**, like `.tau/SYSTEM.md` (§6). Not the
   draft's "trusted but not pinned".
8. **No compaction on a provider's "prompt too long"**: the trigger is proactive only; `/compact`
   then a step recovers.
9. **`context_window` comes from the config only** for 0.16.0.
10. **Prune's content arguments:** any string over 200 characters becomes `[pruned: N chars]` (§9).
11. **Prune's transcript** is prepended to turn N-1's user text in one derived `User` node (§9).
12. **Prune's turn N** drops long arguments and keeps its tool results (§9).
13. **`--no-extensions`** affects no built-in (§1). **`handle.model_config`** is renamed
    `resolved_model`, since `tau.model_config` is public. **Compaction's `pre_sample`** skips its
    own summarizer cursors (§2). **M7**'s abort is a distinct result. **`usage.written`** is
    added now; `cached` absent-when-unreported only after checking LiteLLM (§3).

14. **Compaction has two limits** (owner, 2026-11-03). The soft limit lets the turn run on and
    compacts when it ends; the hard limit interrupts the turn before the next sample (§2). It
    replaces the one trigger of the first draft; 0.15's second, softer limit was dropped for being
    a percentage of the same thing, and this one is the owner's. (The list of what was dropped in §2
    said "the soft limit" in the first draft, which this ruling reversed; it no longer does.)

## Glossary entries proposed

- **prefix (of a walk)**: the root `Config` and the `System` nodes before the first other
  message. Compaction keeps it, and a sub-agent starts from it.
- **`Compaction`** (`compaction`, `Compaction.Options`: `auto`, `reserve`, `keep`, `model`); the
  **head**, **tail** and **cut** of a compaction; the **summarizer cursor** (label
  `compaction:N`).
- **`context_window`** (`ModelConfig`): the model's context size in tokens; optional.
- **`price`**, **`Price`** (`ModelConfig`): `input`, `output`, `cache_read`, `cache_write`, in USD
  per million tokens.
- **`written`** (normalized usage): prompt tokens written to the cache. And amend **meta**:
  `cached` is absent when not reported, once LiteLLM's behaviour is checked.
- **`resolved_model`** (handle): the resolved `ModelConfig` of a qualified name; and
  `model(at=)`.
- **`Usage`** (`usage`, option `cache_notice`); **cache notice**.
- **`RetryProvider`** (moved from the example) and **`Retrying`** (live delta).
- **`Attachments`** (`attachments`, option `limit`); **attachment** and **reference** (the two
  tags).
- **`Skills`** (`skills`, option `advertise`); **template**, **skill**, **skills list**.
- **`Prune`** (`prune`, option `auto`); the **pruned transcript**.
- **`Permissions`** (`permissions`, options `ask`, `allow`, `deny`); **rule**.
- **`Agents`** (`agents`, options `model`, `tools`); the **`agent`** tool; **sub-agent**
  (label `agent:N`).
- **off by default** (a built-in that `load` must turn on).
- Amended: **`load`** (the key `retry`).

## Built: 8E-1

Retry (§4), `@file` attachments (§5), skills and prompt templates (§6), with M8 and M9 and the
shared pieces of §1. Where the build differs from the text above, or the text did not say:

- **`Retrying(attempt, attempts, seconds, reason)`** is a `Delta` (a new `Live` delta kind: wire
  schema regenerated, no `PROTOCOL` bump). `attempt` counts retries (1 to 3), `reason` is the first
  line of the error, cut at 200 characters. The driver forwards it as a `Live` and does not put it
  in the reply's parts: it says what the sample is doing now. (Amended 2026-11-11, John: a late
  client must not be misled. A late watcher's catch-up during the wait is that `Retrying` with the
  seconds left, rounded to 0.1 s, and nothing once the wait is over; before, it got nothing, and
  saw a busy cursor with no reason for up to 60 s.) The
  TUI shows it as a status-bar line (`HTTP 529: overloaded_error: Overloaded: retry 1 of 3 in 2 s`)
  until the next delta or the end of the step. `examples/ratatui` must handle the new kind.
- **`RetryProvider(inner, *, sleep=asyncio.sleep)`** has no `attempts` or `max_wait`: the policy is
  fixed. A `Retry-After` of exactly 60 s is waited for; over 60 s fails with the wait in the message.
  `examples/retry.py` now uses the shipped class; its test moved to `tests/providers`.
- **`retry` in `load`** is a source row (`built-in`, origin `built-in (provider wrapper)`, no
  extensions), so `tau ext list`, `enable`, `disable` and `tau config check` know it. The
  `--verbose` start report prints it without the word `extension`.
- **M9**: `_BUILTINS` factories take the `LoadedConfig`. `tau ext list` and `switch_source`, which
  have no `LoadedConfig` of their own, build one the way they already built their inputs (never
  asking about trust), so an untrusted project's prompt files are not read there either.
- **`attachments`**: `Attachments(cwd)`; the loader passes the process's working directory, which
  `start_session` has made the session's. `limit` must be at least 1 (a bad value fails the step
  like `step_limit`'s). Following the text, a word that names no regular file stays text, a missing
  file included; only a file that exists and cannot be inlined becomes a `<reference>`, with
  `unreadable: <reason>`. `~` expands with the process's home, which for a remote client is the
  server's.
- **`skills`**: `name` in the frontmatter, which §6 lists without a rule, must equal the file's
  name (a mismatch is a load error) rather than rename the command. `argument-hint` becomes the
  description of the command's `args` field (what a form shows); the command's help stays the
  `description`. Unknown keys and indented lines are ignored (a Claude Code skill loads); a block
  scalar (`description: >`) is refused. Every problem with a file (name, encoding, frontmatter, no
  description, a template and a skill of one name in one directory, a command named `config`) is an
  `ExtensionLoadError` naming the file and ending in `tau ext disable skills`. A skill's tag is
  `<skill name="NAME" path="/abs/SKILL.md">`, a newline, the body, a newline, `</skill>`. Hidden
  files and directories, non-`.md` files in `prompts/` and a `skills/` child without `SKILL.md`
  are ignored. The user's files are read through the filesystem as they are; the project's from
  `Trusted.prompt_files`.
- **Trust**: `Trusted.prompt_files` (path under `.tau/` to bytes), `trusted.json` gains
  `prompts_sha256` and `prompts` per project, and the report lists the files and what changed. A
  record without `prompts_sha256` is not approved, so every project trusted before 8E-1 asks once
  more. Only `prompts/*.md` and `skills/*/SKILL.md` are pinned and read; other files in a skill's
  directory are the model's to `read`, unpinned. `_trust.py` would have passed 800 lines, so its
  file-reading helpers (`TrustError`, `safe_text`, `read_regular`) moved to `_project_read.py`.
- **Public**: `Attachments`, `Skills`, `RetryProvider` and `Retrying` are exported.
- **`tau -p "/review x"`** prints the reply: when a command's run sent input, the reply is printed
  after the command's own result and the run is held to a message's rules (D22 section 3).
- **Generated commands yield their bare name.** `Skills.yields_names = True`; `Form.yields` carries
  it on the wire (additive). A bare `/NAME` means the non-yielding commands that have it; only
  `/skills:NAME` is the file. Without this a template named `model` or `name` broke the built-ins.
- **Records from before 8E-1**: a missing `prompts_sha256` is the hash of no prompts, so only a
  project that has prompt or skill files is asked again, with "added" lines. (The first build asked
  every project, without a reason.)
- **Options are checked at the start.** `start_session` refuses what `option_problems` finds
  (`attachments.limit: 0`, `step_limit.limit: 0`, which used to fail every step), options on an
  extension that has none, and `extensions.retry`.
- A listed skill is read live from disk by the model; trust pins what `/NAME` sends.
- The `--no-extensions` hint is not offered for a built-in's failure.

## Built: 8E-2

Usage (§3) with M2 to M6. Where the build differs from the text above, or the text did not say:

- **Money is `Decimal`**, not integer micro-dollars. Prices are decimal numbers (0.30, 3.75) that
  would need a fixed number of decimal places as integers; `Decimal` reads them as written (a JSON
  float is taken as its shortest text, so `0.15` is 0.15) and a sum of any number of replies is
  exact. Cost is `tokens × price` summed, then scaled by 10⁻⁶ (exact); only the display rounds
  (half up: `$1.27` from a dollar, `$0.014` below, `<$0.001` under that).
- **M2/M3**: `ModelConfig.context_window` (positive int) and `ModelConfig.price`
  (`tau.Price`, four `Decimal`s). Both are model keys in the layered files (D13 amended); a `price`
  object merges by field like `options`. A missing field, an unknown one, a negative or non-number is
  a `ConfigError` at the model. (There is no `tau config show`; `tau config check` reports both.)
- **M4/M5 live on `TreeHandle`**, so `Cursor`, `NodeHandle` and a command's tree handle all have
  them: `model(at=None)` (the cursor's node, the batch's node, or `at`; a tree handle must give
  `at`) and `resolved_model(name=None)` (defaults to `model()`). `OptionsReader` gained `resolved`.
- **M6**: `usage.written` and an absent `cached` as in D15's amendment. LiteLLM (1.106.6, read from
  its source) sends `prompt_tokens_details.cached_tokens` (0 on a miss) and `cache_write_tokens`
  for an Anthropic model, so there a miss is `cached: 0`; a server that sends nothing (llama.cpp)
  now has no `cached`. `examples/cache_rate.py` and `harness_sense.py` read `cached` as optional.
- **The status line** is `Cursor.show` under the name `usage` (the TUI prints it as
  `usage: 12.3k in · …`; the wire is unchanged, no schema or `PROTOCOL` change). It follows the
  text above with these additions: `cached` is left out when unreported; `ctx` is the reply's
  `input + output` over the window; the session total ends in `+` when replies of an unpriced
  model were left out of it; a reply with no usage shows `MODEL reported no usage`.
- **`/usage`** prints a row per model that reported usage (samples, input, cached share, cache
  writes, output, cost, with `-` for an unreported share or a missing price), a `total` row for
  more than one model, and lines for replies that reported no usage and models with no price. A
  model the catalog no longer has is marked `(not in config)` and costs `-`, not an error.
- **The cache notice** follows §3, for a model with a `price` only (8E-5: "full price" means
  nothing without one, and the earlier `try provider "anthropic"` advice for a model on the
  `openai` provider is gone). Its previous reply is the nearest `Assistant` on the walk, which
  must count, be of the same model and under 300 s older.
- **The seam for copies (8E-3)**: `tau._usage.counted(node)` is the one rule for "this reply
  counts" and returns true for every reply. Once M1 gives nodes `copied_from`, 8E-3 changes its
  body to `node.copied_from is None`; usage and the cache notice both go through it, and a
  copy then no longer counts or serves as a notice's previous reply.
- **Inconsistent counts are no usage** (review fix): a provider's normalizer (`note_usage`) drops
  `usage` when `cached + written` exceeds `input`, as it does for a malformed count (`usage_raw` still
  holds what the server said). The extension never fails a step over a past node: a reply whose
  usage cannot be read (from any provider) is recorded as read once, counts as "reported no
  usage", and `/usage` names its node and the reason; its own status line says why too. A reply
  whose provider kept `usage_raw` but could not normalize it is the same case (8E-5: its reason is
  "the provider could not read the usage ..."). A reply aborted before any usage was reported says
  `reply was aborted and reported no usage` on its status line, and `/usage` counts it apart from
  the silent ones. One formatter, `tau._usage.tokens`, writes counts (`k`, `M`, `G`) for the
  status line, `/usage` and the TUI's reply badge.
- **Display rules**: money rounds half up to three decimals below a dollar and two from a dollar, an
  amount that rounds up to a dollar is `$1.00`, exactly nothing is `$0`, and under `<$0.001` is
  `<$0.001`; `999,950` tokens is `1.0M`. The `/usage` total row ends in `+` like the status line when
  a model could not be priced.
- **`TreeHandle` requires its `options`** (the driver's `OptionsReader`); a program outside tau gets
  one as a command's `TreeHandle` argument.
- **`--verbose` under `tau -p`** prints every extension's shown line when it changes (D22 section 7),
  usage's included. The cache notice reaches stderr as every notice does.

## Built: 8E-3

Prune (§9) and compaction (§2) with M1 and M7. `copied_from` and `derived_from` came first (core,
store, bindings, stub, wire schema), then `/prune`, which lands the new-line building
(`tau._lines`, private), then compaction on it. Where the build differs from the text above, or
the text did not say:

- **M1.** `Node` gains `copied_from` and `derived_from`, each one optional node id. D3 and model.md
  said "lists"; one id each is what every use needs (a summary derives from the last node it
  replaced, a copy repeats one node), and a list can be added later without breaking a reader that
  takes the first. Core has `Provenance` (the two ids) and `Session::append_from` / `write_from`;
  `append` and `write` are those with none. Both ids must name a node (`NoSuchNode`, nothing
  written). The log record writes a field only when set, and a record without them loads as none,
  so a log from before 8E-3 loads (`a_record_from_before_meta_loads_with_none` and
  `tests/core/test_provenance.py`); there is no store `VERSION` or `PROTOCOL` change. A fork
  (D18) renumbers a source that is in the walk it copies and drops one that is not. Python:
  `TreeHandle.append(parent, body, copied_from=, derived_from=)` and `Cursor.write(...,
  copied_from=, derived_from=)`. The wire schema's `Node` lists them as optional.
- **`counted` also skips derived replies.** The seam in `tau._usage` is `copied_from is None and
  derived_from is None`, not `copied_from is None` alone: a pruned reply keeps the `meta` (and so
  the `usage`) of the sample it came from, and would otherwise be counted again. The estimate for
  compaction uses the same rule.
- **Copies are written by the extension.** A handle never passes `by` (model.md), so a copied node's
  `by` is `Extension("prune")` or `Extension("compaction")`, not its original author; `copied_from`
  names the original. A `Results` node keeps each outcome's own `by`. This means a future automatic
  prune cannot tell a person's input from a copy by `by` alone; it should follow `copied_from`.
- **The line.** Both build the new line under the last node of the prefix (`prefix_length`): copies
  of the nodes they keep, a computed node with `derived_from`, appended with no cursor moving,
  then the cursor moves onto the last one. The prefix is shared, not copied. Nothing is awaited
  between the first append and the move, so a line is whole or absent.
- **Prune** (`/prune`, `tau.Prune`). Followed §9 and Rulings 10 to 12 exactly. The choices the text
  left: "messages" in `[pruned: N messages, M tool calls]` counts the turn's replies other than the
  one shown, "tool calls" counts the calls of every reply of the turn; either is omitted when both
  are 0; the transcript is `<pruned>`, the accounts, `</pruned>`, a blank line, then the user's text.
  An old turn's last reply is the last reply that has text. A user message that already begins with
  a transcript (from an earlier prune) is flattened into the new one, so pruning again never
  nests. A reply left with no parts once its reasoning is dropped is left out. An outcome becomes
  `ok` or `error` (an interrupted one stays); the `Config` and `Custom` nodes of an old turn are
  copied ahead of the transcript so the model and options stay. A node is derived only if it
  differs from the one it replaces, so a conversation with nothing to prune says `nothing to
  prune` and writes nothing, and pruning twice in a row does the second time. The option `auto` is
  there, as §9 says, and a `true` is refused (`prune.auto is not built yet`): the triggers wait on
  a way to know the cache's state. The command's result is `pruned ~46k → ~6k tokens (node N; the
  old line is still in the tree)`, characters over four, as everywhere (D26, 8E-6: by the ceilings of both
  lines, shown with `≤`).
- **Compaction** (`tau.Compaction`, `/compact [focus]`, option `auto`, `reserve`, `keep`, `model`).
  As §2. The tail begins at the nearest `User` or `Assistant` node at or before `keep` tokens
  from the end; a head with no `User`, `Assistant` or `Results` node is `nothing to compact yet`.
  The summary is wrapped as `The earlier part of this conversation was summarized:` and a
  `<summary>` tag. The notice is the text of §2, from the trigger's estimate before and a
  character count after, to every client and as `/compact`'s result. `compaction:N` takes the
  first N no label has. The prompt is in `tau._compaction`; it asks for sections (goal,
  constraints, progress, open work, facts) and to carry an earlier summary forward.
- **Failures.** A summarizer that fails, is halted, replies without text, or is cut off at the
  model's output limit (`Stop.Length`: a truncated summary would be kept as if whole) fails the hook
  with the reason, and nothing is written. Also refused up front: a `keep` that is not smaller
  than `context_window − reserve`, since every sample would then compact again (`keep (950) leaves
  no room before the reserve ...; lower either`).
- **M7.** `rest` called from a handle whose own cursor is aborted, or sent elsewhere, while the
  wait is out returns `Ending.Aborted()` at once (the type was `Ending | None`; `None` still means
  "not known"). A hook is not cancelled by an abort, so this is how a hook's wait ends; a tool is
  cancelled anyway. The cursor waited on is left alone: compaction closes its summarizer. Compaction's
  `pre_sample` then returns a halt reason, because core runs a step's sample after the `pre_sample`
  hooks even when it was aborted meanwhile (the halt keeps the ending `Aborted`). `Esc` therefore
  leaves the cursor where it was and the tree with no copy or derived node; the summarizer's partial
  reply, if any, stays on its own branch. An abort during `/compact` cancels the command; its
  `finally` closes the summarizer.
- **Recovery.** `/compact` after a provider's "prompt too long" works as Ruling 8 says: the failed
  step leaves the cursor on the input; `/compact` summarizes, and a step samples the new line.
- **The estimate** counted a call by its arguments only; since D26 (8E-6) the count of a node is
  its bytes, call id, name and arguments, and the tree screen's walk size uses `Cursor.count`'s
  meter (`tau._meter`).
- **The tree screen** draws it (D23 "Built: tree view" amended): `⧉` after a derived node's text,
  `≡ (copy of N)` after a copy; the old line is the folded stub it already was, with its size; a
  closed summarizer cursor is the dimmed `○ N compaction:1`; the detail pane starts a copy or
  derived node with `copied from node N` or `derived from node N` and `walk before: X nodes, about
  Y tokens` / `walk after: ...`; `p` highlights the source node (unfolding what hides it). The
  help line dropped `←→ fold` for `p source`. `examples/ratatui` gets the two fields from the
  regenerated schema types and draws the two marks on its rows; its detail pane and `p` are not
  built.
- **Not built:** automatic prune (§9, waits for a way to know the cache's state); automatic
  compaction on a provider's "prompt too long" (Ruling 8).

### Review fixes (8E-3)

- **Core: an abort while the `pre_sample` hooks run** now ends the step without sampling (the
  `Sample` position hops to the tools position, then `post_step`, when the ending is not `Done`;
  the hook's draft is dropped). Before, any awaiting `pre_sample` hook (a question included) let the
  sample go out after an abort. Compaction's halt-on-abort workaround is gone. Test in Rust
  (`abort_while_the_pre_sample_hooks_run_ends_the_step_without_sampling`) and through the Harness.
- **`keep` has no fixed default.** Unset, it is `min(20000, room / 2)` where room is
  `context_window − reserve` (20000 when the model has no window, for `/compact`), so a 32k model
  works with the defaults (room 16384, keep 8192). A `keep` that is set and does not fit the room,
  or a `reserve` that leaves none, fails the step with a message.
- **A compaction that gains nothing is refused.** Before summarizing, if the prefix and the tail
  alone reach the room, the step fails naming `keep`; before writing, a line whose estimate is not
  smaller than the branch's fails with both figures and writes nothing. Prune writes nothing
  (`nothing to prune: it would not be shorter`) when its line is not smaller.
- **Prune cuts long strings at any depth** of lists and objects (`edits: [{old, new}]`).
- **A fork (D18) strips `usage`** from the `meta` of a copy or derived reply whose source it leaves
  behind (`meta` is rewritten without that one key), so it counts as nobody's sample; sources that
  are in the walk are kept, renumbered.
- The tree help line is `Enter go  v view  o open here  l label  [ ] cursors  p source  f follow
  ←→ fold  Esc leave`.
- **Left as it is, for the owner:** the summarizer's request can end with two adjacent `User` nodes
  (the branch's last input, then the summary prompt).

### Ruling 14 built (8E-3)

- Options `hard` and `soft` (token counts, unset by default) beside `reserve`. Hard is `hard` or
  the window less `reserve`; soft is `soft` or 80% of hard. `soft` above `hard` is refused when the
  options are read (`soft (700) is above hard (600)`), or when the one that is unset is derived
  (`soft (950) is above the hard limit (900)`); a hard limit outside the window is refused too.
- The soft trigger is `post_step` on a `Done` ending whose cursor is at a reply with no `Call`
  part; a step that ends on `Results`, or an aborted, halted or failed one, never triggers it. The
  line it writes must end under the soft limit (`prefix` and `keep` tokens must be under it, else the
  step fails naming `keep`), so the next message does not compact again. `keep`, when unset, is
  `min(20000, soft / 2)`; a `keep` that is set must be under `soft`. This replaces the "half the
  room" above.
- At the hard limit on new input the branch ends in a `User` node not yet answered; the
  summarizer is opened at that node's parent and sent the input text and the summary prompt as one
  `User` node (by `Extension("compaction")`), as prune merges, so two user messages never sit side
  by side. This is the only case; it supersedes "left as it is" above. (8E-6: the summarizer is
  still opened at the parent, but is sent the prompt alone: the input is kept verbatim in the
  tail, and the message that did not fit no longer makes the summarizer's request not fit.)

### Second review fixes (8E-3)

- **The soft trigger needs a turn.** `post_step` compacts only if this step itself committed the
  reply the cursor is on (`post_reply` records it, `pre_step` clears it). Going back to a reply, a
  cursor opened on one, or an item waking a cursor at rest end a step there without a turn, and do
  not compact.
- **`reserve` unset is `min(16384, window / 4)`**, so an 8k model has a hard limit of 6144; a
  `reserve` that is set and leaves no room still fails.
- **Whole-line checks** (superseded by D26, stage 8E-6, which counts the cut, the checks and the
  notice in the bill's unit and checks before paying; kept as history). `before` and `after` are both the character estimate (the exact usage count
  still decides *when*, but is not comparable with a line that has no replies of its own). A line is
  refused, and nothing written, if `after` is not below `before` or not below the limit that
  called. When the prefix and the tail alone reach the soft limit, the soft attempt does not run a
  summarizer: the clients are told once per cursor what is too big (`the prefix (system prompt, ...)`,
  `the newest node alone`, or `the newest nodes kept (keep = N) ...; lower keep`) and nothing fails.
  The same condition at the hard limit fails the step with that text.
- `keep`, `hard` and `soft` below 1 are a `ConfigError` (`keep = 0` used to raise `IndexError`).
- **Core: an abort while `pre_input` hooks run** now clears the rest of the input queue, which stay
  pending (`input_job` built it once and never rechecked); the input being offered when the abort
  came still commits, since its hooks are not cancelled. No new transition.

## Built: 8E-4

Permissions (§7) and agents (§8) with M10. Where the build differs from the text above, or the
text did not say:

### Permissions (`tau.Permissions`, `_permissions.py`)

- **Rules.** As §7. A rule is split at its first `:`, and only when `TOOL` is `bash` or one of
  `read`, `write`, `edit`, `grep`, `find`; any other text is a tool's whole name (a name may hold a
  colon: `srv:tool`). A glob is `fnmatch`'s, case-sensitive, over the whole argument: `*` crosses
  `/` and newlines, `?` is one character, `[abc]` a set, a backslash is ordinary. An empty rule or
  an empty glob is an option error (`start_session` and `tau config check` refuse it; a call made
  while one stands is refused with the message, so a bad `deny` never lets a call through). A rule
  naming a tool no extension provides (`Bash`, `wirte`) is a `ConfigError` at the start, an error in
  `tau config check` (`Options.tool_problems`), and a `ConfigError` from `/config permissions ...`
  (checked against the driver's tools before anything is saved); a tool of a built-in extension
  that is not loaded (`agent`, with `agents` off) is known, so a rule for it is no error; rules for
  a tool a program gives its `Driver` directly are not known to `start_session`. A bare rule matches every call of the tool.
- **Paths.** The session's working directory (`Permissions(cwd)`; the loader passes the process's,
  which the start made the session's) is joined to each path argument and made real
  (`os.path.realpath`: `..`, `//`, `.`, symlinks). A path call names its `path`; `grep` also names
  each entry of `files`; a `grep` or `find` that names neither searches the working directory. A
  call whose path arguments are missing (`read`, `write`, `edit`) or not strings matches no
  pattern rule; the bare rule matches it. `deny` and `ask` fire if any named path matches, as
  written or resolved. `allow` needs every named path to match, resolved. So `write:/etc/*` holds
  for `/tmp/../etc/x`, `//etc/x`, a relative path or a symlink that lands in `/etc`, and
  `grep:/etc/*` for `files: ["/etc/shadow"]`. A rule that starts with a literal relative component
  (`read:src/*`, `./out/*`) starts in the working directory, like the tool's own paths: it matches
  the path as written and, joined to the real working directory, the one it lands on, so an
  `allow: ["read:src/*"]` works. A rule that starts with `/` or a wildcard is used as it is
  (`write:*/secrets/*` matches the path it lands on). `tau config check` has no working directory
  to resolve against and says nothing about a relative rule.
  **Searches.** `grep` and `find` read everything under their root. A `deny` or `ask` rule fires
when a named path matches, as for any path tool. A `deny` also reaches the files a search
finds: `pre_tool` adds `exclude: [PATTERN]` to the call's arguments for each deny rule about the
tool, the rule's pattern over real absolute paths (a relative rule starts in the working
directory). It does so for every search, not only one whose root holds the pattern, because a link
under any root can lead to a denied file. The
tool (not the extension) then leaves out every walked file whose absolute path, or whose real path
after symlinks, matches (`walk.rs`, `Exclude`), before it reads it: nothing denied reaches the
output or the overflow file. `exclude` is not in the tools' schema; it is the hook's channel, and
a model that sends its own only narrows its search (`null` is none, and a value `pre_tool` cannot
merge into, or arguments that are not an object, refuse the call: a deny that could not be applied
never lets the search run). The patterns are regular expressions that `glob_regex` makes from the
rule's glob (`fnmatch`'s grammar, written once, so the check and the tool agree; `[^b]` is a set of
`^` and `b`, an unclosed `[` is itself; the directories before the first wildcard are normalised,
`/tmp/../etc/*` is `/etc/*`); an invalid one (`[z-a]`) is an option error naming the rule. A
directory that matches (with a `/` added) is not entered, so no error names what is inside. An
`ask` rule also fires when the root could hold the pattern (the root and the glob's literal start
are prefixes of one another), as before 8E-6. The answer ends `[N path(s) left out of this
search: ...]`, a count with no names: silence would read as "no match" and mislead the model. So
`deny: ["grep:/etc/*"]` refuses `path=/etc/ssh` and runs `path=/etc` and `path=/` without `/etc`'s
files. A `read` deny with a glob guards `grep` this way too (`grep` reads; `find` only names).
Refusing a broad search whole was E4's first answer; John chose filtering (8E-6).
- **Commands (`tau._shell`, removed 2026-11-11).** As built, a glob over a command, with an
  `allow` that never vouched for a chain and a `deny`/`ask` tried on each piece of one after
  stripping wrappers (`sudo`, `env`, `xargs`, ...). Best effort, and so no guard: quoting, `eval`,
  `bash -c` and expansion step around it. **Ruling (John, D08 #3, 2026-11-11):** advertise only
  what holds, walls or open paths, not walls with holes. A `bash` rule names the tool: `deny` and
  `ask` take `bash` alone, `allow` takes `bash` or `bash:COMMAND`, one exact command (`always` saves
  that form, `*` `?` `[` written `[*]` `[?]` `[[]`, so saved rules keep working). A `bash` rule
  with a wildcard, or with a command in `deny` or `ask`, is an option error naming the rule. Finer
  rules over commands are left to an extension of one's own (its `pre_tool` sees the command).
- **The question** is the call, then `(ask rule: RULE)`. The call is `bash: COMMAND`, a line
  `TOOL: PATH` for each resolved path of a path tool, or `TOOL: ARGS` (the raw arguments) for any
  other tool. When a path was written differently, the rule line ends `; as written: PATH)`. The
  TUI shows a multi-line text, and the ratatui example draws each line and puts the answer field
  after them. Choices are `yes | always | no`, in that order.
- **`--answer`** (D22 §5). For a `permissions` question with an explicit TEXT the glob is over the
  call alone: the `(ask rule: ...)` line is not part of it, so no trailing `*` is needed. The TEXT
  may be written as the question shows it (`bash: make test`) or as a rule (`bash:make test`). A
  `bash` TEXT is as an `allow` rule is (2026-11-11): `bash` alone answers every command, otherwise
  one exact command; one with a wildcard is a usage error. Any other
  tool's call is matched whole by the glob, since its JSON arguments are no shell command. A path
  tool's TEXT must match every `TOOL: PATH` line, and those hold the resolved paths, so
  `permissions:read: /proj/*=yes` does not answer `/proj/../etc/shadow`. A TEXT left out
  (`permissions=yes`) answers every question.
- **`always`** appends the exact rule for the call to the user's file's own `allow`, when the file
  lacks it: for `bash` `bash:` and the command, for a path tool a rule for each resolved path, in
  both with `*`, `?` and `[` escaped as `[*]`, `[?]`, `[[]` (so `ls *.py` is allowed as that
  command, and `ls x.py` still asks); for a tool without a pattern the bare tool name. A `bash` or
  path-tool call whose arguments are malformed (no `command`, no string `path`) has no exact rule,
  and the bare name would allow every call: `always` then runs the call once, saves nothing, and
  `tell`s the clients so. The question still offers `always` (the choices do not change per call). It also
  keeps the rule in memory for the run, so a Config node or a higher layer that sets `allow` and
  hides the file's list cannot make the next identical call ask again, and it `tell`s every client
  where the rule went, and that a layer hides it when `options().allow` does not show it
  (`NodeHandle.tell`). It does not touch the project's layer.
- **A refusal** from `no`, a decline, an abort or a go-to is `the user refused this call`. A `deny`
  rule says `permissions refused this call: it matches the deny rule 'RULE'`.
- **`/config permissions ask=bash,write`** splits a list at commas; a rule that contains a comma
  is typed as a JSON array (`allow=["bash:cp a,b c"]`), as for any list option.
- **Attached `tau -p`** answers (by `--answer`, else declines) the questions that open after it
  attached on its own cursor and on the cursors its own run opened, an agent's included. Nothing records who opened a cursor, so a
  cursor is the run's if an extension wrote a user message on its walk after the attach, while the
  run's own cursor was busy (a sub-agent's task; it opens on the session's prefix; a follow-up to an
  agent the session already had open, which is in the attach snapshot, counts too; 8E-5), or its walk
  passes through the run's own turn. A cursor another client opens at an existing node is that
  client's; a sub-agent that another client's step starts during the run's turn would look like the
  run's (known limit). The run's own
  turn is from its first `User` node that is not another client's to the next one that is, and the
  answer it prints is the last reply without calls in that turn. A declined question is told as
  `declined question N from EXT on cursor C: "..."`. Clients and
  cursors are not separate users: one user hops between sub-agents and parallel branches, and
  every client may answer for any cursor, so there is no `opened_by` and no owner of a question
  beyond the first answer.
- **Known limits, not fixed.** (1) `always` holds for the run and in the user's file but is not
  written as a `Config` node on the branch: a tool hook has no cursor to write one with.
  (2) It is a guard against the model, not a sandbox: a later extension's `pre_tool` can change the
  outcome, and a `bash` deny is best effort.

### Driver: a tool that was not offered is not run

A sample's reply remembers the names it was offered (`pre_sample`'s `subject.tools`); a call to a
registered tool outside them is `Err("tool not offered: X")`, before any hook (so no permission
question for it). This is what makes the `agents` `tools` option bind and makes "no recursion, no
depth counter" true: an agent is offered `agent` only if its `tools` option names it, and the
option refuses that. A reply this driver did not sample (a session loaded with a batch pending, or
a cursor gone back to an old reply) is not checked, since what it was offered is not recorded.

### Agents (`tau.Agents`, `_agents.py`)

As §8. The cursor opens at the last node of the prefix; if the model it should run on (the `model`
option, else the calling branch's own, read at the batch's node) is not the one the prefix names, a
`Config` naming it comes first. The label is the first `agent:N` no cursor of the session has. The
tool's result is `agent:N answered:` and a newline and the reply's text. A call fails with a
`ToolError` that names the agent: `agent:N failed: ...`, `was halted: ...`, `was aborted`, a reply
that asks for tools (a step limit turned auto-step off: `stopped without a final reply (a step
limit?)`), or no text (`gave an empty reply`). A failed agent is closed like any other, and `to` can
reopen it. `to` must name an `agent:` label: anything else (`to=""` included) is `there is no agent
'X'`. A closed cursor is reopened; an open one at rest (a crash left it; `tau serve --stop` aborts and closes agents first, 8E-5) is continued as it is; one that is busy is `agent:N
is still working`. A name in `tools` that no loaded extension provides is a `ConfigError` from `/config` and
`tau config set`, and a warning at the start and in `tau config check` when a file holds it
(`Options.missing_tools`, 8E-6, as permissions'). A list option
typed on a command line (`commandline.value`, and the ratatui form) is split at commas and each
item trimmed. The agent cursor's `tools` are kept in the extension's memory
for the length of the call, read from the calling branch's options (so `/config agents tools=...`
on the parent applies). `tools` containing `agent` is an option error. Not built: `opened_by`.

### M10

`OFF_BY_DEFAULT` in `_extensions.py` names the built-ins that `load` must turn on. Its row is `off`
with the detail `off (default); to turn it on: tau ext enable agents`; `load: {agents: true}`,
`tau ext enable agents` and `-e agents` turn it on; `--no-extensions` turns on nothing. A drop-in
or package with the key `agents` is a different source and not off by default. Hook order with
`agents` on is `agents`, `attachments`, `base_tools`, `compaction`, `permissions`, `prune`,
`skills`, `step_limit`, `usage`, as §1 says.

### 8E-5 (hands-on fixes to section 6)

- **Reload before a command.** `Skills.rescan()` stats `prompts/` and `skills/` (user and
  project) and reads again what changed; the driver asks every extension that has a `rescan`
  before it lists, parses or runs a command, and rebuilds the commands if one says they changed.
  A project file that is not what trust approved is not used: each project name is a command that
  fails with the report `tau trust` would give. A file that cannot be used is a command that fails
  with the reason. Nothing else stops. The skills list keeps D24's rule (written once), so a new
  skill is a command at once and reaches the model's list in new sessions only.
- **Arguments are a command's own refusal.** A command that runs as a step of a cursor may raise
  `CommandFailed` itself: the client that ran it is told once, the message as written, and it is
  not a hook failure (the step ends without moving the cursor, as an abort does).
- **Standard input.** A command with a parameter named `stdin` gets `tau -p`'s standard input in
  it; the command line never fills it. A template sends its text, a blank line, then `stdin`.
- **Files in the wrong place are errors**, and `tau trust` lists every error of a project's files
  and refuses it. The files are pinned like the rest, misplaced ones included.
- An unknown `/ext:name` is an error (`-p` exits 1), not text. A UTF-8 byte order mark is dropped.
  A skill sent by `/NAME` ends its tag with the skills list's line, naming the directory: "A
  relative path in a skill starts from the directory of its file: /abs/NAME." Nothing in the
  body is attached (§5: only a person's typing is), so the model reads the files itself.


### 8E-6 (config hardening; D13 "Built (8E-6)")

A permissions rule or an `agents.tools` name that nothing provides is refused when set (`/config`,
`tau config set`) and only warned about when a file already holds it, at the start and in `tau
config check`: a deny naming an unloaded tool was fatal. `tau config check --clean` removes such
entries. Retry has no switch of its own: `--set load.retry=false` or `TAU_SETTINGS` turns the
provider wrapper off for one run, like any other `load` key.

### 8E-6 (compaction on reported counts, D26 T1)

- **One unit.** The cut, the checks, the notice and the limits use `Cursor.count`, not characters
  over four: a line that passes is under its limit by the measure the next bill uses, so the end of
  the next turn does not compact again (the loop when real tokens ran above the characters), and a
  summary is not paid for when the line could not come out under the limit (§2 *Counts*,
  *Algorithm*).
- **`auto` gates both limits.** `false` switches off the soft limit's compaction as well as the
  hard one's. The guide described it backwards.
- **A summary that does not fit the soft limit is told once** and not asked for again at the soft
  limit until the limits change; the hard limit and `/compact` always try. A summary no shorter than
  what it replaces is not written.
- **Stopping the summarizer fails the step.** Aborting or closing `compaction:N` while the hard
  limit waits for it fails the step with the reason; it no longer sends the sample that did not fit.
  The hard limit never sends what it cannot show fits (D26 §5): when compaction cannot make room,
  or has nothing to compact (a first message too big for the window), the step fails with the
  figures, even if only the ceiling of what is not sent yet is over.
- **`/compact` with no `keep` set** keeps at most half of the line, so it summarizes something on a
  model with no window or a short conversation. A head of nothing but an earlier summary is `nothing
  to compact yet`. A turn in progress keeps the user message it began with, verbatim, after the
  summary.
- **A `reserve` under 2 tokens** (a window of 1 to 7 with `reserve` unset) leaves a summary no
  allowance and is a `ConfigError`; the `IndexError` of a window of 1 to 4 is gone with the code
  that raised it.
- **Presentation.** The cursor's line says `local:qwen has no context_window` (the TUI adds the
  extension's name; it had read `compaction: compaction:`); the TUI's title is the first message the
  person wrote, taken from the walk the summary was made from when the line begins with a summary.
