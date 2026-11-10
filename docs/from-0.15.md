# τ 0.15.0 -> 0.16.0 "Trusty": capability ledger

First pass 2026-10-31: a reader's survey of both trees. Refreshed 2026-11-10 against the code after Phase 8
(the terminal UI, `tau -p`, the ratatui example, the standard extensions), fix round 8E-6 and D26 stage T1.
Statuses are as of that day. `docs/plan.md` overrides this file where they disagree, and its "Phases 7–9"
section and "After 0.16.0" place each group. The release notes for 0.16.0 are written from this file.

Compared: 0.15 = tag `v0.15.0` (paths below are relative to it; `git worktree add /tmp/tau-0.15 v0.15.0`).
0.16 = this repository (`python/tau`, `crates/`, `tests/`, `docs/`; its paths are written with a
leading `0.16:`). Design cites: `model.md`, `glossary.md` = `docs/design/`; `Dn` = `docs/design/decisions/`;
`plan.md`, `fixes` = `docs/plan.md`, `docs/plan-phase5-fixes.md`; `guide` = `docs/guide/`.

Status vocabulary: **built** | **planned** (phase/stage cited) | **dropped** (by design, cited) |
**REGRESSION** (not built, not planned anywhere; "deferred" notes where a doc admits it but gives no phase) |
**post-0.16** (named in plan.md "After 0.16.0" or a decision as coming after the release). A row marked
`(unspecified)` is planned only as a heading (e.g. "TUI follow-ups") with no statement that this capability
is in it; those are the ones most worth confirming.

## 1. Test counts

| suite | counting rule | count |
|---|---|---|
| 0.15 `tau-llm/tests` | `def test_` + `async def test_` | 815 |
| 0.15 `tau-agent-core/tests` | same | 3,482 |
| 0.15 `tau-coding-agent/tests` | same | 2,065 |
| 0.15 `tau-jmfts/tests` | same | 109 |
| 0.15 `tau-meta` | none | 0 |
| **0.15 package tests, total** | | **6,471** (ROADMAP.md:120 reports 6,805 passed incl. parametrize) |
| 0.15 other | `agent-core` non-tests 74 (contract suites in `src/.../testing`), `examples/` 1, `scripts/` 1, `experiments/` 134 (research, exempt) | 210 |
| 0.16 `tests/behavior` | `def test_` (38 files) | 644 |
| 0.16 `tests/cli` | 14 files | 368 |
| 0.16 `tests/config` | 8 files | 223 |
| 0.16 `tests/providers` | 17 files | 180 |
| 0.16 `tests/tui` | 18 files | 153 |
| 0.16 `tests/serve` | 8 files | 124 |
| 0.16 `tests/examples` | 9 files | 114 |
| 0.16 `tests/tools` | 8 files | 97 |
| 0.16 `tests/core` | 4 files | 59 |
| **0.16 Python test functions, total** | 124 files, 113 `parametrize` uses | **1,962** (suite: 4,310 collected, because the behavior and TUI suites run twice, clients local then remote) |
| 0.16 Rust `#[test]` | tau-core 97, tau-tools 122, tau-store 26 (tau-py, bench-formats 0) | 245 |
| 0.16 Rust `proptest!` blocks | 4 properties inside the `#[test]` count above (`invariants.rs` x2, `replay.rs`, `store.rs`); one block, `invariants_hold_under_any_driver`, runs 512 cases | 4 blocks |
| **0.16 total** | 1,962 Python + 245 Rust | **2,207 test functions** |

Reading it: 0.16 has about 34% of 0.15's test functions (it had 15% on 2026-10-31, before the frontends and
the standard extensions). Where 0.15's tests lived (filename heuristic, overlapping): coding-agent: TUI/app/chat/dialog 541, CLI/print/headless 237, REPL 154,
serve/connect 93, extension commands 348; agent-core: extensions/hooks/flows/locks 761, rpc/capabilities 544,
session/log/catalog 449, tools 305, compaction 213, tree/surgery/cursor 167, suspension/step-debugger 217,
bus/background 127, skills/templates/attachments 97, codemode 44. The areas with 0.16 counterparts now are the TUI
(`tests/tui`), `-p` and the other commands (`tests/cli`), serve and the wire (`tests/serve`), extensions, hooks and
cursors (`tests/behavior`), tools, compaction, skills, templates and attachments. REPL, stdio RPC, bus, code mode and
the step debugger have none, which matches section 2: the suite is smaller because the surface is.

## 2. Capability ledger

### 2.1 Providers (`tau-llm` -> `python/tau/providers`)

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| OpenAI-compatible `/chat/completions` (llama.cpp, vLLM, OpenAI, gateways) | `tau-llm/.../providers/openai.py` (2,111 lines) | **built** | `0.16: python/tau/_providers/openai.py` (445 lines), streams parts + deltas, `usage` normalized, `usage_raw` kept; live-tested against a llama.cpp server |
| Anthropic Messages API | `providers/anthropic.py` | **built** | `0.16: _providers/anthropic.py`, `_anthropic_request.py`; thinking replay data, signatures |
| Google Gemini | `providers/google.py` | **built** | `0.16: _providers/gemini.py`; Gemini 3 thought signatures; no fallback signature when switching providers mid tool loop (D15) |
| Provider registry: `register_api` / `register_provider` / `ProviderSpec` (add a vendor in 6 lines) | `providers/base.py`, `providers/__init__.py` | **built** (changed) | Vendors are config groups (`base_url`, `api_key_env`, `provider`) per D13; extra providers are objects passed to `Driver` (D15). `tau config check` knows only the built-ins (D15 "Deferred") |
| Prompt cache marks (Anthropic `cache_control`) | `docs/PROMPT-CACHING.md`, `providers/anthropic.py` | **built** | `0.16: _anthropic_request.py` puts a mark on the system prompt and the last block of every request; `options.prompt_caching` (default on) turns it off, as 0.15's `prompt_cache: false` did. `cache_state(walk)` for the provider was **planned** in Phase 5 and never built (D24 §9: automatic pruning waits for it) |
| Prompt cache dialect: an Anthropic model behind an OpenAI-compatible gateway (`prompt_cache`, `prompt_cache_dialect: "anthropic"`) | `README.md` "Prompt caching", `providers/openai.py` | **REGRESSION** | The `openai` provider's options are `temperature`, `top_p`, `reasoning_effort`, `include_usage` and `extra_body` (`0.16: _providers/openai.py:41`); it sends no cache marks. A gateway to Anthropic is billed without caching unless `extra_body` carries the marks by hand |
| Cache-miss notice / `prompt_cache.py` ("your cache is not being read") | `tau_agent_core/prompt_cache.py`, `test_cache_miss_notice.py` | **built** (8E-2, changed) | the `usage` extension tells every client once per model, and only for a model with a `price` (D24 §3; `guide/built-ins.md`); `--set usage.cache_notice=false` silences it |
| Auth discovery: per-vendor env defaults (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`), literal `api_key`, credential hints | `providers/__init__.py:147-165`, `docs/PROVIDER-CREDENTIALS.md` | **dropped** | D13/glossary "api_key_env": a model must name its variable or give a literal (a literal warns); `ModelConfig.api_key` (`_config.py:116`). Hosted providers fail `check` without a key. Deliberate Fail Early, but a 0.15 config with no `api_key_env` will not work |
| Project `.env` as key source | none | **built** (new) | `0.16: _dotenv.py`, trust-hashed (D13) |
| Provider retry (408/429/5xx/529, 3 tries, `Retry-After`) | `tau_llm/retry.py`, `docs/PROVIDER-RETRY.md` | **built** (8E-1, changed) | D24 §4, `guide/built-ins.md`: `RetryProvider` ships in `tau` and `start_session` wraps every provider in it, so `tau`, `tau -p` and `tau serve` all retry; off with `tau ext disable retry` (`load: {retry: false}`) or `--set load.retry=false`. Retried: 408, 429, 502, 503, 504, 529, a 500 that carries `Retry-After`, and a connection that failed before any answer; never a plain 500, and never once a word of the reply has arrived. Waits `Retry-After`, else 1, 2 and 4 s; a `Retry-After` over 60 s fails at once; a `Retrying` delta tells every client (status bar; a stderr line under `-p`); no jitter; nothing to tune |
| HTTP errors typed (`ProviderHttpError` with status, body, headers) | `tau_llm/errors.py` | **built** | `SampleError(message, meta, status, retry_after)`; error recorded in the reply `meta.error` (glossary `finish_reason`/meta) |
| Response headers reach the caller (`DoneEvent.response_headers`) | `tau_llm/`, `docs/RESPONSE-HEADERS.md` | **built** (changed) | A provider maps chosen headers to `meta` keys, on success and on failure (`0.16: _providers/_wire.py` `meta_headers`; Anthropic's `request-id` as `request_id`, glossary); the full header set is not kept |
| Reasoning / thinking levels (`--thinking`, `thinking_level_map`, minimal..xhigh) | `tau_llm/models.py`, `docs/REASONING-WIRE.md` | **dropped** (changed) | No unified level. Per provider `options` (`thinking`, `reasoning_effort`) via config and `/config` (D15). No `thinking_level_map` |
| Image input (read tool images, `@image`, resize) | `tools/image_resize.py`, `tools/read.py` | **post-0.16** | plan.md Phase 8 "Dropped or deferred from 0.15": image input until after 0.16.0 (D15 "Deferred"); `model.md:69` shows `User(text, images*)` but nothing builds it |
| Constrained decoding / llguidance grammar channel (`DecodeConstraints`) | `tau_llm/constraints.py`, `grammar.py` | **post-0.16** | plan.md "After 0.16.0 ... Later": extension-shaped. Reachable by hand through `options.extra_body` |
| models.dev catalog (`python -m tau_llm.catalog providers/search/show/config`) | `tau_llm/catalog.py` | **post-0.16** | plan.md "After 0.16.0 ... Later": a `tau config` subcommand that writes model entries |
| Per-endpoint wire quirks `Compat` (`max_tokens` vs `max_completion_tokens`, stream_options) | `tau_llm/compat.py` | **dropped** (changed) | `options.extra_body` (`examples/config.json`), `include_usage` option (D15) |
| Token counting for context decisions (tokenizers, calibrated char-class counter) | `tau_llm/tokens.py` (640 lines) | **built** (changed) | D26 stage T1 (8E-6): τ counts in the unit the server bills in. What was sent is the reply's own `usage`; what is not sent yet is bounded by its bytes (plus 16 tokens a message) and written `≤N`; the sum rule, `Cursor.count`, `/usage`'s counts line. Compaction, its checks and the tree screen use it. τ never estimates from characters. Nothing has to be installed (`guide/built-ins.md`) |
| Tokenizers, a calibrated counter, `ffwf-tau-tokens`, count calls | `tau_llm/tokens.py`, optional tokenizer in the TUI | **partly built** (D26 T2, T3; T4–T7 planned) | T2: every family's usage facts and proved spans, and the fixture recorder (no fixture is recorded yet). T3: the built-in `tokens` learns a per-model heuristic from proved spans (`~N` counts, `/tokens`, `tau tokens show|reset`, `$TAU_HOME/calibration/`); it ships no fitted prior. The tokenizer wheel, exact counts and count calls (T4 to T7) are not built and plan.md places them in no release |
| Tolerant JSON for streamed tool-call args (`json_parse`), repair count | `tau_llm/json_parse.py` | **dropped** (changed) | Calls keep `args_raw` byte for byte; bad args come back to the model as an `Err` (`test_scenarios.py` "bad args"); no repair, no repair count |
| llama.cpp `timings` readout / telemetry strip | `examples/70_telemetry.py`, `chat_widgets.format_telemetry` | **post-0.16** (minor) | plan.md "After 0.16.0 ... Later": the strip. `usage_raw` is kept; folding `timings` onto `meta` is D26 T2, not built |
| Provider lifetime/pool, `aclose()` | `docs/PROVIDER-LIFETIME.md` | **built** | Long-lived provider objects, caller closes (D15, `builtin_providers`) |
| Mock provider | `tau_agent_core/testing`, tests | **built** | `0.16: _providers/mock.py`, `FakeProvider` in `tests/behavior`; `provider: mock` is a model any config can name (`guide/built-ins.md` runs on it) |
| Third-party / extension-supplied providers | `docs/EXTENSION-PROVIDERS.md` (research) | **built** | Any object with `check/sample/aclose` passed to `Driver` |

### 2.2 Tools

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| `read` (offset/limit, truncation, images) | `tools/read.py` | **built** (text only) | `0.16: crates/tau-tools/src/read.rs`, `tau.ReadTool`; no images (see 2.1), binary refused |
| `write` | `tools/write.py` | **built** | atomic temp+rename, creates dirs, `tau.WriteTool` |
| `grep` | `tools/grep.py` | **built** | ripgrep engine, ignore-aware, `files`, `glob`, `ignore_case` |
| `bash` (timeout, streaming, kill) | `tools/bash.py` | **built** | kills the process group on cancel/timeout; Linux-only (`/proc`); live `ToolProgress` (`examples/bash_progress.py`) |
| `edit` (search/replace with diff rendering) | `tools/edit.py` | **built** (changed) | `crates/tau-tools/src/edit.rs`, `tau.EditTool`. One replacement per call, `replace_all` as in 0.15; the diff is a real unified diff; atomic write (0.15 truncated in place); CRLF and a missing final newline are kept (0.15 turned CRLF into LF); a non-UTF-8 file is refused; the errors say how to fix the call |
| `find` (glob/regex file finder) | `tools/find.py` | **built** (changed) | `crates/tau-tools/src/find.rs`, `tau.FindTool`. One glob (`pattern`) on `grep`'s walk, so ignore rules apply (0.15 listed everything); hidden files and directories are listed, `.git` is not; sorted, limited like `grep`. No `regex` or `type` filter, and no directories (`bash` has `find`) |
| `ls` | `tools/ls.py` | **dropped** | `bash` covers it (decided 2026-11-01) |
| Output limit 10 kB + overflow file | `tool_output.py`, `docs/` | **built** (changed) | `max_output`, overflow file `<id>.out` in `/tmp/tau-<uid>` (0600), 64 MiB cap; pruning of old overflow files deferred (plan.md "Deferred 2026-10-29") |
| Tool factories (`create_coding_tools`, `create_read_only_tools`) | `tools/__init__.py` | **built** (changed) | `base_tools(cwd)` returns all six; the shared start gets them from the built-in extension `base_tools` (off with `load`); no read-only preset (`agents.tools` limits what a sub-agent is offered) |
| Function tools (`@tool`, schema from signature) | `tool_schema.py`, `tools/base.py` | **built** | `0.16: _functions.py`, `Annotated[T,"text"]` descriptions (D16 section 1) |
| Parallel tool calls; sequential mode (`tool_execution_mode`) | `settings.py:48` | **planned** | Calls run in parallel; Q21 `exclusive` tool "not built yet" (`model.md:608`: `Tool` has no `exclusive`) |
| Tool allow/deny lists: `--tools`, `--no-tools`, `--exclude-tools`, `--no-builtin-tools`, `tool_exposure`, `tool_declaration` | `cli.py`, `tool_catalog.py`, `session_log.CONFIG_KEYS` | **post-0.16** | D22 §9: tools come from extensions (`load`, `-e`); `tau ext disable base_tools` drops the base tools; a per-run tool list is Tectum's `--no-tools`, after 0.16.0. Library side: a `pre_sample` hook sets the tools offered (fixes D2), and the driver refuses a tool the sample was not offered (D24 "Driver: a tool that was not offered is not run") |
| Background tools (`background.py` built-in extension) | `extensions_builtin/background.py`, `docs/BACKGROUND-TOOLS.md` | **post-0.16** | plan.md "After 0.16.0 ... Later": extension-shaped |
| Code mode (Python script calling every tool in a sandbox) | `tau_agent_core/codemode/` (10 files, 44 tests) | **post-0.16** | plan.md "After 0.16.0 ... Later": extension-shaped; experimental in 0.15 |
| Tool progress events | `tool_pipeline.py` | **built** | `ToolProgress`, `Progress` (D15) |
| Truncated tool-call detection (`Length` stop) | `truncation.py`, `docs/TRUNCATED-TOOL-CALLS.md` | **built** (changed) | `Stop::Length` is an ordinary reply; `Wait` (model.md:428) |

### 2.3 Session store and tree operations

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Durable session log (JSONL `SessionLog`) | `session_log.py`, `session_manager.py` | **built** (changed) | `0.16: crates/tau-store`: length-prefixed checksummed record log + scratchpad file (D12); torn tails cut; I7 proptest |
| 0.15 session files readable | `~/.tau/sessions/*.jsonl` | **dropped** | D9 (superseded): no converter; 0.16 ignores 0.15 files; `~/.tau-0.15` archive. Release notes owed (plan.md Phase 9) |
| Per-session write lock | `session_lock.py` (flock) | **built** | the lease (`Leased` error) |
| Tree of entries, branches, leaf | `conversation_tree.py` | **built** (changed) | `0.16: crates/tau-core/src/tree`: nodes, `walk`, `children`, `lca`, labels, `config_at` |
| Cursors / multi-writer (`Cursor`, sub-agent cursors) | `cursor.py`, `docs/CURSORS.md` | **built** (changed) | cursors `{id, at}`, `open`, `go_to`, scratchpad items wake cursors (D6, model.md) |
| `navigate` / go to node | `tree_ops.py` | **built** | `go_to` (glossary "go to"); in the TUI, Enter in the tree (Ctrl+G) and Ctrl+Z |
| Branch from any node, in-session fork | `ctx.fork`, `tree_ops.fork` | **built** (renamed) | "branch" (glossary; rule 3 bans in-session "fork") |
| Re-run a tool batch | `Session.rerun` | **built** | "re-run" (glossary): go to the `Assistant`, step, sibling `Results` |
| `--fork REF` / copy a session into a new one | `cli.py:--fork`, `session_manager` | **built** (changed) | D18: `Session.fork(node, now, directory)` copies one walk in one call; `--fork REF[:NODE]` is a flag of `tau`, `tau -p` and `tau serve`; provenance is `forked_from` in `info.json` (not `copied_from`, which stays same-session) |
| `summarize_and_navigate` (branch summary on leaving a branch) | `tree_ops.py`, `capabilities.py:1991` | **planned** (unspecified) | D3 compaction extension builds "a summary node under a kept prefix"; branch summaries are not named |
| `elide_span` (fold a span out of context) | `tree_surgery.py`, `tree_ops.py` | **post-0.16** | plan.md "After 0.16.0" (tau-code): elide "become commands or extensions, or are dropped"; no wire counterpart (D21, D27). `/prune` and compaction fold context in its place |
| `commit_branch`, `paste_subtree` | `tree_surgery.py` | **post-0.16** | same sentence in plan.md "After 0.16.0"; glossary "branch" retires `commit_branch` |
| Tree browser TUI (edit the tree) | `tree_browser.py`, `docs/TREE-BROWSER-AS-EDITOR.md` | **built** (changed) | D23 §6 and "Built: tree view": Ctrl+G, the viewed walk straight with other branches as folded stubs, cursors marked, closed ones dimmed; go to, open a cursor, label, jump to a node's `copied_from`/`derived_from`. It does not edit the tree (no elide, paste or commit) |
| Model compare (N cursors, one prompt, keep one) | `compare.py`, `compare_view.py`, `/compare` | **post-0.16** | plan.md "After 0.16.0" (tau-code sentence lists "compare"); D23 §7: several watched cursors fit it later |
| Seeded branches / spawn (`spawn_branch`, `seed`) | `seed.py`, `docs/SEEDED-BRANCHES.md` | **built** (changed) | Stage B (authored input, `tree.rest`, `tree.abort`) and the sub-agent extension, `agents` (2.5) |
| Session listing (`list_sessions`) | `session_catalog.py` | **built** (changed) | D18: `tau.list_sessions`, `tau sessions [--all] [--json]`: a scan of `$TAU_HOME/sessions`, no index; message count and last message are **dropped** (they need a full read of the log), the title is the name, else the first message |
| Session picker (`--resume`) | `session_picker.py` | **built** (8C-2) | `tau -r` and Ctrl+O: this directory's sessions, Tab widens to every directory, `/` filters; marks `served`, `in use`, `no reply` (`guide/tui.md`). `-r` with `-p` is a usage error (exit 2): name the session with `--session REF` or `-c` |
| Session naming (`--name`, `/name`) | `session_log.py` | **built** | D18: `-n NAME`, `tau sessions name`, `/name` from the built-in `session_name`; the name is in `info.json`. Auto-naming (`examples/42_session_autoname.py`) is **dropped**: the fallback title is the first message |
| Session export: Markdown (`export.py`) | `export.py` | **built** (changed) | D18: `tau export REF [-o FILE] [--thinking]`, `tau.to_markdown`; deterministic, one walk (`REF:NODE`), thinking only on request |
| Session export: HTML (`--export-html`) | `export.py`, `docs/HTML-EXPORT.md` | **post-0.16** | D18: not built; plan.md "After 0.16.0 ... Later" |
| 0.15 session files (`sessions/<dashed-cwd>/<time>_<uuid>.jsonl`), the read-only second view of a held session | `session_store.py` | **dropped** (changed) | D9, D18: a directory per session under `$TAU_HOME/sessions`; a held session is `Leased` (fork it, read it with `tau export`, or attach to the `tau serve` that holds it) |
| Session import/export JSONL (JMFTS) | `cli.py:--import-session/--export-session` | **post-0.16** | tied to JMFTS (below) |
| `--no-session` ephemeral | `cli.py` | **built** | `Session.create` with no directory is in memory (`examples/one_off.py`); `--no-session` is in `session_flags` and `start_session` |
| Message timestamps | `docs/MESSAGE-TIMESTAMPS.md` | **built** | `Node.time` |
| Crash behavior (partial reply saved, finalize) | `docs/TURN-FAILURES.md` | **built** (changed) | D4: rest at the batch, no repair; `Interrupted` replies kept |
| Transcript window (render cap for long sessions) | `docs/TRANSCRIPT-WINDOW.md` | **built** (8C-3, changed) | D23 "Built: 8C-3": the TUI draws the last four turns (or 100 nodes), slides along the walk at the edges (Ctrl+Up/Down, the wheel); the session still holds every node. D14 "deferred" still stands for the store: every body stays in memory |
| Entry annotations (`annotate` hook writes `ext` JSON on an entry) | `entry_annotations.py` | **dropped** | glossary Retired terms: `annotate` -> event observers; D8 |
| Custom entries / extension messages in the tree | `docs/EXTENSION-MESSAGES.md` | **built** (changed) | `Body::Custom` nodes never reach the model (I1) |

### 2.4 Compaction and context management

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Auto-compaction (on by default for CLI heads), `/compact` | `compaction.py`, `compaction_policy.py`, `config.py:parse_compaction_config` | **built** (8E-3, 8E-6, changed) | D24 §2, `guide/built-ins.md`: the built-in `compaction` extension. `/compact [focus]` and a soft limit (80% of the hard one, after a finished turn) and a hard limit (the window less `reserve`, just before a request that would not fit); both need the model's `context_window`. The old line stays in the tree and the cursor moves to a new one; counts come from the server's `usage` (2.1 "Token counting"); the hard limit never sends a request it cannot show fits |
| `/autocompact` and the `compaction` config block (`auto`, `reserve_tokens`, `keep_recent_tokens`, `summarizer`) | `capabilities.py` flows, `README.md` "Compaction" | **built** (changed) | `extensions.compaction` options, set with `/config compaction ...` or `tau config set`: `auto` (false stops both limits), `reserve`, `keep`, `soft`, `hard`, `model` (the summarizer). There is no `/autocompact` command |
| A model's own `compaction` block (replaces the top-level one for that model) | `README.md` "Compaction" | **REGRESSION** | `ModelConfig` has no compaction key (`0.16: _config_layers.py`); the options belong to the extension and to a branch's `Config` node, not to a model |
| Cold summary from a cheaper model | `compaction.py` | **built** | `model` option; the summary is written by a second cursor `compaction:N` (a detached request on the branch), closed when done, counted by `/usage` |
| Compaction entry + splice in `context_for` | `session_log.py` | **dropped** | D3, glossary Retired terms: a summary node under a kept prefix, on a new line; `copied_from` and `derived_from` on nodes record where it came from (M1) |
| System prompt kept across compaction/elide | `docs/SYSTEM-PROMPT-IN-THE-FOLD.md` | **built** (by construction) | `walk` is a plain parent chain (I1) |
| Context margin / window sizing (`context_margin`, `context_window`) | `settings.py:28`, `docs/SILENT-DEFAULTS.md` | **built** (8E-2, changed) | `ModelConfig.context_window` (optional, positive) and `reserve`; a model without a window is never compacted by itself and the status bar says so |
| Prune | no 0.15 counterpart found | **built** (8E-3, new) | D24 §9: `/prune`, no model: keeps the last two turns and turns older ones into a short transcript; drops reasoning, cuts long call arguments, reduces older results to `ok`/`error`. Automatic pruning is not built (`prune.auto` must stay `false`; it waits for a way to know the cache's state) |
| `StepLimit` / `--max-turns` | `cli.py:--max-turns`, `config max_turns` | **built** (changed) | `tau.StepLimit` extension, `extensions.step_limit.limit` (D1); per run `--set step_limit.limit=N`; no `--max-turns` flag |
| Follow-up / queued messages dropped notices | `follow_ups.py` | **dropped** | D2: queueing is an extension's |

### 2.5 Extensions, hooks, skills, prompt templates

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Extension API (`ExtensionAPI`, 42 methods, runner, registry) | `extensions/` | **dropped** (rebuilt) | D8: no compatibility; hooks `pre_input`, `pre_sample`, `pre_reply`, `post_reply`, `pre_tool`, `pre_result`, `post_result`, `post_step`, `pre_step` (model.md section 5, glossary Hooks). Mapping table in D8 and Retired terms; `guide/extensions.md` |
| Lifecycle hooks (`session_start`, shutdown, `turn_end` ...) | `extension_types.py` | **dropped** | D8: "No lifecycle hooks until a Phase 5 extension needs one (Q23)" |
| Hook error policy / handler policies / deadlines | `policies.py`, `docs/HOOK-ERRORS.md` | **dropped** | Q9; `HookFailure` in `CursorView` (D16) |
| Extension commands (`/name`), flows (forms, completion) | `flows.py`, `capabilities.py` | **built** | `@command` functions, `Form`, `commandline`, `Client.run/commands/complete` (D16 stages 1-3) |
| Command namespacing (`/ext:cmd`) | `docs/EXTENSION-NAMESPACE.md` | **built** (changed) | D19: `/extension:name` (`/tau:model`, `/skills:review`); a bare name two extensions share is refused; an unknown `/extension:name` is an error and an unknown `/name` is text; `/config` is reserved. 0.15's literal `ext:` prefix is dropped (`guide/tui.md`) |
| Extension config (`--ext-config NAME:KEY=VALUE`, `config.json extensions`) | `cli.py`, `extension_auth.py` | **built** (changed) | `options` dataclass, `extensions.<name>` in layers, `/config` (a form, with a save switch), `tau config set`, per-run `--set EXTENSION.FIELD=VALUE`, `Cursor.set_options` |
| Extension locks (stop a session, ask the attached user) | `extension_locks.py` | **dropped** | glossary: suspension/responder -> auto-step off + `question` (D16) |
| Step debugger: `ctx.suspend`, suspension UI, `80_step_mode.py` | `suspension.py`, `suspensions.py`, `suspension_ui.py` | **dropped** (rebuilt smaller) | Retired terms "suspension, responder -> auto-step off; a question". Phase 5 "debug frontend (auto-step off)" is not built and plan.md Phase 8 does not list it: turning auto-step off from a hook and answering a question is what exists |
| Permission gate | `examples/01_permission_gate.py`, `30_permission_gate.py` (examples only; no built-in) | **built** (8E-4, new) | D24 §7: the built-in `permissions` extension; rules in `ask`, `allow`, `deny`, asking nothing until configured; `yes \| always \| no`; `tau -p --answer permissions=yes` |
| Sub-agents / delegate / swarm | `examples/20_delegate.py`, `50_review_swarm.py`, `51_delegate_fleet.py` | **built** (8E-4, changed) | D24 §8: the built-in `agents` extension, off by default (`tau ext enable agents`); the `agent(task, to=None)` tool opens a cursor `agent:N` in the same process (dropped from the example: child processes, chain mode, `tasks[]`, `max_usd`, `max_seconds`); options `model` and `tools` |
| Extension discovery: drop-ins `~/.tau/extensions`, installed entry points, `-e PATH\|MODULE` | `extension_sources.py`, `docs/EXTENSION-PACKAGES.md` | **built** (changed) | D19: the built-ins, the `tau.extensions` entry-point group (a module, not `module:attr`), `$TAU_HOME/extensions/`, the trusted project's `.tau/extensions/`, `-e PATH\|KEY`; later sources shadow earlier ones; a source that fails to load stops the start (0.15 carried on). Code in a project loads only after `tau trust` pins its bytes (`guide/extensions.md`). tau-search's extension loads through the entry point once ported (after 0.16.0) |
| Extension authorization store (`tau ext authorize/revoke/enable/disable/list`) | `extension_auth.py`, `ext_cli.py` | **dropped** (changed) | D19: authorize/revoke and version pins are not kept (the user installed the code; a project's is covered by `tau trust`). `tau ext list [--json]`, `tau ext enable KEY` and `tau ext disable KEY` are built and write `load` into your config |
| Hot reload of extensions | `reload_extension` capability | **built** (library only) | fixes C2: `Driver.reload(extensions)`, refused with `Busy` while an effect is out. No command or key calls it; a running `tau` sees edited prompt and skill files without it (`guide/built-ins.md`) |
| Skills (`SKILL.md` dirs, `--skill`, `--no-skills`) | `skills.py`, `resources.py` | **built** (8E-1, changed) | D24 §6: the built-in `skills` extension; `$TAU_HOME/skills/` and the trusted project's `.tau/skills/` (pinned by trust); `/NAME`, listed for the model at a session's first step; no `--skill` or `--no-skills` (use `load` or `-e`), `/skill:NAME` is `/NAME` (or `/skills:NAME`) |
| Prompt templates (`/name args` from `~/.tau/prompts`, `.tau/prompts`) | `prompt_templates.py` | **built** (8E-1, changed) | D24 §6: the same extension; `$ARGUMENTS`, `$1`..`$9` and above, `${N}`, `$$` (no `${N:-default}`, `${@:N:L}`, `$@`); frontmatter `description`, `argument-hint`, `disable-model-invocation`, `name` (`guide/built-ins.md`) |
| Context files (AGENTS.md, CLAUDE.md, `.tau/SYSTEM.md`) into the system prompt | `backends.py`, `cli.py:--no-context-files` | **built** | D20: `tau.system_prompt`, once at creation, recorded as the `System` node; names are `AGENTS.md`, `CLAUDE.md` only; `tau context` shows the parts |
| `ext:` bus / request-reply between extensions (`api.request`) | `docs/EXT-REQUEST.md` | **dropped** | Not in glossary; handles are the cross-extension surface (`TreeHandle`) |
| NATS bus extension, `--bus` | `extensions_builtin/nats_bus.py` | **post-0.16** | plan.md "After 0.16.0 ... Later": the NATS bus; extension-shaped |
| Budget / cost ledger examples (`24_budget.py`, `43_budget_ledger.py`) | `examples/` | **REGRESSION** | Cost is shown by `usage` (2.10), but no example or extension stops a run at a spend. 0.15 had 40 numbered examples; 0.16 ships 10 Python programs, a sample config, `examples/ratatui/` and 7 speculative prototypes |
| SDK entry `create_agent_session()` | `sdk.py` | **built** (changed) | `Driver`, `LocalClient`, `tau.start_session`, `tau.open_client`; the public surface is `docs/api/surface.txt` and `docs/reference/` (Phase 7C) |

### 2.6 Permissions

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Built-in permission system | none; example extensions only | **built** (8E-4, new) | D24 §7: `permissions`, rules `TOOL` or `TOOL:GLOB` in `ask`, `allow`, `deny`; `always` writes the exact rule to your config |
| Protected paths / sandbox | `examples/31_protected_paths.py` | **built** (8E-4, changed) | `deny: ["write:/etc/*", "edit:/etc/*"]`; paths are matched as written and as resolved (`..`, symlinks). Not a sandbox, and `bash` rules are best effort (D24 §7, `guide/built-ins.md`) |
| Headless approval (`--ui-defaults confirm=yes,...`) | `cli.py:--ui-defaults`, `headless.py` | **built** (changed) | D22: `tau -p --answer EXTENSION[:TEXT]=VALUE`. Rules match who asked and what, not a dialog kind; a question no rule matches is declined (stderr says so), and a value the question's schema refuses aborts the run (exit 1). D16 ruling 1 holds: `-p` is the client that answers |

### 2.7 Config and trust

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| `~/.tau/config.json` models (`backend`, `model`, `base_url`, `api_key`) | `config.py`, `tau_default_config.json` | **built** (changed) | D13: `provider` replaces `backend`; groups, qualified names `group:name`, layered merge. 0.15 shipped a sample config with three models; 0.16 ships none and `examples/config.json` is a sample |
| Model entry keys `context_window`, `cost` | `README.md` "Compaction", "Cost" | **built** (8E-2, changed) | `context_window`; `cost` is `price` `{input, output, cache_read, cache_write}`, USD per million, all four required (D13 amended, `guide/built-ins.md`) |
| Project `.tau/config.json` | `setting_sources.py` | **built** | gated by trust (D13) |
| `TAU_HOME`, portable home | `tau_home.py`, `docs/PORTABLE-HOME.md` | **built** | `0.16: _trust.tau_home` |
| Project trust gate (`TRUST-GATE.md`, research only in 0.15) | none shipped | **built** (new) | `tau trust`, `trusted.json` with content hashes, `.env` hashed, project extensions and (since 8E-1) prompt and skill files pinned, symlink/owner/700 checks |
| `tau config check [--show]` | none | **built** (new) | `0.16: cli.py`; since 8E-6 also `--clean` and `--purge EXTENSION` (`guide/configuration.md`) |
| Config writes by the program (`save_config`, `update_config`, `/model --save`) | `config.py:82-115` | **built** (changed) | `tau config set EXTENSION.FIELD=VALUE`, `/config EXTENSION name=value --save`, the options form's "save for every session" switch, `tau ext enable/disable`, the theme picker; the program writes only your user file (a project layer has no write, D13). `/model` is a `Config` node on the branch and is not saved |
| Model unavailable recorded on session (`model_availability`) | `model_availability.py` | **built** (changed) | model named by a `Config` node, resolved at sample; unknown name is a failed sample (`ConfigError`) |
| Defaults: `default_model` | `config.py` | **built** | `select_model` / `load_config` (`_startup.py`) |
| `steering_strategy`, `enter_key`, `attachment_inline_limit`, `theme` keys | `tau_default_config.json` | **built** / **dropped** | steering dropped (D2); `theme` is the top-level `tui` key, `{"tui": {"theme": ...}}` (D13 amended; the user's file only); `enter_key` is dropped (Enter sends, Ctrl+J starts a line); `attachment_inline_limit` is `extensions.attachments.limit` (bytes) |
| `session_store.backend` (`file` / `jmfts`) | `store_factory.py` | **post-0.16** | JMFTS |
| Configurable `max_tokens`, `temperature` | `settings.py` | **built** | `max_tokens` typed in `ModelConfig`; `temperature` via provider `options` (D15) |

### 2.8 CLI: every 0.15 flag and subcommand (`tau --help`, `tau_coding_agent/cli.py:132-470`)

0.16's CLI is `tau` (the terminal UI), `tau -p`, `tau --version` and the subcommands `config` (`check`, `set`), `ext` (`list`, `enable`, `disable`), `sessions`, `export`, `context`, `trust` and `serve` (`0.16: python/tau/cli.py`; `tau --help`). The first argument decides: a subcommand's name runs it, anything else is the launch command line, so `tau -p sessions` sends the word "sessions". D22 §9 is the table of what became of each flag. Statuses below are about the capability.

| 0.15 flag / subcommand | 0.16 status | note |
|---|---|---|
| `tau` (no args) -> Textual TUI | **built** (8C) | D23: `tau` opens `tau.tui.TauApp` over `open_client`; without a terminal it is a usage error. `guide/tui.md` |
| `MESSAGES...` with `@file` references | **built** (changed) | D22: the words are joined with spaces (0.15: newlines), then standard input after a blank line when it is not a terminal (a pipe silent for 3 s is left out with a stderr line; `-` as the first word waits for it). `@file` is the `attachments` extension (2.9, 8E-1), which `-p` goes through like any input |
| `-p`, `--print` | **built** (changed) | D22: a switch, not a flag that takes the message. Prints only the last reply on stdout; exit 0/1/2/3/130 (3, stopped before finishing, is new); `-p` attaches to the `tau serve` that holds the session |
| `--mode text` / `json` | **built** (changed) | `text` is the default and the flag is dropped; `json` is `--json`: the wire's own events, one object per line after a `Welcome` line (not 0.15's bus events) |
| `--mode rpc` (JSON-RPC 2.0 over stdio, ~45 verbs, Tectum contract) | **built** (changed) | replaced by `tau serve` JSON-RPC over a Unix socket (D21); stdio is Tectum's, after 0.16.0 |
| `--mode repl` | **dropped** | plan.md "Dropped or deferred from 0.15" (2026-11-01): the TUI and `examples/chat.py` cover it. 154 0.15 tests |
| `-v`, `--version` | **built** | D22: prints `tau VERSION (protocol N)`, the τ version and the wire protocol a client must speak; a flag of the launch command line only |
| `-m`, `--model` | **built** | D22: the model of a new session (D18: an existing session records its own; `/model` switches a running one) |
| `--provider` | **dropped** | the model's `provider` key in config (D22 §9) |
| `--theme` | **built** (8C-1) | D23: for one run; a usage error with `-p` |
| `-t`, `--tools` | **post-0.16** | tools come from extensions (`load`, `-e`); a per-run tool list is Tectum's `--no-tools`, after 0.16.0 (D22 §9) |
| `-nt`, `--no-tools` | **post-0.16** | same |
| `-xt`, `--exclude-tools` | **post-0.16** | same |
| `-nbt`, `--no-builtin-tools` | **post-0.16** | `load: {base_tools: false}` (`tau ext disable base_tools`) turns the base tools off; `--no-extensions` does not |
| `-e`, `--extension PATH\|KEY` | **built** (changed) | D19: a path, or the key of a source; naming it is the consent. `tau`, `tau -p`, `tau serve` and `tau ext list` take it; not for a session a server holds (D22 §8) |
| `--no-extensions` | **built** (changed); `-ne` **dropped** | D19 has the long form only (D22 §9): loads no package or drop-in; the built-ins (`agents`, `attachments`, `base_tools`, `compaction`, `permissions`, `prune`, `skills`, `step_limit`, `usage`, and the `retry` wrapper) follow `load` |
| `--bus` | **post-0.16** | NATS extension (plan.md "After 0.16.0 ... Later") |
| `--no-session` | **built** | D18: `session_flags`, `start_session` |
| `--max-turns N` | **dropped** (changed) | `StepLimit`'s option; for one run `--set step_limit.limit=N` (D22). A run that stops there exits 3 and says so (0.15 stopped silently and exited 0) |
| `--ext-config NAME:KEY=VALUE` | **built** (changed) | `--set EXTENSION.FIELD=VALUE` (D22), repeatable: the session settings of D13, read and checked as `tau config set` does, for one run; `-p`, `tau` and `tau serve` take it. `TAU_SETTINGS` in the environment does the same |
| `--ui-defaults METHOD=ANSWER,...` | **built** (changed) | `--answer EXTENSION[:TEXT]=VALUE` (D22); see 2.6 |
| `--append-system-prompt TEXT` | **built** | D20: `session_flags`; an error with `-c`, `-r`, `--session`, `--fork` (0.15 ignored it) |
| `--system-prompt TEXT` | **built** | D20: the CLI layer of the `system_prompt` config key; no `{{fields}}` |
| `-nc`, `--no-context-files` | **built** | D20: `session_flags`; accepted when continuing or forking (nothing is read then) |
| `--prompt-template PATH`, `--no-prompt-templates` | **dropped** (changed) | D24 §6: files in `$TAU_HOME/prompts/` and `.tau/prompts/`; `tau ext disable skills` or `-e` |
| `--skill PATH`, `--no-skills` | **dropped** (changed) | D24 §6: files in `$TAU_HOME/skills/` and `.tau/skills/`; `tau ext disable skills` or `-e` |
| `-c`, `--continue` | **built** | D18: the newest session of the working directory; `tau -p` takes it (0.15: `-p` only), and attaches if a server holds the session |
| `-r`, `--resume` | **built** (8C-2) | D18, D23 §5: opens the picker before the terminal UI starts. A usage error with `-p` (exit 2): name the session with `--session REF` or `-c` |
| `--session REF` | **built** | D18: a path, an id, a name or a unique id prefix, searched over the whole sessions directory |
| `--fork REF` | **built** (changed) | D18: `REF[:NODE]`; see 2.3 |
| `-n`, `--name` | **built** | D18 |
| `--thinking LEVEL` | **dropped** (changed) | provider `options` (2.1) |
| `--store file\|jmfts` | **post-0.16** | JMFTS |
| `--connect ADDR`, `--cwd PATH` | **dropped** (changed) | attaching is automatic: `tau`, `tau -p` and `tau -r` connect to the `tau serve` that holds the session (D22 §8, D23 §1); `cd` sets the directory |
| `--session-dir DIR` | **built** | D18: a flag only; `TAU_HOME` already moves everything |
| `--import-session PATH` / `--export-session REF PATH` | **post-0.16** | JMFTS |
| `--export-html REF [OUT]` | **post-0.16** | see 2.3; Markdown is `tau export` |
| `--verbose` | **built** (changed) | D22: on stderr, a report of the start (session, directory, model, config files, extensions, system prompt parts) and under `-p` a line per node, step ending and question, and each extension's shown line when it changes; not an arguments dump or a debug log (`tau.trace` is the profiler). `tau serve` takes it too |
| `--fun` / `--no-fun` (random tagline) | **dropped** (cosmetic) | plan.md "After 0.16.0 ... Dropped"; D23 §7: `tagline.py` is not carried |
| `tau serve` (default 127.0.0.1) | **built** (changed) | D21: one server per session, on a Unix socket (`$TAU_HOME/serve/<session id>.sock`; `--socket PATH`), no TCP, port or token in 0.16. Takes the session flags. Other machines use OpenSSH's socket forwarding |
| `tau serve --listen ADDR` | **dropped** | D21 "0.15 behaviour dropped": `--socket PATH` replaces it; TCP for browsers waits for tau-code's web client (D21 Open 4) |
| `tau serve -d` / `--daemon` | **built** (changed) | `-d`, `--detach`; prints the serve record |
| `tau serve --json` | **built** | one JSON object per line: the record, or an error |
| `tau serve --stop` | **built** | stops the server of the session the session flags name |
| `tau serve --tail [SESSION]` | **post-0.16** | plan.md "After 0.16.0 ... Later" |
| `tau serve --connect ADDR` | **dropped** | D21: attaching is automatic (D22 §8); `--connect` is dropped with `--listen` |
| `tau serve --web-root DIR` (static web client, origin check) | **post-0.16** | D21 and plan.md "After 0.16.0" (tau-code): the web client comes with TCP, after 0.16.0 |
| `tau serve --schema` | **built** | prints the wire's JSON Schema (`docs/protocol.schema.json`); the ratatui example and tau-code generate types from it |
| `tau ext list [-e] [--json]` | **built** | D19; also `--no-extensions`. One row per source and why it is on or off |
| `tau ext authorize NAME [--package]` | **dropped** | see 2.5 |
| `tau ext revoke IDENTIFIER` | **dropped** | see 2.5 |
| `tau ext disable NAME` / `tau ext enable NAME` | **built** | D19: writes `load` into your config (`tau ext disable retry` too) |
| `tau config check [--show MODEL]` | **built** (new) | also `--clean`, `--purge EXTENSION`, `--layer` |
| `tau config set EXTENSION.FIELD=VALUE` | **built** (new) | writes extension options into your config file |
| `tau sessions [--all] [--json]`, `tau sessions name`, `tau export`, `tau context [--show]` | **built** (new) | D18, D20; see 2.3 |
| `tau trust [DIR]` | **built** (new) | |
| env `TAU_HOME` | **built** | |

### 2.9 Frontends and interactive features

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Textual TUI (chat, sidebar, palette, dialogs, themes mocha/latte/gruvbox/ansi + user themes) | `app.py`, `chat_widgets.py`, `themes.py`, `tau.tcss`, `dialogs.py`, `modals.py` | **built** (8C, changed) | D23, `guide/tui.md`: `python/tau/tui`, a `Client` frontend. Carried from 0.15: message, tool and reasoning boxes, themes (`mocha`, `latte`, `gruvbox`, `ansi`, Textual's own, `$TAU_HOME/themes/*.json`), `tau.tcss`, dialogs, the session picker, the tree widgets. The sidebar is the cursor bar (hidden with one cursor and no label); the palette (Ctrl+P) has Theme, Tree, Sessions, New session and Quit; questions are inline boxes; `/config` and `/model` are forms and pickers. Not carried: panels, `SystemPromptEditor`, attachment chips, the read-only banner (D23 §7) |
| Typing while the model runs (steer / enqueue strategies, `TUI-STEERING.md`) | `steering.py`, `follow_ups.py` | **dropped** (changed) | D2: input is a scratchpad item delivered by the next step; "queueing is an extension's"; Retired terms: steering queue, follow-up queue. In the TUI Enter sends while busy, the line waits above the editor, and Up on an empty editor takes it back (D23 "Built: 8C-2") |
| Enter-key mode, editor widgets, slash-command completion in editor | `editor_widgets.py`, `ENTER-KEY.md` | **built** (changed) | Enter sends and Ctrl+J starts a line (no `enter_key` setting); `/` lists commands, Tab completes names and then each argument by what it is, from `Client.complete` and `commandline` (D16, D23 §4) |
| `@file` attachments (inline limit, TUI chips) | `attachments.py`, `docs/FILE-ATTACHMENTS.md` | **built** (8E-1, changed) | D24 §5: the built-in `attachments` extension, option `limit` (bytes); the `User` node holds the expanded text; `<attachment path>` and `<reference path size reason/>`; Tab completes an `@path` in the TUI; no images, chips or transcript fold |
| `-p` headless printing | `headless.py` | **built** (changed) | D22 (8B) |
| JSONL event stream (`--mode json`, deltas) | `JSON-MODE-DELTAS.md` | **built** (changed) | `tau -p --json` (D22): the wire's events, with `Live` deltas for the cursor `-p` sends to; one format with `tau serve` and the schema |
| REPL head (scrollback prompt loop) | `repl.py`, `repl_input.py`, `repl_theme.py` | **dropped** | see 2.8 |
| RPC (stdio JSON-RPC) | `tau_agent_core/rpc/`, `rpc_mode.py` | **built** (changed) / **post-0.16** | `tau serve` (D21): the `Client` contract as JSON-RPC over a Unix socket, passing the same behavior suite as a local client. Stdio is Tectum's, after 0.16.0 |
| `tau serve` multiplexer daemon, `RemoteClient` | `serve/` (8 files) | **built** (changed) | D21: one server per session, not a multiplexer; `RemoteClient` in `tau`; the schema is `tau serve --schema` |
| VS Code / tau-code head | `docs/VSCODE-HEAD.md` | **post-0.16** | plan.md "After 0.16.0" and D27: tau-code's protocol package is regenerated from `--schema`, its components carry over; it is not part of 0.16.0 |
| Ratatui example client | none | **built** (8D, new) | `examples/ratatui/`: a Rust client of `tau serve` (its own Cargo package), types generated from the schema; transcript, send, Esc, questions, `/` lines, tree browser, `/config` forms, reconnect after `CutOff` |
| Web client served by `tau serve` | `serve/http.py` | **post-0.16** | tau-code's web client, with TCP (D21 Open 4; plan.md "After 0.16.0") |
| Session picker | `session_picker.py` | **built** (8C-2) | see 2.3 |
| Compare view | `compare_view.py` | **post-0.16** | see 2.3 |
| Slash commands `/compact`, `/model`, `/name` | `capabilities.py` `FLOWS` | **built** | `/compact` (compaction), `/model` (`tau`), `/name` (`session_name`); `/usage`, `/prune` and `/config` are new, and each prompt template and skill is a command (2.5) |
| `/autocompact`, `/thinking` | `capabilities.py` `FLOWS` | **dropped** (changed) | `compaction.auto` option; provider `options` (2.1, 2.4) |
| `/resume`, `/fork`, `/tree` | `capabilities.py` `FLOWS` and views | **built** (changed) | Ctrl+O or `tau -r`; a branch is a go-to and a send (Ctrl+Z, the tree's Enter) and a copy is `--fork` (D23 §8); Ctrl+G or the palette. They are keys, not slash commands |
| `/enable_extension`, `/disable_extension`, `/reload_extension`, `/extensions` | `capabilities.py` `FLOWS` and views | **planned** (unspecified) | `tau ext list/enable/disable` exist on the command line. plan.md Phase 8 status names "extension and tool lists with load/unload written as config" as a TUI follow-up after 8D; it is not built (the palette has five entries). `/config` lists the extensions that have options |
| `/compare` | `capabilities.py` `FLOWS` | **post-0.16** | see 2.3 |
| Extension UI (panels, extension dialogs) | `extension_ui.py` | **built** (changed) | `Cursor.show` lines (the status bar prints one per extension), `Notice` toasts, questions and forms from JSON Schema (D16, D23 §8). Panels are **dropped** |
| Devshot / screenshot harness for TUI | `devshot.py`, `testing/` | **post-0.16** (dev tooling) | D23 §7: after 0.16.0. `tests/tui/test_snapshots.py` compares SVG snapshots in the suite |
| Textual headless test support | `docs/textual-headless-testing.md` | **built** | `tests/tui` drives the app through Textual's pilot over a `Harness` client, local and remote (D23 §9) |

### 2.10 Everything else

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Usage / token accounting in events | `usage.py`, `docs/TOKEN-ACCOUNTING.md` | **built** (8E-2) | `meta.usage {input, output, cached?, written?}` on each reply; the `usage` extension shows the status line and `/usage` (D24 section 3); `cached` is absent when a server does not report it |
| Cost / pricing (cache-write pricing, per-turn cost, TUI cost line, `test_cost.py`) | `docs/PRICING-AND-COMPACTION-CONFIG.md`, `app.py:_side_cost` | **built** (8E-2, changed) | `price` on a model; the status line shows the reply's cost and a session total; money is `Decimal`, worked out when you look; a model with no price shows no cost, never `$0` |
| Session stats (`get_session_stats`) | `capabilities.py` | **built** (changed) | `/usage`: a row per model (samples, input, cached share, cache writes, output, cost) and a total, summaries and sub-agents included. No message counts |
| Latency collection / run manifests (SIM_SPEC measurement runs) | `latency.py`, `run_manifest.py` | **REGRESSION** (research tooling) | `tau.trace` + `docs/benchmarks.md` cover tracing, not these artifacts |
| Telemetry strip (llama.cpp timings) | `examples/70_telemetry.py` | **post-0.16** (minor) | see 2.1 |
| Tracing | none | **built** (new) | `tau.trace`, Chrome trace across Rust and Python |
| Agent docs generator (`@agent_facing`, `docs/library/reference`) | `docs.py`, `docs_build.py` | **built** (changed) | Phase 7C: `docs/api/groups.toml` says what is public; `scripts/api_docs.py` generates `docs/api/surface.txt` and `docs/reference/` from docstrings, and CI fails when they are stale; guides in `docs/guide/`. A docs site is Phase 9 |
| Packaging: five distributions, `ffwf-tau` meta-package, extras (`[tui]`, `[repl]`, `[jmfts]`, `[bus]`) | `tau-*/pyproject.toml`, `tau-meta` | **built** (changed) | one `ffwf-tau` wheel (cp312-abi3 + cp314t), import `tau` (D10), no extras: Textual is a dependency, and `tau -p` never loads it. D26 plans a second distribution, `ffwf-tau-tokens` (T4, not built) |
| The `ffwf-tau` command next to `tau` | `README.md` "Two names for the command" | **dropped** | `pyproject.toml` installs the one script `tau` |
| Wheels: Linux, macOS, Windows, py3.11-3.14 | `package.sh`, `docs/RELEASING.md` | **planned** | Phase 9: only Linux x86_64 today; Python floor is now 3.12 (0.15: 3.11); base tools Linux-only. macOS and `powershell` tool deferred (plan.md) |
| Docker image / entrypoint | `Dockerfile`, `docker/` | **post-0.16** (packaging) | plan.md "After 0.16.0 ... Later" |
| Free-threaded (3.14t) support | `experiments/freethreading` | **built** | `gil_used = false`, tested in `tests/core/test_threads.py` |
| JMFTS integration: store backend, enrichment extension, importer, catalog, `jmfts` tools | `tau-jmfts/` (5 + 4 files, 109 tests) | **post-0.16** | plan.md Phase 9: "The JMFTS replica comes after 0.16.0"; D12: JMFTS is a replica fed from the local log |
| Extension examples | `examples/` (40 numbered) | **built** (reduced) | `0.16: examples/`: 10 Python programs (`first_call.py`, `first_tool.py`, `chat.py`, `tour.py`, `retry.py`, ...), `config.json`, `ratatui/`, and `speculative/` with 7 prototypes |
| Tectum integration contract (`--no-tools`, `PI-RPC-REPLACEMENT`, WIRE-CONTRACT) | `docs/TECTUM-NO-TOOLS-MIGRATION.md` | **post-0.16** (external) | plan.md "After 0.16.0": the same protocol over stdio, and `--no-tools`, in one pass with the JMFTS replica |
| 0.15 hotfix branch | n/a | **built** | branch `0.15.x` (plan.md Phase 6) |

## 3. Tally

Counted by the leading status token of each table row in section 2 (a few rows carry two statuses; each
counted once under its first).

| status | rows | of which |
|---|---|---|
| built | 133 | includes "built (changed)" and "built (new)" rows (0.16-only: trust, `config check`, `config set`, `.env`, tracing, permissions, prune, the ratatui client) |
| planned | 5 | 2 are "planned (unspecified)": `summarize_and_navigate`, and the extension and tool lists in the TUI |
| dropped by design | 33 | |
| REGRESSION | 4 | all four listed in section 4 |
| post-0.16 | 33 | JMFTS (5 rows), the tool-list flags, Tectum and stdio RPC, tau-code and the web client, and the extension-shaped "Later" list (plan.md "After 0.16.0") |
| **total** | **208** | |

## 4. What is not built and not placed, before release

Rows marked **REGRESSION** in section 2, by impact on a coding agent session:

1. **Prompt cache dialect** for an Anthropic model behind an OpenAI-compatible gateway (2.1): that route is billed without caching unless the request body is built by hand in `extra_body`.
2. **A model's own compaction settings** (2.4): one setting set for the session, a branch, or your config, not per model.
3. **Budget caps** (2.5): cost is shown (2.10) but nothing stops a run at a spend.
4. **Latency collection and run manifests** (2.10): research tooling.

Marked **planned** with no release placed: `summarize_and_navigate` (2.3), the extension and tool lists in the TUI (2.9), the tokenizer and calibrated counter stages of D26 (2.1), a sequential tool mode (2.2), `cache_state` and automatic pruning (2.1, 2.4).

Closed since the first pass: `edit` and `find` (Stage 8A; `ls` is dropped for `bash`), the system prompt and context files (D20), headless approval (`--answer`), `--version` and `--verbose` (8B), `--fork` and Markdown export (D18), extension discovery and `tau ext` (D19), `tau serve` (D21), the terminal UI with its tree and session picker (8C), the ratatui client (8D), and every standard extension: retry, `@file`, skills and prompt templates, usage with cost and the cache notice, compaction with `/prune`, permissions and sub-agents (8E), and reported-count token counting (D26 T1).

After 0.16.0 (named in plan.md "After 0.16.0", extension-shaped or tooling unless noted): image input, constrained decoding, the models.dev catalog, background tools, code mode, the NATS bus, HTML export, the llama.cpp telemetry strip, `serve --tail`, the Docker image, the TUI screenshot harness, elide / commit-branch / paste-subtree / compare (commands, extensions or dropped), the tool-list flags (Tectum's `--no-tools`), stdio RPC, the web client with TCP, tau-code, and the JMFTS replica (all JMFTS rows).

Dropped on purpose: the REPL, `ls`, `--fun`, steering and enqueue, the suspension step debugger, extension locks, annotations, the 0.15 extension API and its lifecycle hooks, the authorization store, unified `--thinking` levels, tolerant JSON repair, the `ffwf-tau` command alias.

Accepted by design (not regressions, but behavior changes users will feel): implicit per-vendor API key discovery (a model names its variable), unified `--thinking` levels, steering/enqueue, the suspension step-debugger, 0.15 session files (no converter), 0.15 extensions (no shim), a project's extensions and prompt files loading only after `tau trust`, a discovered extension that fails to load stopping the start, `tau serve` as one server per session on a Unix socket (no port, no token), JMFTS (post-0.16). Retry, compaction, usage and `@file` are built in and on; sub-agents are built in and off until `tau ext enable agents`.

## 5. Sources read

0.15: `README.md`, `CLAUDE.md`, `docs/INDEX.md`, `tau-coding-agent/src/tau_coding_agent/{cli,ext_cli,serve/cli}.py`,
`tau-agent-core/src/tau_agent_core/{capabilities,session_log,settings,tools/*}.py`, `tau-llm/src/tau_llm/*`.
0.16: `docs/plan.md`, `docs/plan-phase5-fixes.md`, `docs/design/{model,glossary}.md`, decisions D3, D8, D13,
D15, D16 (first pass) and D18 to D24, D26, D27 (refresh), `docs/guide/*`, `tau --help` and each subcommand's `--help`,
`python/tau/{cli,__init__,_extensions,_config_layers,providers/*}.py`, `pyproject.toml`, `examples/`, `tests/`, `crates/*`.
Counts are `grep` over test function definitions, not collected-test counts, so parametrized tests are 1 each.
