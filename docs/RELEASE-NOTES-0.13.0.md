# v0.13.0 — a script can call the tools, and `off` finally means off

*"There was no choice but to be pioneers."*

About thirty merges since 0.12.0 (`bebdedf..a255205`). Code mode and the
reasoning work are the two big ones. Most of the rest is one theme: a fact that
τ used to drop, guess or bury in a string now reaches the caller as a field.
This is a minor bump, and it breaks things. The serve protocol moves twice, and
several defaults change under existing configs. Read the breaking section first.

---

## BREAKING: serve protocol 0.8 → 0.10, RPC protocol 2.1 → 2.2

The serve hello refuses any version but its own, so **a 0.8 or 0.9 client
(tau-code included) cannot talk to a 0.13.0 daemon until it moves to 0.10.**
RPC compares MAJOR only, so a 2.1 host still connects. It breaks only where it
reads the renamed key below.

| | Was (0.12.0) | Is (0.13.0) |
|---|---|---|
| Serve protocol | `0.8` | `0.10` (`docs/SERVE-PROTOCOL.md`) |
| RPC protocol | `2.1` | `2.2` (`rpc/capabilities.py`, `PROTOCOL_VERSION`) |
| `HelloResult.models` | absent | **required** list of `ModelRecord` (`docs/MODEL-PICKER.md` §7) |
| `compaction_settings.enabled` (`get_session_stats`, `CompactionSettingsRecord`) | `enabled` | `auto` (`docs/PRICING-AND-COMPACTION-CONFIG.md` §6) |
| `CommandInfo.origin` / `get_commands` origin | `builtin`, `extension` | adds `template`, `skill` (`docs/PROMPT-TEMPLATES-AND-SKILLS.md` §8) |

Serve 0.9 came with code mode and 0.10 with the model picker. Neither went out
in a release, so 0.8 → 0.10 is the step a client takes. The full list of
additive wire changes is at the end.

## BREAKING: compaction is on by default, and `enabled` is now `auto`

`TauBackend` built every session with `CompactionSettings(enabled=False)`. That
line was in the initial commit. So the TUI, `tau -p`, `--mode rpc`,
`--mode repl` and `tau serve` never compacted unless asked. A small model reached
131k tokens in a CLI session and failed with the provider's prompt-too-long 400
(`docs/PRICING-AND-COMPACTION-CONFIG.md` §1.2).

| Was | Is |
|---|---|
| Off in every CLI head, RPC sessions included | On in every CLI head unless config says `"auto": false` |
| No config key | A `compaction` block: `auto`, `reserve_tokens`, `keep_recent_tokens`, `summarizer` (§3) |
| `CompactionSettings.enabled` | `CompactionSettings.auto`. No alias. |
| A summarizer was a `Model` plus a key string | `local_summarizer(summarizer=<name>)`, resolved through `resolve_model` (§4.1) |

- An absent block means on, with `reserve_tokens=16384` and
  `keep_recent_tokens=20000`. A bad key, type or value raises `ConfigError`
  naming the key.
- A model entry may carry its own `compaction` block. It **replaces** the
  top-level block whole and does not merge (§5).
- `auto` gates automatic compaction only. `/compact` never read the old flag, and
  it still does not.
- A model switch re-reads the new model's block, so an earlier
  `set_auto_compaction` does not survive the switch (§6).
- The summarizer's `context_window` must be at least the session model's.
  Otherwise construction, `set_model` and `set_model_resolver` raise
  `CompactionPolicyError`.
- RPC hosts: a session now compacts when it nears its window. The
  auto-compaction `agent_start`/`agent_end` pair still carries no submission id
  (§7). Before, a host met this only after opting in.

## BREAKING: the default output cap is 32768, not 4096

A model entry or SDK id that sets no `max_tokens` now gets `32 * 1024`. 4096
stopped ordinary agent completions at the cap. The three built-in OpenAI entries
keep their own 4096. A config that sets `max_tokens` is unaffected (commit
`2020898`; `docs/TRUNCATED-TOOL-CALLS.md` §7).

## BREAKING: a completion cut off at the cap runs no tool call, and the turn ends

On `stop_reason == "length"`, every tool call in the completion stays in the
message with the partial arguments the stream delivered. Each one gets an error
result naming the cap and the cut argument, and none runs. The turn ends with no
second request (`docs/TRUNCATED-TOOL-CALLS.md` §3.2).

The defect was measured. On `claude-haiku-5-5` at 4096, a `write` reached the
tool with `path` and no `content` three times in a row. Then
`repeat_tool_call_limit` ended the turn, and 12288 output tokens produced
nothing. The OpenAI path leaked another way: JSON repair could close a cut
string, so a truncated `write` ran.

- Calls that finished before the cut are refused too, and told that their
  arguments were complete.
- `dropped_tool_calls` and `dropped_partial_tool_calls` keep their names, but now
  count calls *not run*, kept calls included. The old meaning is a subset.
- **τ now sets `eager_input_streaming: true` on every Anthropic tool.** Without
  it, the API sent 19 characters of a cut-off call that billed 1500 output
  tokens. With it, the API sent 4042. This changes the tools block once, so each
  cached Anthropic prefix misses once after the upgrade.

## BREAKING: reasoning levels are translated per wire, and `off` means off

Full record: `docs/REASONING-WIRE.md`. Measured against raw HTTP in
`docs/probe-results/reasoning-wire-2026-10-17.md`.

| | Was | Is |
|---|---|---|
| Level names on the wire | τ's own names sent as-is. `minimal` was a 400 on Anthropic and `gpt-5.6-luna`, `xhigh` a 400 on Gemini. | Each provider translates. A level the wire lacks clamps upward (§2.1). `Model.thinking_level_map` overrides it per model. |
| Google levels | Every level sent `include_thoughts: true` only, so `minimal` and `high` were one request | `thinking_level` is sent |
| `--thinking off` | Became "send nothing", so the model thought anyway (§1.3) | Sends the wire's off form, or raises (§4) |
| `off` on an OpenAI-compatible model with `reasoning: false` | Sent nothing | **Raises**, naming `thinking_level_map.off` (§4) |
| `create_agent_session(thinking_level=…)` default | `"off"` | `None` (provider default) |
| Anthropic thinking display | Not set, so 5.5 models returned empty thinking text | `display: "summarized"` with every adaptive level. Free per Anthropic's docs (§8). |
| `Model.reasoning_replay` default | `"turn"` everywhere | `None`, the provider default: `"all"` on Anthropic and Google, `"turn"` on OpenAI-compatible (§6) |
| `reasoning_replay: "off"` on Anthropic | Accepted | Raises at config load and in the provider (§6) |
| OpenAI-compatible `content_filter` or unknown `finish_reason` | `"stop"` | `"error"`, naming the raw value (§9, #10) |

Upgrade notes:

- A llama.cpp entry that uses `--thinking off` needs
  `"thinking_level_map": {"off": {"chat_template_kwargs": {"enable_thinking": false}}}`,
  or `{"off": {}}` for a model that never reasons (§4).
- Anthropic now replays every signed thinking block, which the API reads and
  bills. Replaying a 77-token prior-turn block raised turn-2 `input_tokens` from
  80 to 159 (§1.4). An explicit `"turn"` is still honoured.
- Gemini's answer `thoughtSignature` is kept and replayed. With it, the model
  recalled its own earlier pick (+227 prompt tokens). Without it, the model
  guessed again and spent 635 thought tokens doing so (§1.5, §5).
- Compaction and branch summaries get the session's level unchanged (§4.1).

## BREAKING: provider failures are typed exceptions

| Was | Is |
|---|---|
| A non-2xx was `ErrorEvent(message="HTTP …")`, then a bare `Exception`/`RuntimeError` | `tau_llm.ProviderHttpError(RuntimeError)` with `status_code`, `message`, `body`, `model_id`, `base_url`, `retry_after`, `headers` |
| Iterating `provider.stream_chat()` directly yielded an `ErrorEvent` for a non-2xx | It **raises** (`docs/PROVIDER-HTTP-ERRORS.md` §2) |
| A refused connection was an in-band `ErrorEvent` | `ProviderConnectionError(RuntimeError)` (`docs/PROVIDER-RETRY.md` §3) |
| An SSE `error` frame after a 200 raised `RuntimeError`. Anthropic's became `ProviderHttpError(200)`. | `ProviderStreamError(RuntimeError)` with `code`, `status`, `body` (`docs/RESPONSE-HEADERS.md` §5.5) |
| Anthropic/Google message text: `Streaming error from model … BadRequestError: …` | `HTTP <status> from model '<id>' at '<base_url>': …`, the OpenAI form |

All three subclass `RuntimeError`, so an `except RuntimeError` around a turn
behaves as before. The OpenAI message text is byte-identical to 0.12.0, which
keeps JMFTS's `^HTTP (\d{3}) from model ` regex working (`PROVIDER-HTTP-ERRORS.md`
§4). `ErrorEvent.exception` carries the original, and `result()` re-raises it.

## BREAKING: an abort says who asked, and a user abort drops queued followUps

`AbortSignal.abort(reason="user" | "extension")` defaults to `"user"`.
`ctx.abort()` is `"extension"` (`docs/TURN-SETTINGS.md` §4).

- After an **extension** abort, queued followUps run on a fresh signal. Before,
  each one was written as a user message with no reply.
- After a **user** abort (Esc, Ctrl-C, RPC `abort`), queued followUps are dropped
  and announced by the new `follow_ups_dropped` event. The TUI shows a toast, the
  REPL a system line, `tau -p` a stderr line (§5).
- **An abort now cancels the pending provider read** (§4.1, #36 #37). tau-code
  measured an abort answered at +1.0 s while `message_end` waited for the
  server's next byte at +4.0 s, and `agent_end` said `"done"`. Each provider now
  races its read against `AbortSignal.wait()`. A turn cut short mid-stream ends
  `end_reason: "aborted"` in every exit. Not covered: entering the Anthropic
  SDK's `messages.stream()` context, which waits for response headers.

## BREAKING: four constants stop posing as facts

`docs/SILENT-DEFAULTS.md` (#8, #9):

| Was | Is |
|---|---|
| A model entry with no `model` was sent as `gpt-4` | `ValueError`: `models.<name> has no 'model'` |
| `ctx.is_idle` was always `True` | `True` only when no cursor is streaming. Raises when unbound. |
| `ctx.session_manager` was always `None` | Removed |
| `SessionInfo.tool_count` was always `0` | Removed (an old `tool_count=` is kept as an extra) |

Also from this family: **a model with an unregistered `backend` and no
`base_url` raises** where it used to get OpenAI's URL. This was measured: Groq's
key went to `api.openai.com` (`docs/EXTENSION-PROVIDERS.md` §2). In the SDK,
`resolve_model` now takes the registered provider's wire and endpoint. It
returns a copy instead of mutating the shared `gpt-4o` entry (`3c4d04a`).

`context_window`'s 128000 default is the same fault and is **not** changed. It
needs a decision (`SILENT-DEFAULTS.md` §5).

---

## Code mode, and a tools block that never changes on its own

Experimental. `tau -e examples/codemode.py` adds one `codemode` tool. The model
writes a Python script, and τ's tools run inside it as `tools.<name>(...)`. The
implementation is `tau_agent_core.codemode`. The research is `docs/CODE-MODE.md`,
and the build is `experiments/sandbox-providers/RESULTS.md`.

- **Two interpreters.** CPython is a stdlib-only subprocess. Monty needs
  `ffwf-tau-agent-core[codemode-monty]`. A prompt-only TypeScript provider shows
  the plug-in surface.
- **Three lifetimes:** `script`, `turn` and `session`. Each provider and lifetime
  pair renders its own system-prompt section, and config can override every part
  of it.
- **Limits:** a wall-clock limit, a memory limit, a tool-call count, and the
  number of output characters returned to the model.
- **Live smoke** on Haiku 4.5, five runs: valid CPython scripts on the first
  attempt in 3/3 runs. Monty needed its subset's sharp edges named in the prompt
  (§8). One run each is an anecdote, not a rate.
- **Read this before relying on it:** a script's tool calls go through
  `DirectToolInvoker`, which **bypasses the hook pipeline**. No `tool_call` veto
  runs, no `tool_result` rewrite applies, and no `tool_execution_*` events fire.
  A permission gate written as a hook does not see script calls
  (`codemode/invoker.py`). The CPython process runs as the user, with filesystem
  and network reachable (§10).

The seams under it shipped with it (`experiments/tool-exposure/RESULTS.md`):

- **Nested tool calls.** `ctx.execute_tool(name, args)` runs the loop's own
  validate, hook and execute pipeline, so hooks cover tool-to-tool calls. Events
  carry `parent_tool_call_id`.
- **Exposure.** Each tool is `direct`, `model-only`, `callable`, `deferred` or
  `hidden`, recorded in the tree as config entries.
- **The cache-safe tools block** (§4). Masking a tool leaves it declared. The loop
  refuses the call and tells the model by an appended message. Loading a deferred
  tool is a `tool_search` result plus `call_tool`. Only an explicit
  `redeclare_tools` changes the block. Measured on `claude-haiku-4-5`: each block
  change re-billed about 10.8k tokens as a cache write, 12.5× the hit it replaced.
- **Typed returns.** `output_schema` comes from the annotation, and `bash` and
  `grep` have typed results. `bash` now runs with `stdin=/dev/null`.
- **#20 fixes** (§8). `preview_redeclare` refuses what `redeclare_tools` refuses.
  Neither writes an entry when nothing changes. `find_extension` raises on a
  duplicate name.

## Backgrounding is a property conferred on a tool

A built-in extension, `extensions_builtin/background.py`.
`"extensions": {"background": {"tools": ["bash", "grep"]}}` lets the model run
those tools in the background, call by call. The result returns to the cursor
that issued it. A `bg` tool reports, waits for or cancels a job
(`docs/BACKGROUND-TOOLS.md`). The registry is loop-confined, and it enforces
that. Measured on 3.14t with the GIL off, the unguarded counter minted about
330,000 duplicate handles (§8).

## Settings are read per request

`AgentLoop` reads model, key, reasoning and temperature before every request, not
once per loop. A `turn_end` hook calling `ctx.set_model("cheap")` now reaches
the loop's next request (was `['m','m']`, now `['m','cheap']`).
`ctx.set_reasoning` / `AgentSession.set_reasoning` are new. A change is recorded
in the tree ahead of the request it governs, so config entries can now sit
mid-loop. `ctx.set_model` is recorded under the config name `--continue` reads
(`docs/TURN-SETTINGS.md` §1–3, §6). `/thinking <level>` in the TUI and REPL, and
RPC `set_reasoning`, call the same setter (`REASONING-WIRE.md` §10).

## Provider retry and backoff

`stream_simple` re-sends a request that failed before yielding anything,
byte-identical. That covers 408, 429, 502, 503, 504 and 529, plus a refused or
reset connection. It makes up to 3 retries at about 1, 2 and 4 s, honours
`Retry-After` up to 60 s, and stops on abort (`docs/PROVIDER-RETRY.md`).

- A failure after any output reached the caller is never retried, because that
  would duplicate output. 500, 524 and read timeouts are not retried either (§3).
- **The default is on.** A caller with its own retry loop passes
  `options["retry"]=False`, or the layers multiply. JMFTS needs this change and
  has not made it (§6).
- The Anthropic SDK used to retry twice, silently. It now runs with
  `max_retries=0` (§4).
- Each retry is a `provider_retry` event. The TUI shows a toast, the compare
  screen shows `retrying in Ns`, the REPL a line, `tau -p` a stderr line (§5).

## Response headers and structured errors reach every head

Every provider returns HTTP response headers: `DoneEvent.response_headers` on
success and `ProviderHttpError.headers` on refusal. `None` means not captured,
and `{}` means none sent. The agent loop passes them on as
`message_end.response_headers` and as `error_detail` (`type`, `status`,
`retry_after`, `headers`, `retryable`) on `agent_end` and `provider_retry`.
Extensions, RPC, serve and `--mode json` all get them. `set-cookie` is stripped
at the process boundary. Headers never reach the session log
(`docs/RESPONSE-HEADERS.md` §2–5). τ parses only `Retry-After`. Rate-limit
counters are the consumer's to read.

## Reasoning tokens are counted

`Usage.reasoning_tokens` is the part of `output_tokens` that was reasoning. It
comes from each wire's own field, and `None` means the server gave no split.
`output_tokens` stays the billed total, so pricing is unchanged. The truncation
notice quotes the split (`REASONING-WIRE.md` §7). `message_end` carries
`reasoning_level`, the level actually sent after clamping (§3).

## Cache writes are priced

Three copies of the cost formula left out `cache_write`. Both providers report
written tokens outside `input_tokens`, so a cold 14116-token prompt was priced
as 3 tokens: $0.000009 reported for $0.0529 spent
(`docs/PRICING-AND-COMPACTION-CONFIG.md` §1.1).
`tau_agent_core.usage.price_usage` is now the only formula. It raises on the wire
spelling `prompt_tokens`, rather than pricing it as zero (§2).
`compute_cost_usd` is deleted. RPC still carries no cost (§7).

## Prompt templates and skills

- `/name args` expands `~/.tau/prompts/name.md`, `<cwd>/.tau/prompts/name.md`,
  or an explicit `--prompt-template` path. The format is Claude Code's custom
  commands, so `--prompt-template ~/.claude/commands` loads unchanged
  (`docs/PROMPT-TEMPLATES-AND-SKILLS.md` §4).
- `~/.tau/skills/<name>/SKILL.md` is listed in the system prompt when `read` is
  offered, and runs as `/skill:<name>` (§5).
- Expansion happens in `submit()`, so the stored node, the bubble and the
  model's input are one string in every head (§6).
- **First wins, explicit > user > project.** A cloned repo cannot replace your
  `/deploy` (§2.2). Project *templates* load with no trust gate. Project
  *skills* are held back until there is one (§3).
- `$ARGUMENTS` is verbatim. A bare `$N` past the words typed refuses rather than
  sending an empty string (§4.2).
- Fixed on the way: **`tau -p` persisted the typed prompt**, not the one the turn
  built. An `input`-hook rewrite was sent but not saved, and queued `nextTurn`
  messages were never saved (§1, §7).

## Seeded spawn branches

`ctx.spawn_branch(at, prompt=None, *, seed=[…])` writes messages on the branch
before it runs. With no prompt, the sub-agent answers the seed with no user turn.
`seed_tool_call(name, args, result)` builds an injected call and result with a
Mistral-safe 9-character id (`docs/SEEDED-BRANCHES.md` §3–5).

- Spawning from inside a tool at `ctx.cursor.leaf` hangs the child from the
  planner's unanswered call, and Mistral answers HTTP 400. That is now refused
  before any write, naming the call (§2, §6).
- **A branch's model runs with its own key.** An Anthropic parent spawning
  `mistral-small` sent its own key and got HTTP 401. `ctx.complete` had the same
  leak (§7).

## Models without a session

`/model` completion works before the first chat and under `--connect`
(`docs/MODEL-PICKER.md` §2). A flow form's single-choice field is now a
filterable list with OK/Cancel pinned. Before, 22 models in a `RadioSet` pushed
the button row off any terminal under 38 rows (§1). The serve hello now lists
models (protocol 0.10, §7).

## HTML export

`tau --export-html REF [OUT]` writes a saved session, every branch, as one HTML
file. It has no script, a CSP that forbids every fetch, and images inlined as
`data:` URIs. The renderer, `export.render_session_html`, is a pure function any
head can call (`docs/HTML-EXPORT.md`). Measured: 311 entries with 9 images came
to 2.4 MB in 25 ms (§1).

The old `HTMLExporter` had no caller, and it rendered every reasoning block
empty. It read `text` where `ThinkingContent` stores `thinking`, and its test
fixture made the same mistake. `MarkdownExporter` had the same bug, also fixed.

## Smaller fixes

- **`ctx.cwd` is the session's cwd**, not the process cwd. Under `tau serve`, a
  path-confining `tool_call` hook resolved paths in the daemon's directory
  (`ca92e18`).
- **Mistral list-valued content.** Mistral's reasoning models stream `content` as
  a list of `text`/`thinking` chunks. The OpenAI provider now splits it into text
  and thinking (`90228c5`).
- **`tau -p` binds its cursor**, so entries written during a turn
  (`append_entry`, exposure entries) are kept for `--continue`. A hook-injected
  message is stored as a `customMessage` entry (`3a74ed7`).
- **`uv tool install ffwf-tau` works.** The metapackage declares the `tau`
  scripts (`283ca75`).
- **One way to find τ's home.** `resolve_tau_home` returns
  `<$TAU_PORTABLE_BASE>/.tau` if it exists, else `~/.tau`. Before, five sites
  computed `~/.tau` themselves (`docs/PORTABLE-HOME.md`).
- **Free-threaded CPython is gated.** 3.14t is in both release matrices, and a
  test fails if any τ module or default dependency re-enables the GIL. Two races
  are fixed, one a real torn read in the JMFTS mirror
  (`experiments/freethreading/RESULTS.md` §8). `scripts/build_runtime.py` builds
  a relocatable runtime, free-threaded by default
  (`experiments/distribution/RESULTS.md` §11).
- **Provider contract suite.** `tau-llm/tests/test_provider_contract.py` drives
  all three wires through their real transports over synthetic cassettes. Its
  first run marked four defects as strict xfails: `content_filter` → `stop`, the
  Anthropic SDK's silent retries, and missing reasoning tokens. All are fixed in
  this release (`docs/PROVIDER-COVERAGE.md`).
- **Docstring coverage is 1234/1234** marked objects, up from 600/1068 at the
  0.12.0 cycle (`ROADMAP.md`). `check_docs_coverage.py` is still not in the hook.

---

## For tau-code / RPC clients

- **Serve 0.10.** Hello refuses 0.8 and 0.9. `HelloResult.models` is required.
- **RPC 2.2.** 41 live verbs (was 40) and 15 event types (was 13)
  (`docs/RPC-PROTOCOL.md`).
- **Renamed:** `compaction_settings.enabled` → `auto`. The `set_auto_compaction`
  argument keeps the name `enabled`.
- **New verb:** `set_reasoning(level)` → `{level, leaf}`. `leaf` is the unchanged
  tip.
- **New events:** `provider_retry` (`attempt`, `max_retries`, `delay_seconds`,
  `error`, `error_detail`) and `follow_ups_dropped` (`dropped`: the texts,
  unbounded, written nowhere else).
- **New fields:** `message_end.response_headers`, `message_end.reasoning_level`,
  `error_detail` on `agent_end` and `provider_retry`, `parent_tool_call_id` on
  `tool_execution_*`, `Usage.reasoning_tokens`.
- **Changed meaning:** `dropped_tool_calls` counts calls not run, kept calls
  included. A mid-stream abort ends `end_reason: "aborted"`, where it used to say
  `"done"`.
- **Widened enum:** `CommandInfo.origin` adds `template` and `skill`.
- **Defaults a host now meets:** compaction on, and `stop_reason: "error"` for a
  `content_filter` finish on OpenAI-compatible wires.

## Not in this release

- **The trust gate.** Research only, nothing built. Six questions are open
  (`docs/TRUST-GATE.md`).
- **`ExtensionAPI.register_provider` and a package manager.** Research only
  (`docs/EXTENSION-PROVIDERS.md` §3–5). Calling `register_provider` at import
  time still fails on an extension's second load in a process.
- **Code mode through hooks.** Script calls bypass the hook pipeline. The
  `session` lifetime keeps interpreter state across navigation and fork
  (`experiments/sandbox-providers/RESULTS.md` §9–10).
- **A `context_window` decision.** It still defaults to 128000, and
  auto-compaction's thresholds come from it (`SILENT-DEFAULTS.md` §5).
- **OpenAI's Responses API** and OpenRouter `reasoning_details` replay
  (`REASONING-WIRE.md` §11).
- **A way back to "provider default" reasoning** once a session has set a level
  (`REASONING-WIRE.md` §11).
- **TUI `/export` and an RPC export verb** (`HTML-EXPORT.md` §6).
- **Cost on the RPC wire** (`PRICING-AND-COMPACTION-CONFIG.md` §7).
- **Retries shown for compaction and branch summaries** (`PROVIDER-RETRY.md` §6).

## New design records

* `docs/CODE-MODE.md`, `docs/BACKGROUND-TOOLS.md`, `docs/PORTABLE-HOME.md`
* `docs/REASONING-WIRE.md`, `docs/TURN-SETTINGS.md`, `docs/SILENT-DEFAULTS.md`
* `docs/PROVIDER-HTTP-ERRORS.md`, `docs/PROVIDER-RETRY.md`, `docs/RESPONSE-HEADERS.md`
* `docs/PRICING-AND-COMPACTION-CONFIG.md`, `docs/PROMPT-TEMPLATES-AND-SKILLS.md`
* `docs/SEEDED-BRANCHES.md`, `docs/MODEL-PICKER.md`, `docs/HTML-EXPORT.md`
* `docs/EXTENSION-PROVIDERS.md`, `docs/PROVIDER-COVERAGE.md`, `docs/TRUST-GATE.md`
