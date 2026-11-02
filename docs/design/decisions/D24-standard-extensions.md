# D24: Standard extensions

Status: proposed 2026-11-02. Phase 8, stage 8E, "Standard extensions" (`docs/plan.md`). Amends D3
(the summary's sample; the cold summary), D13 (`ModelConfig` gains `context_window` and `price`),
D15 (D3: retries ship in `tau`; normalized usage gains `written`; `cached` can be absent), D19
(a built-in may be off by default; built-ins are built with the loaded config; §5:
`--no-extensions` turns off these built-ins too, which D19's Alternatives rejects for every
built-in but `base_tools`: "built-ins are τ's behaviour, switched in `load`") and
model.md §7–§8 (an abort ends its hooks' `rest` waits, M7).

## Context

The plan ships eight extensions built in: compaction, permissions, sub-agent, prune, retry,
skills and prompt templates, `@file` attachments, and usage. 0.15 had most of these as features,
so their absence is a regression (`docs/from-0.15.md` §2.4–2.10). Permissions and sub-agents were
examples only in 0.15, and prune had no 0.15 counterpart.

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
  reads its template and skill files, §6). Retry (§4) is not an extension and has no key.
  `--no-extensions` turns them off too, joining 8A's `_BARE` set with `base_tools`, so a bare run
  is bare. `step_limit` stays on, because it is a guard.
- **The prefix of a walk** is its root `Config` and the `System` nodes before the first other
  message. Compaction keeps it, and a sub-agent starts from it. It holds the system prompt, the
  context files and the skills list, so it is also the prompt-cache prefix every cursor shares.
- Hook order is load order (sorted keys): `agents`, `attachments`, `base_tools`, `compaction`,
  `permissions`, `skills`, `step_limit`, `usage`. No extension here depends on running before
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
| `auto` | `true` | compact before a sample that would not fit |
| `reserve` | 16384 | tokens kept free for the reply and for the summary itself |
| `keep` | 20000 | tokens of the newest nodes copied verbatim |
| `model` | none | the model that writes the summary; none means the branch's own |

**Trigger.** A `pre_sample` hook. If `auto` is on, the model has a `context_window`, and the
estimate of `walk(at)` is above `context_window − reserve`, it compacts, then the sample runs on
the new line. One trigger: 0.15's second, softer limit (80% at the end of a turn) is dropped.
A model with no `context_window` gets no automatic compaction, and the cursor shows
`compaction: local:qwen has no context_window` so the user knows why. `/compact` still works.

**Estimate.** The newest reply on the walk that has `meta.usage` and is not a copy gives an exact
count: its `input + output`. Each message node after it adds its characters divided by 4 (text,
thinking, call arguments, outcomes). With no such reply, the whole walk is counted by characters.
This is an estimate by design, never treated as exact. 0.15's calibrated per-class counter is
dropped.

**Algorithm** (D3's shape, now specified):

1. **Cut.** Walk back from `at`, adding each node's estimate, until `keep` is reached. The tail
   starts at the nearest `User` or `Assistant` node at or before that point; a `Results` node
   always stays with its batch. The head is everything between the prefix and the tail. An
   empty head means `nothing to compact yet`; the sample then runs as it is, and may fail.
2. **Summary.** The extension opens a **summarizer cursor** at `at`, labelled `compaction:N`
   (with a `Config` naming `model` under `at` first, if `model` is set), `send`s it the summary
   prompt and the focus text, and `await rest`s. The summary is the text of its reply. The
   summarizer is offered the same tools as the branch, so its request is the branch's request
   plus one message, which keeps the prompt cache warm. A call it makes anyway is refused by
   compaction's own `pre_tool` (`summarizing: no tools`). An earlier summary is just part of the
   head, and the prompt says to carry it forward, so 0.15's separate update prompt is dropped.
   Afterwards the summarizer cursor is closed. Its reply stays in the tree, so `usage` counts it.
3. **New line.** Under the prefix's last node: copies of the head's `Config` nodes, so the model
   and options at the cut stay the same; then the summary as a `User` node by
   `Extension("compaction")` with `derived_from` = the last head node; then a copy of every tail
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
(Rust); (c) a handle read of the resolved model (`model_config`); (d) abort ends a hook's `rest`.

**Dropped from 0.15:** the soft limit, the split-turn prefix summary, the read and modified file
lists appended to the summary, `/autocompact`, the calibrated counter, the summarizer
window check, and `compaction.summarizer` (now `extensions.compaction.model`).

### 3. Usage

**What the user sees.** After each reply, the cursor's status line:
`12.3k in · 96% cached · 812 out · $0.014 · ctx 41% · $1.27 session`. The dollar figures appear
only for a model with a `price`, and `ctx` only for a model with a `context_window`: a missing
price shows no cost, never `$0`. `/usage` prints one row per model for the whole session (samples,
input, cached share, cache writes, output, cost) and a total. Summaries and sub-agents are
included, since their replies are in the tree; copies are not counted twice.

**Cache notice.** After a reply that reports `cached: 0` with at least 4,096 input tokens, when the
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
does not report it; (d) `model_config(name)` and `model(at=node)` on a handle; (e) `copied_from`,
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
  provider in it, so `tau`, `tau -p` and `tau serve` all retry. A program that builds its own
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
  cancels, since the wait is part of the sample. A `Retrying` delta does not count as output for
  the "nothing yielded yet" rule.
- **No key and no options** (Open 5).

**Needs:** the `Retrying` delta (D15's delta list).
**Dropped from 0.15:** jitter, and the code-only `options["retry"]`.

### 5. `@file` attachments

**What the user sees and types.** `explain @src/main.rs` sends the line with the file's text
attached after it:
`<attachment path="src/main.rs">…</attachment>`. A file over the limit, not UTF-8, or
unreadable becomes `<reference path="/abs/src/big.log" size="2.1 MB" reason="over 10 kB"/>`, so
the model can `read` the part it needs. A token is `@` followed by non-spaces, at the start of the
line or after whitespace, with trailing punctuation retried stripped. `~` expands, and a relative
path resolves against the driver's working directory, as `complete` does. A word that names no
regular file, or names a directory, stays as text. A file named twice is attached once.

**Key and options.** `attachments`, on by default. One option, `limit`, in bytes (default 10240).
**Hook:** `pre_input` rewrites the draft's text. The `User` node therefore holds exactly what
the model saw (I1). Every input is expanded, a template's or a sub-agent's task included.
**Completion:** the TUI calls `Client.complete(cursor, "path", prefix)` for an `@` word; that
already works over the wire.

**Needs:** nothing.
**Dropped from 0.15:** images (deferred, D15), the attachment bar and the transcript fold (D23's),
the `tau -p "@file"` raw-inline rule, and the no-match popup line.

### 6. Skills and prompt templates

**What the user sees and types.** Each template `NAME.md` and each skill `NAME/SKILL.md` is a
command, `/NAME args`, or `/skills:NAME args` when another extension also has `/NAME`. The
command's help is the file's `description`, which defaults to the first body line for a
template; a skill requires one. Running it sends the expanded text to the cursor as input
(`send`, authored `Extension("skills")`), so the `pre_input` gates and `attachments` see it, and
the cursor samples.

- **Template expansion:** `$ARGUMENTS` is the whole argument text and `$1`…`$9` are its shell
  words. A missing `$N` fails the command. A body with no placeholder gets the arguments appended
  after a blank line. 0.15's `${N:-default}`, `${@:N:L}` and `$@` are dropped.
- **A skill** sends `<skill name="NAME" path="/abs/…/SKILL.md">body</skill>` followed by the
  arguments.
- **Skills for the model:** when a session's first step begins (its walk has only the prefix) and
  a `read` tool is offered, `pre_step` writes one `System` node listing each skill's name,
  description and path, and saying "read the file to use it". Skills marked
  `disable-model-invocation: true` are left out. The list is then part of the prefix, which keeps
  the cache stable. A skill added later reaches new sessions only, as with `AGENTS.md` (D20 §1).

**Files.** `$TAU_HOME/prompts/*.md`, `$TAU_HOME/skills/*/SKILL.md`, and the same under the
**trusted** project's `.tau/` (Open 7). They are read once, when extensions load. For one name the
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

**Key and options.** `permissions`, on by default, asking nothing until configured (Open 3).

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

**Key and options.** `agents`, **off by default** (Open 4): the tool would be in every request,
and 0.15 had it only as an example. Options: `model` (none: the branch's own) and `tools`
(default `[]`, meaning all but `agent`).

**Needs:** a built-in that is off by default, which D19 deferred to "Phase 8's first". It would be
`load: {agents: true}`, or `tau ext enable agents`, and `tau ext list` would show
`off (default)`. Optional: `opened_by` (not spiked) would let a frontend draw who opened whom;
labels serve for 0.16.0.
**Dropped from 0.15's `delegate` example:** child processes, chain mode, `tasks[]` (several calls
do the same), `max_usd` and `max_seconds`, and forcing parallel children read-only.

### 9. Prune: cut from 0.16.0

Prune has no 0.15 counterpart and no definition in the docs (`from-0.15.md` §2.4). Its likely
meaning is to replace old tool outputs in the context with stubs. That is compaction without a
model: the same new line, `Results` copied with outcomes shortened, `derived_from` instead of
`copied_from`. The base tools already cap output at 10 kB with an overflow file, which removes
most of the need. If someone asks, it becomes a compaction option (`prune_after` turns) rather
than an extension of its own.

### 10. What is missing today

| # | piece | where | for |
|---|---|---|---|
| M1 | `copied_from`, `derived_from` built: node fields, the log record (missing fields default, so old logs load), `append` and `write` take them, the codecs, the wire schema. No store `VERSION` or `PROTOCOL` bump: both mark released formats and 0.16.0 is the first release of each (D12, D21 §8) | Rust core, store, bindings, `_protocol` | compaction; usage skips copies |
| M2 | `ModelConfig.context_window` (optional positive int) | `_config.py`, D13 | compaction, usage `ctx` |
| M3 | `ModelConfig.price` `{input, output, cache_read, cache_write}`, USD per million, all four required | `_config.py`, D13 | usage |
| M4 | `handle.model_config(name=None) -> ModelConfig` (the resolved model, as at a sample) | `_handles.py` | compaction, usage |
| M5 | `cursor.model(at=node)`: the model at any node, the fold the driver already has | `_handles.py` | usage |
| M6 | normalized `usage.written` (cache writes; Anthropic's `cache_creation_input_tokens`), and `cached` absent when the server reports none (today `0` in `openai.py` and `anthropic.py`, which a cache notice cannot tell from a miss; Gemini omits zero counts, so there an absent count stays `0`) | providers, D15 §11 | usage |
| M7 | abort of a cursor ends its hooks' `rest` waits (returns `None`), as it declines their questions | driver, `_handles.py` | compaction's Esc |
| M8 | the live delta `Retrying`; `RetryProvider` public; `start_session` wraps | providers, D15 | retry |
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
5. **compaction** (M1, M7): the largest, and the only Rust.
6. **permissions**, once D22's `--answer` exists.
7. **agents** (M10).

**Cuts.** Prune is cut now (§9). If 8E runs long, agents goes next: 0.15 had only an example, and
gamemaster and forest already show the pattern. The other six cover regressions 0.15 users would
notice.

## Alternatives

| option | why not |
|---|---|
| The summary from `handle.sample(walk)`, a one-off call outside any step | A new handle API (model.md §7 lists it as not built). A summarizer cursor needs none, is visible to every client, and is counted by usage. |
| Copies recorded in a `Custom` map node instead of `copied_from` | Every reader (usage, the TUI, export, the estimate) would rebuild the map. D3 already accepted the fields. |
| Retry as a `post_step` extension | It sees the failure after the reply is written, and its wait cannot be aborted (hooks are never cancelled). |
| A `retry` key on `ModelConfig` | No one has asked for a different policy. Add it when someone does. |
| Cost stored in `meta` at sample time | Stored data that is derived. A price fixed in the config re-prices the whole session, which is what a user editing a price wants. |
| Prompt templates as one `/prompt NAME` command | Typing `/prompt review` instead of `/review` is worse than 0.15. Generated commands cost nothing. |
| `@file` expanded by the frontend | Each frontend would do it again, and a remote frontend would read its own disk, not the session's. |
| Permissions as a mode (`ask`/`allow`/`auto`) | Rules cover a mode (`ask: [bash, write, edit]`) and also say which commands. |
| Sub-agents with the parent's context (open at `at`) | It costs the whole context on every call, and the model writes better tasks for a fresh one. Open at `at` with your own extension if you want it. |

## Open questions

1. **The summary's sample: a cursor or `handle.sample`?** Recommendation: a cursor (§2).
   No new API, and it is visible and counted.
2. **`copied_from` in core (M1), or no provenance?** Recommendation: core, as D3 accepted.
   Without it, usage counts every compaction twice, and the estimate anchors on a copy's stale
   `usage`.
3. **Permissions' default: ask nothing, or ask for `bash`, `write` and `edit`?**
   Recommendation: ask nothing. That is what 0.15 users and their `-p` scripts had. Turning it
   on is one `/config ... --save`, which the TUI's first-run text can mention.
4. **Agents: off by default, and in 0.16.0 at all?** Recommendation: off by default, built
   last, and the first cut if 8E runs long.
5. **A config key for retry?** Recommendation: none until someone asks (§4).
6. **Price: units and missing fields.** Recommendation: USD per million tokens, matching 0.15's
   `cost` block renamed to `price`, with all four fields required. A model that writes no cache
   repeats its input price for `cache_write`. A missing field is a `ConfigError`, not a silent 0.
7. **Project prompts and skills: pinned by trust?** Recommendation: read only from a trusted
   project, not pinned. They are text, not code, with the same exposure as an `AGENTS.md` a
   `git pull` changes, which is not pinned either. The other answer is to pin `.tau/skills/`
   beside `.tau/SYSTEM.md` (D20 Open 2), since the skills list reaches every request.
8. **Compact when a provider says the prompt is too long?** Recommendation: no. The trigger is
   proactive only. A 400 is a visible failed sample, and `/compact` then a step recovers. Every
   provider words that error differently.
9. **Where `context_window` comes from.** Recommendation: the config only, for 0.16.0. Asking
   the server (llama.cpp's `/props`) or a models.dev catalog comes later (plan, "After 0.16.0").

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
  `cached` is absent when not reported.
- **`model_config`** (handle): the resolved `ModelConfig` of a qualified name; and
  `model(at=)`.
- **`Usage`** (`usage`, option `cache_notice`); **cache notice**.
- **`RetryProvider`** (moved from the example) and **`Retrying`** (live delta).
- **`Attachments`** (`attachments`, option `limit`); **attachment** and **reference** (the two
  tags).
- **`Skills`** (`skills`, option `advertise`); **template**, **skill**, **skills list**.
- **`Permissions`** (`permissions`, options `ask`, `allow`, `deny`); **rule**.
- **`Agents`** (`agents`, options `model`, `tools`); the **`agent`** tool; **sub-agent**
  (label `agent:N`).
- **off by default** (a built-in that `load` must turn on).
