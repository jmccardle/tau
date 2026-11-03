# τ 0.15.0 -> 0.16.0 "Trusty": capability ledger

First pass 2026-10-31: a reader's survey of both trees. Statuses are as of that day. As decisions land,
`docs/plan.md` overrides this file, and its "Phases 7–9" section places each REGRESSION group.

Compared: 0.15 = tag `v0.15.0` (paths below are relative to it; `git worktree add /tmp/tau-0.15 v0.15.0`).
0.16 = this repository (`python/tau`, `crates/`, `tests/`, `docs/`; its paths are written with a
leading `0.16:`). Design cites: `model.md`, `glossary.md` = `docs/design/`; `Dn` = `docs/design/decisions/`;
`plan.md`, `fixes` = `docs/plan.md`, `docs/plan-phase5-fixes.md`.

Status vocabulary: **built** | **planned** (phase/stage cited) | **dropped** (by design, cited) |
**REGRESSION** (not built, not planned anywhere; "deferred" notes where a doc admits it but gives no phase) |
**post-0.16**. A row marked `(unspecified)` is planned only as a heading (e.g. "Phase 8 frontends") with
no statement that this capability is in it; those are the ones most worth confirming.

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
| 0.16 `tests/behavior` | `def test_` (19 files) | 297 |
| 0.16 `tests/providers` | 16 files | 161 |
| 0.16 `tests/examples` | 9 files (speculative extensions 87 of these, plus chat, retry) | 109 |
| 0.16 `tests/config` | 3 files | 96 |
| 0.16 `tests/tools` | 6 files | 55 |
| 0.16 `tests/cli` | 2 files | 53 |
| 0.16 `tests/core` | 3 files | 46 |
| **0.16 Python test functions, total** | 58 files, 59 `parametrize` uses | **817** (suite: 1,052 passed + 4 skipped) |
| 0.16 Rust `#[test]` | tau-core 78, tau-tools 90, tau-store 18 (tau-py, bench-formats 0) | 186 |
| 0.16 Rust `proptest!` blocks | 4 properties inside the `#[test]` count above (`invariants.rs` x2, `replay.rs`, `store.rs`); one block, `invariants_hold_under_any_driver`, runs 512 cases | 4 blocks |
| **0.16 total** | 817 Python + 186 Rust | **1,003 test functions** |

Reading it: 0.16 has about 15% of 0.15's test functions (about 16% by the passed-count). Where 0.15's tests
lived (filename heuristic, overlapping): coding-agent: TUI/app/chat/dialog 541, CLI/print/headless 237, REPL 154,
serve/connect 93, extension commands 348; agent-core: extensions/hooks/flows/locks 761, rpc/capabilities 544,
session/log/catalog 449, tools 305, compaction 213, tree/surgery/cursor 167, suspension/step-debugger 217,
bus/background 127, skills/templates/attachments 97, codemode 44. Almost none of those areas has a 0.16
counterpart yet, which matches section 2: the suite shrank because the surface did.

## 2. Capability ledger

### 2.1 Providers (`tau-llm` -> `python/tau/providers`)

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| OpenAI-compatible `/chat/completions` (llama.cpp, vLLM, OpenAI, gateways) | `tau-llm/.../providers/openai.py` (2,111 lines) | **built** | `0.16: python/tau/_providers/openai.py` (435 lines), streams parts + deltas, `usage` normalized, `usage_raw` kept; live-tested against 192.168.1.100:8100 |
| Anthropic Messages API | `providers/anthropic.py` | **built** | `0.16: _providers/anthropic.py`, `_anthropic_request.py`; thinking replay data, signatures |
| Google Gemini | `providers/google.py` | **built** | `0.16: _providers/gemini.py`; Gemini 3 thought signatures; no fallback signature when switching providers mid tool loop (D15) |
| Provider registry: `register_api` / `register_provider` / `ProviderSpec` (add a vendor in 6 lines) | `providers/base.py`, `providers/__init__.py` | **built** (changed) | Vendors are config groups (`base_url`, `api_key_env`, `provider`) per D13; extra providers are objects passed to `Driver` (D15). `tau config check` knows only the built-ins (D15 "Deferred") |
| Prompt cache marks (Anthropic `cache_control`) | `docs/PROMPT-CACHING.md`, `providers/anthropic.py` | **built** | `0.16: _anthropic_request.py:197`; `options.prompt_caching` default on. `cache_state(walk)` for the provider is **planned** (plan.md Phase 5 paragraph) |
| Cache-miss notice / `prompt_cache.py` ("your cache is not being read") | `tau_agent_core/prompt_cache.py`, `test_cache_miss_notice.py` | **REGRESSION** | `usage.cached` is recorded; nothing reads it and warns. Natural fit for a frontend or extension; not listed |
| Auth discovery: per-vendor env defaults (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`), literal `api_key`, credential hints | `providers/__init__.py:147-165`, `docs/PROVIDER-CREDENTIALS.md` | **dropped** | D13/glossary "api_key_env": a model must name its variable or give a literal; `ModelConfig.api_key` (`_config.py:116`). Hosted providers fail `check` without a key. Deliberate Fail Early, but a 0.15 config with no `api_key_env` will not work |
| Project `.env` as key source | none | **built** (new) | `0.16: _dotenv.py`, trust-hashed (D13) |
| Provider retry (408/429/5xx/529, 3 tries, `Retry-After`) | `tau_llm/retry.py`, `docs/PROVIDER-RETRY.md` | **dropped** | D15 D3: "a cursor never spins"; ships only as `examples/retry.py` (`RetryProvider`). Default behavior regression for users, accepted by design |
| HTTP errors typed (`ProviderHttpError` with status, body, headers) | `tau_llm/errors.py` | **built** | `SampleError(message, meta, status, retry_after)`; error recorded in the reply `meta.error` (glossary `finish_reason`/meta) |
| Reasoning / thinking levels (`--thinking`, `thinking_level_map`, minimal..xhigh) | `tau_llm/models.py`, `docs/REASONING-WIRE.md` | **dropped** (changed) | No unified level. Per provider `options` (`thinking`, `reasoning_effort`) via config and `/config` (D15). No `thinking_level_map` |
| Image input (read tool images, `@image`, resize) | `tools/image_resize.py`, `tools/read.py` | **REGRESSION** | D15 "Deferred": "Images (not needed for a text-only port)"; `model.md:69` shows `User(text, images*)` but no phase builds it |
| Constrained decoding / llguidance grammar channel (`DecodeConstraints`) | `tau_llm/constraints.py`, `grammar.py` | **REGRESSION** | Only reachable by hand via `options.extra_body`. No decision records dropping it |
| models.dev catalog (`python -m tau_llm.catalog providers/search/show/config`) | `tau_llm/catalog.py` | **REGRESSION** | Config-entry generator; not listed anywhere. Could be a `tau config` subcommand |
| Per-endpoint wire quirks `Compat` (`max_tokens` vs `max_completion_tokens`, stream_options) | `tau_llm/compat.py` | **dropped** (changed) | `options.extra_body` (`examples/config.json`), `include_usage` option (D15) |
| Token counting (tokenizers, calibrated char-class counter) | `tau_llm/tokens.py` (640 lines) | **REGRESSION** | Needed by compaction and context margin. Compaction is planned (Phase 8) but no doc names a token counter; D15 defers "usage fields beyond input, output, cached" |
| Tolerant JSON for streamed tool-call args (`json_parse`), repair count | `tau_llm/json_parse.py` | **dropped** (changed) | Calls keep `args_raw` byte for byte; bad args come back to the model as an `Err` (`test_scenarios.py` "bad args"); no repair, no repair count |
| llama.cpp `timings` readout / telemetry strip | `examples/70_telemetry.py`, `chat_widgets.format_telemetry` | **REGRESSION** | `usage_raw` is kept; `timings` is not folded onto `meta`. Minor |
| Provider lifetime/pool, `aclose()` | `docs/PROVIDER-LIFETIME.md` | **built** | Long-lived provider objects, caller closes (D15, `builtin_providers`) |
| Mock provider | `tau_agent_core/testing`, tests | **built** | `0.16: _providers/mock.py`, `FakeProvider` in `tests/behavior` |
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
| Output limit 10 kB + overflow file | `tool_output.py`, `docs/` | **built** (changed) | `max_output`, overflow file `<id>.out` in `/tmp/tau-<uid>` (0600), 64 MiB cap; pruning deferred (plan.md "Deferred 2026-10-29") |
| Tool factories (`create_coding_tools`, `create_read_only_tools`) | `tools/__init__.py` | **built** (changed) | `base_tools(cwd)` returns all six; the shared start gets them from the built-in extension `base_tools` (off with `load`); no read-only preset |
| Function tools (`@tool`, schema from signature) | `tool_schema.py`, `tools/base.py` | **built** | `0.16: _functions.py`, `Annotated[T,"text"]` descriptions (D16 section 1) |
| Parallel tool calls; sequential mode (`tool_execution_mode`) | `settings.py:48` | **planned** | Calls run in parallel; Q21 `exclusive` tool "not built yet" (`model.md:565`) |
| Tool allow/deny lists: `--tools`, `--no-tools`, `--exclude-tools`, `--no-builtin-tools`, `tool_exposure`, `tool_declaration` | `cli.py`, `tool_catalog.py`, `session_log.CONFIG_KEYS` | **planned** (unspecified) | Library side: fixes D2 (`pre_sample` hook sets `subject.tools`; Stage D) and the program passes `tools=` to `Driver`. No CLI flag exists (see 2.8) |
| Background tools (`background.py` built-in extension) | `extensions_builtin/background.py`, `docs/BACKGROUND-TOOLS.md` | **REGRESSION** | Extension-shaped, not listed |
| Code mode (Python script calling every tool in a sandbox) | `tau_agent_core/codemode/` (10 files, 44 tests) | **REGRESSION** | Experimental in 0.15; not listed. Extension-shaped |
| Tool progress events | `tool_pipeline.py` | **built** | `ToolProgress`, `Progress` (D15) |
| Truncated tool-call detection (`Length` stop) | `truncation.py`, `docs/TRUNCATED-TOOL-CALLS.md` | **built** (changed) | `Stop::Length` is an ordinary reply; `Wait` (model.md:428) |

### 2.3 Session store and tree operations

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Durable session log (JSONL `SessionLog`) | `session_log.py`, `session_manager.py` | **built** (changed) | `0.16: crates/tau-store`: length-prefixed checksummed record log + scratchpad file (D12); torn tails cut; I7 proptest |
| 0.15 session files readable | `~/.tau/sessions/*.jsonl` | **dropped** | D9 (superseded): no converter; 0.16 ignores 0.15 files; `~/.tau-0.15` archive. Releases notes owed (plan.md Phase 9) |
| Per-session write lock | `session_lock.py` (flock) | **built** | the lease (`Leased` error) |
| Tree of entries, branches, leaf | `conversation_tree.py` | **built** (changed) | `0.16: crates/tau-core/src/tree`: nodes, `walk`, `children`, `lca`, labels, `config_at` |
| Cursors / multi-writer (`Cursor`, sub-agent cursors) | `cursor.py`, `docs/CURSORS.md` | **built** (changed) | cursors `{id, at}`, `open`, `go_to`, scratchpad items wake cursors (D6, model.md) |
| `navigate` / go to node | `tree_ops.py` | **built** | `go_to` (glossary "go to") |
| Branch from any node, in-session fork | `ctx.fork`, `tree_ops.fork` | **built** (renamed) | "branch" (glossary; rule 3 bans in-session "fork") |
| Re-run a tool batch | `Session.rerun` | **built** | "re-run" (glossary): go to the `Assistant`, step, sibling `Results` |
| `--fork REF` / copy a session into a new one | `cli.py:--fork`, `session_manager` | **built** (changed) | D18: `Session.fork(node, now, directory)` copies one walk in one call; `--fork REF[:NODE]` is in `session_flags`, provenance is `forked_from` in `info.json` (not `copied_from`, which stays same-session); the `tau` program that takes the flag is Phase 8 |
| `summarize_and_navigate` (branch summary on leaving a branch) | `tree_ops.py`, `capabilities.py:1991` | **planned** (unspecified) | D3 compaction extension builds "a summary node under a kept prefix"; branch summaries are not named |
| `elide_span` (fold a span out of context) | `tree_surgery.py`, `tree_ops.py` | **planned** (unspecified) | plan.md Phase 8: tau-code "elide ... become commands or extensions, or are dropped" |
| `commit_branch`, `paste_subtree` | `tree_surgery.py` | **planned** (unspecified) | same Phase 8 sentence; glossary "branch" retires `commit_branch` |
| Tree browser TUI (edit the tree) | `tree_browser.py`, `docs/TREE-BROWSER-AS-EDITOR.md` | **REGRESSION** | Phase 8 widget reuse list names "message, tool and reasoning boxes, stream strip, themes, dialogs, session picker"; not the tree browser |
| Model compare (N cursors, one prompt, keep one) | `compare.py`, `compare_view.py`, `/compare` | **planned** (unspecified) | Phase 8 tau-code sentence (above) lists "compare"; ADR D17 mentions it; no design |
| Seeded branches / spawn (`spawn_branch`, `seed`) | `seed.py`, `docs/SEEDED-BRANCHES.md` | **planned** | Stage B1-B3 (sub-agent primitive: authored input, `tree.rest`, `tree.abort`), sub-agent extension Phase 8 |
| Session listing (`list_sessions`) | `session_catalog.py` | **built** (changed) | D18: `tau.list_sessions`, `tau sessions [--all] [--json]`: a scan of `$TAU_HOME/sessions`, no index; message count and last message are **dropped** (they need a full read of the log), the title is the name, else the first message |
| Session picker (`--resume`) | `session_picker.py` | **planned** | `-r` is in `session_flags` and the start takes the picker as a callable (`pick_session`); the widget is Phase 8 |
| Session naming (`--name`, `/name`) | `session_log.py` | **built** | D18: `-n NAME`, `tau sessions name`, `/name` from the built-in `session_name`; the name is in `info.json`. Auto-naming (`examples/42_session_autoname.py`) is **dropped**: the fallback title is the first message |
| Session export: Markdown (`export.py`) | `export.py` | **built** (changed) | D18: `tau export REF [-o FILE] [--thinking]`, `tau.to_markdown`; deterministic, one walk (`REF:NODE`), thinking only on request |
| Session export: HTML (`--export-html`) | `export.py`, `docs/HTML-EXPORT.md` | **post-0.16** | D18: not built |
| 0.15 session files (`sessions/<dashed-cwd>/<time>_<uuid>.jsonl`), the read-only second view of a held session | `session_store.py` | **dropped** (changed) | D9, D18: a directory per session under `$TAU_HOME/sessions`; a held session is `Leased` (fork it, or read it with `tau export`) |
| Session import/export JSONL (JMFTS) | `cli.py:--import-session/--export-session` | **post-0.16** | tied to JMFTS (below) |
| `--no-session` ephemeral | `cli.py` | **built** | `Session.create` with no directory is in memory (`examples/one_off.py`); `--no-session` is in `session_flags` and `start_session` |
| Message timestamps | `docs/MESSAGE-TIMESTAMPS.md` | **built** | `Node.time` |
| Crash behavior (partial reply saved, finalize) | `docs/TURN-FAILURES.md` | **built** (changed) | D4: rest at the batch, no repair; `Interrupted` replies kept |
| Transcript window (render cap for long sessions) | `docs/TRANSCRIPT-WINDOW.md` | **planned** | D14 "deferred": every body stays in memory; load-a-subset left for later; TUI is Phase 8 |
| Entry annotations (`annotate` hook writes `ext` JSON on an entry) | `entry_annotations.py` | **dropped** | glossary Retired terms: `annotate` -> event observers; D8 |
| Custom entries / extension messages in the tree | `docs/EXTENSION-MESSAGES.md` | **built** (changed) | `Body::Custom` nodes never reach the model (I1) |

### 2.4 Compaction and context management

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Auto-compaction (on by default for CLI heads), `/compact`, `/autocompact` | `compaction.py`, `compaction_policy.py`, `config.py:parse_compaction_config` | **planned** | D3: an extension (new line + `pre_sample` move); Phase 5 list, re-scoped to Phase 8 standard extensions (plan.md). Not built; "first command" per Phase 5 intro |
| Cold summary from a cheaper model | `compaction.py` | **planned** | D3 "work on a detached root" |
| Compaction entry + splice in `context_for` | `session_log.py` | **dropped** | D3, glossary Retired terms |
| System prompt kept across compaction/elide | `docs/SYSTEM-PROMPT-IN-THE-FOLD.md` | **built** (by construction) | `walk` is a plain parent chain (I1) |
| Context margin / window sizing (`context_margin`, `context_window`) | `settings.py:28`, `docs/SILENT-DEFAULTS.md` | **REGRESSION** | `ModelConfig` has no `context_window`; the compaction ADR does not say where thresholds come from |
| Prune (D16 names it with compaction as "the first commands") | no 0.15 counterpart found | **planned** (new) | plan.md Phase 5 list "prune" -> Phase 8 standard extensions; no definition in the docs |
| `StepLimit` / `--max-turns` | `cli.py:--max-turns`, `config max_turns` | **built** (changed) | `tau.StepLimit` extension, `extensions.step_limit.limit` (D1); no CLI flag |
| Follow-up / queued messages dropped notices | `follow_ups.py` | **dropped** | D2: queueing is an extension's |

### 2.5 Extensions, hooks, skills, prompt templates

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Extension API (`ExtensionAPI`, 42 methods, runner, registry) | `extensions/` | **dropped** (rebuilt) | D8: no compatibility; hooks `pre_input`, `pre_sample`, `pre_reply`, `post_reply`, `pre_tool`, `pre_result`, `post_result`, `post_step`, `pre_step` (model.md section 5, glossary Hooks). Mapping table in D8 and Retired terms |
| Lifecycle hooks (`session_start`, shutdown, `turn_end` ...) | `extension_types.py` | **dropped** | D8: "No lifecycle hooks until a Phase 5 extension needs one (Q23)" |
| Hook error policy / handler policies / deadlines | `policies.py`, `docs/HOOK-ERRORS.md` | **dropped** | Q9; `HookFailure` in `CursorView` (D16) |
| Extension commands (`/name`), flows (forms, completion) | `flows.py`, `capabilities.py` | **built** | `@command` functions, `Form`, `commandline`, `Client.run/commands/complete` (D16 stages 1-3) |
| Command namespacing (`/ext:cmd`) | `docs/EXTENSION-NAMESPACE.md` | **REGRESSION** | D16: unknown `/name` is text; extension names are unique but no qualified-command form is documented |
| Extension config (`--ext-config NAME:KEY=VALUE`, `config.json extensions`) | `cli.py`, `extension_auth.py` | **built** (library) | `options` dataclass, `extensions.<name>` in layers, `/config`, `Cursor.set_options`; CLI flag **planned** (unspecified) |
| Extension locks (stop a session, ask the attached user) | `extension_locks.py` | **dropped** | glossary: suspension/responder -> auto-step off + `question` (D16) |
| Step debugger: `ctx.suspend`, suspension UI, `80_step_mode.py` | `suspension.py`, `suspensions.py`, `suspension_ui.py` | **dropped** (rebuilt smaller) | Retired terms "suspension, responder -> auto-step off; a question". Phase 5 "debug frontend (auto-step off)" **planned** |
| Permission gate | `examples/01_permission_gate.py`, `30_permission_gate.py` (examples only; no built-in) | **planned** | Phase 5/8 permissions extension (`pre_tool` asks); prototype `examples/speculative/bash_guard.py` |
| Sub-agents / delegate / swarm | `examples/20_delegate.py`, `50_review_swarm.py`, `51_delegate_fleet.py` | **planned** | Stage B + Phase 8 sub-agent extension; prototypes `forest.py`, `gamemaster.py` |
| Extension discovery: drop-ins `~/.tau/extensions`, installed entry points, `-e PATH|MODULE` | `extension_sources.py`, `docs/EXTENSION-PACKAGES.md` | **planned** (unspecified) | Only `examples/chat.py --extension PATH` (fixes C3). "Trusted loader for `tau` (hash-pinned) stays with the trust work carried from Phase 4" (fixes C3, plan.md:229) has no phase. `tau` itself cannot load an extension today |
| Extension authorization store (`tau ext authorize/revoke/enable/disable/list`) | `extension_auth.py`, `ext_cli.py` | **REGRESSION** | Not in 0.16 CLI (`config check`, `trust` only); successor is the unscheduled trusted loader above |
| Hot reload of extensions | `reload_extension` capability | **planned** | fixes C2 `driver.reload(extensions)` |
| Skills (`SKILL.md` dirs, `--skill`, `--no-skills`) | `skills.py`, `resources.py` | **REGRESSION** | No glossary entry, no phase. (Only mentioned as an analogy in D17 and fixes ruling 3.) Extension-shaped |
| Prompt templates (`/name args` from `~/.tau/prompts`, `.tau/prompts`) | `prompt_templates.py` | **REGRESSION** | Same: not built, not planned |
| Context files (AGENTS.md, CLAUDE.md, `.tau/SYSTEM.md`) into the system prompt | `backends.py`, `cli.py:--no-context-files` | **built** | D20: `tau.system_prompt`, once at creation, recorded as the `System` node; names are `AGENTS.md`, `CLAUDE.md` only; `tau context` shows the parts |
| `ext:` bus / request-reply between extensions (`api.request`) | `docs/EXT-REQUEST.md` | **dropped** | Not in glossary; handles are the cross-extension surface (`TreeHandle`) |
| NATS bus extension, `--bus` | `extensions_builtin/nats_bus.py` | **REGRESSION** | Not planned (extension-shaped) |
| Budget / cost ledger examples (`24_budget.py`, `43_budget_ledger.py`) | `examples/` | **REGRESSION** | 0.15 had 40 numbered examples; 0.16 ships 11 plus 7 speculative |
| SDK entry `create_agent_session()` | `sdk.py` | **built** (changed) | `Driver`, `LocalClient`; API doc and manifest **planned** Phase 7 |

### 2.6 Permissions

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Built-in permission system | none; example extensions only | **planned** | Phase 8 standard extension; `ask` is built (D16, `_questions.py`) |
| Protected paths / sandbox | `examples/31_protected_paths.py` | **planned** (unspecified) | falls under permissions extension if it covers paths; today bash_guard only matches commands |
| Headless approval (`--ui-defaults confirm=yes,...`) | `cli.py:--ui-defaults`, `headless.py` | **built** (changed) | D22: `tau -p --answer EXTENSION[:TEXT]=VALUE`. Rules match who asked and what, not a dialog kind; a question no rule matches is declined (stderr says so), and a value the question's schema refuses aborts the run (exit 1). D16 ruling 1 holds: `-p` is the client that answers |

### 2.7 Config and trust

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| `~/.tau/config.json` models (`backend`, `model`, `base_url`, `api_key`) | `config.py`, `tau_default_config.json` | **built** (changed) | D13: `provider` replaces `backend`; groups, qualified names `group:name`, layered merge |
| Project `.tau/config.json` | `setting_sources.py` | **built** | gated by trust (D13) |
| `TAU_HOME`, portable home | `tau_home.py`, `docs/PORTABLE-HOME.md` | **built** | `0.16: _trust.tau_home` |
| Project trust gate (`TRUST-GATE.md`, research only in 0.15) | none shipped | **built** (new) | `tau trust`, `trusted.json` with content hashes, `.env` hashed, symlink/owner/700 checks |
| `tau config check [--show]` | none | **built** (new) | `0.16: cli.py` |
| Config writes by the program (`save_config`, `update_config`, `/model --save`) | `config.py:82-115` | **planned** | fixes D1: `tau config set`, `/config --save`, `cursor.save_options` |
| Model unavailable recorded on session (`model_availability`) | `model_availability.py` | **built** (changed) | model named by a `Config` node, resolved at sample; unknown name is a failed sample (`ConfigError`) |
| Defaults: `default_model` | `config.py` | **built** | `select_model` / `load_config` (`_startup.py`) |
| `steering_strategy`, `enter_key`, `attachment_inline_limit`, `theme` keys | `tau_default_config.json` | **dropped** / **planned** | steering dropped (D2); `theme` is built (8C-1): the top-level `tui` key, `{"tui": {"theme": ...}}` (D13 amended); `enter_key` is dropped (Enter sends, Ctrl+J starts a line) |
| `session_store.backend` (`file` / `jmfts`) | `store_factory.py` | **post-0.16** | JMFTS |
| Configurable `max_tokens`, `temperature` | `settings.py` | **built** | `max_tokens` typed in `ModelConfig`; `temperature` via provider `options` (D15) |

### 2.8 CLI: every 0.15 flag and subcommand (`tau --help`, `tau_coding_agent/cli.py:132-470`)

0.16's CLI is `tau -p`, `tau --version` and the subcommands `config`, `ext`, `sessions`, `export`, `context`, `trust` and `serve` (`0.16: python/tau/cli.py`). The first argument decides: a subcommand's name runs it, anything else is the launch command line, so `tau -p sessions` sends the word "sessions". `tau` alone prints help until the terminal UI (8C). D22 §9 is the table of what became of each flag. Statuses below are about the capability.

| 0.15 flag / subcommand | 0.16 status | note |
|---|---|---|
| `tau` (no args) -> Textual TUI | **built** (8C-1; cursor bar, tree, picker follow) | D23: `tau` opens `tau.tui.TauApp` over `open_client`; without a terminal it is a usage error |
| `MESSAGES...` with `@file` references | **built** (changed) / **REGRESSION** for `@file` | D22: the words are joined with spaces (0.15: newlines), then standard input after a blank line when it is not a terminal. `@file` waits for the 8E extension (2.9); `-p` sends text as typed |
| `-p`, `--print` | **built** (changed) | D22: a switch, not a flag that takes the message. Prints only the last reply on stdout; exit 0/1/2/3/130 (3, stopped before finishing, is new); `-p` attaches to the `tau serve` that holds the session |
| `--mode text` / `json` | **built** (changed) | `text` is the default and the flag is dropped; `json` is `--json`: the wire's own events, one object per line after a `Welcome` line (not 0.15's bus events) |
| `--mode rpc` (JSON-RPC 2.0 over stdio, ~45 verbs, Tectum contract) | **built** (changed) | replaced by `tau serve` JSON-RPC over WebSocket (D21); stdio is Tectum's, after 0.16.0 |
| `--mode repl` | **REGRESSION** | Phase 8 lists TUI, `-p`, ratatui example, tau-code; no REPL. 154 0.15 tests |
| `-v`, `--version` | **built** | D22: prints `tau VERSION (protocol N)`, the τ version and the wire protocol a client must speak; a flag of the launch command line only |
| `-m`, `--model` | **built** | D22: the model of a new session (D18: an existing session records its own; `/model` switches a running one) |
| `--provider` | **dropped** | the model's `provider` key in config (D22 §9) |
| `--theme` | **built** (8C-1) | D23: for one run; a usage error with `-p` |
| `-t`, `--tools` | **deferred** | tools come from extensions (`load`, `-e`); a per-run tool list is Tectum's `--no-tools`, after 0.16.0 (D22 §9) |
| `-nt`, `--no-tools` | **deferred** | same |
| `-xt`, `--exclude-tools` | **deferred** | same |
| `-nbt`, `--no-builtin-tools` | **deferred** | `load: {base_tools: false}` (`tau ext disable base_tools`) turns the base tools off; `--no-extensions` does not |
| `-e`, `--extension PATH|KEY` | **built** (changed) | D19: a path, or the key of a source; naming it is the consent. `tau -p` and `tau serve` take it; not for a session a server holds (D22 §8) |
| `--no-extensions` | **built** (changed); `-ne` **dropped** | D19 has the long form only (D22 §9): loads no package or drop-in; the built-ins (`base_tools`, `step_limit`) follow `load` |
| `--bus` | **REGRESSION** | NATS extension not planned |
| `--no-session` | **built** | D18: `session_flags`, `start_session` |
| `--max-turns N` | **dropped** (changed) | `StepLimit`'s option; for one run `--set step_limit.limit=N` (D22). A run that stops there exits 3 and says so (0.15 stopped silently and exited 0) |
| `--ext-config NAME:KEY=VALUE` | **built** (changed) | `--set EXTENSION.FIELD=VALUE` (D22), repeatable: the CLI layer of D13, read and checked as `tau config set` does, for one run; `-p`, `tau` and `tau serve` take it |
| `--ui-defaults METHOD=ANSWER,...` | **built** (changed) | `--answer EXTENSION[:TEXT]=VALUE` (D22); see 2.6 |
| `--append-system-prompt TEXT` | **built** | D20: `session_flags`; an error with `-c`, `-r`, `--session`, `--fork` (0.15 ignored it) |
| `--system-prompt TEXT` | **built** | D20: the CLI layer of the `system_prompt` config key; no `{{fields}}` |
| `-nc`, `--no-context-files` | **built** | D20: `session_flags`; accepted when continuing or forking (nothing is read then) |
| `--prompt-template PATH`, `--no-prompt-templates` | **REGRESSION** | see 2.5 |
| `--skill PATH`, `--no-skills` | **REGRESSION** | see 2.5 |
| `-c`, `--continue` | **built** | D18: the newest session of the working directory; `tau -p` takes it (0.15: `-p` only), and attaches if a server holds the session |
| `-r`, `--resume` | **planned** | D18: parsed and resolved by the start; the picker is Phase 8. A usage error with `-p` (exit 2): name the session with `--session REF` or `-c` |
| `--session REF` | **built** | D18: a path, an id, a name or a unique id prefix, searched over the whole sessions directory |
| `--fork REF` | **built** (changed) | D18: `REF[:NODE]`; see 2.3 |
| `-n`, `--name` | **built** | D18 |
| `--thinking LEVEL` | **dropped** (changed) | provider `options` (2.1) |
| `--store file|jmfts` | **post-0.16** | JMFTS |
| `--connect ADDR`, `--cwd PATH` | **dropped** (changed) | attaching is automatic: `tau -p --session NAME` connects to the `tau serve` that holds the session (D22 §8); `cd` sets the directory |
| `--session-dir DIR` | **built** | D18: a flag only; `TAU_HOME` already moves everything |
| `--import-session PATH` / `--export-session REF PATH` | **post-0.16** | JMFTS |
| `--export-html REF [OUT]` | **post-0.16** | see 2.3; Markdown is `tau export` |
| `--verbose` | **built** (changed) | D22: on stderr, a report of the start (session, directory, model, config files, extensions, system prompt parts) and under `-p` a line per node, step ending and question; not an arguments dump or a debug log (`tau.trace` is the profiler). `tau serve` takes it too |
| `--fun` / `--no-fun` (random tagline) | **REGRESSION** (cosmetic) | |
| `tau serve` (default 127.0.0.1) | **planned** | Phase 7 |
| `tau serve --listen ADDR` | **planned** (unspecified) | Phase 7 |
| `tau serve -d` / `--daemon` | **planned** | "serve -d --json" named in Phase 7 |
| `tau serve --json` | **planned** | same |
| `tau serve --stop` | **planned** (unspecified) | |
| `tau serve --tail [SESSION]` | **REGRESSION** | not mentioned |
| `tau serve --connect ADDR` | **planned** (unspecified) | |
| `tau serve --web-root DIR` (static web client, origin check) | **REGRESSION** | no web client in Phase 7/8 |
| `tau serve --schema` | **planned** | Phase 7, first-class |
| `tau ext list [-e] [--json]` | **REGRESSION** | see 2.5 |
| `tau ext authorize NAME [--package]` | **REGRESSION** | |
| `tau ext revoke IDENTIFIER` | **REGRESSION** | |
| `tau ext disable NAME` / `tau ext enable NAME` | **REGRESSION** | |
| `tau config check [--show MODEL]` | **built** (new) | |
| `tau trust [DIR]` | **built** (new) | |
| env `TAU_HOME` | **built** | |

### 2.9 Frontends and interactive features

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Textual TUI (chat, sidebar, palette, dialogs, themes mocha/latte/gruvbox/ansi + user themes) | `app.py`, `chat_widgets.py`, `themes.py`, `tau.tcss`, `dialogs.py`, `modals.py` | **built** in part (8C-1: transcript, editor, commands, forms, questions, themes, dialogs; the sidebar becomes the cursor bar, 8C-2; the palette is not built) | Phase 8: "reuses 0.15's core widgets (message, tool and reasoning boxes, the stream strip, themes and `tau.tcss`, dialogs, the session picker), with the app around them written against `Client`". Zero lines exist today; `examples/chat.py` is the only interactive client |
| Typing while the model runs (steer / enqueue strategies, `TUI-STEERING.md`) | `steering.py`, `follow_ups.py` | **dropped** | D2: input is a scratchpad item delivered by the next step; "queueing is an extension's"; Retired terms: steering queue, follow-up queue |
| Enter-key mode, editor widgets, slash-command completion in editor | `editor_widgets.py`, `ENTER-KEY.md` | **planned** (unspecified) | `Client.complete` + `commandline` are the library half (D16) |
| `@file` attachments (inline limit, TUI chips) | `attachments.py`, `docs/FILE-ATTACHMENTS.md` | **REGRESSION** | Not built, not planned |
| `-p` headless printing | `headless.py` | **built** (changed) | D22 (8B) |
| JSONL event stream (`--mode json`, deltas) | `JSON-MODE-DELTAS.md` | **built** (changed) | `tau -p --json` (D22): the wire's events, with `Live` deltas for the cursor `-p` sends to; one format with `tau serve` and the schema |
| REPL head (scrollback prompt loop) | `repl.py`, `repl_input.py`, `repl_theme.py` | **REGRESSION** | see 2.8 |
| RPC (stdio JSON-RPC) | `tau_agent_core/rpc/`, `rpc_mode.py` | **planned** (changed) | Phase 7 `tau serve` (WebSocket); same `Client` contract serialized |
| `tau serve` multiplexer daemon, `RemoteClient` | `serve/` (8 files) | **planned** | Phase 7 (D16 stage 4) |
| VS Code / tau-code head | `docs/VSCODE-HEAD.md` | **planned** | Phase 8 "tau-code port ... regenerated from `--schema`" |
| Ratatui example client | none | **planned** (new) | Phase 8 `examples/ratatui/` |
| Web client served by `tau serve` | `serve/http.py` | **REGRESSION** | |
| Session picker | `session_picker.py` | **planned** | Phase 8 reuse |
| Compare view | `compare_view.py` | **planned** (unspecified) | see 2.3 |
| Extension UI (panels, extension dialogs) | `extension_ui.py` | **planned** (unspecified) | `show` string per extension per cursor is fixes D4; no panels |
| Devshot / screenshot harness for TUI | `devshot.py`, `testing/` | **REGRESSION** (dev tooling) | |
| Textual headless test support | `docs/textual-headless-testing.md` | **planned** (unspecified) | with the TUI |

### 2.10 Everything else

| capability | 0.15 where | 0.16 status | note |
|---|---|---|---|
| Usage / token accounting in events | `usage.py`, `docs/TOKEN-ACCOUNTING.md` | **built** (reduced) | `meta.usage {input, output, cached}` on each reply; no aggregation, no session stats |
| Cost / pricing (cache-write pricing, per-turn cost, TUI cost line, `test_cost.py`) | `docs/PRICING-AND-COMPACTION-CONFIG.md`, `app.py:_side_cost` | **REGRESSION** | no price fields in `ModelConfig`; not listed. Budget examples gone too |
| Session stats (`get_session_stats`) | `capabilities.py` | **REGRESSION** | not listed |
| Latency collection / run manifests (SIM_SPEC measurement runs) | `latency.py`, `run_manifest.py` | **REGRESSION** (research tooling) | `tau.trace` + `docs/benchmarks.md` cover tracing, not these artifacts |
| Telemetry strip (llama.cpp timings) | `examples/70_telemetry.py` | **REGRESSION** (minor) | see 2.1 |
| Tracing | none | **built** (new) | `tau.trace`, Chrome trace across Rust and Python |
| Agent docs generator (`@agent_facing`, `docs/library/reference`) | `docs.py`, `docs_build.py` | **planned** | Phase 7 reference from docstrings, surface manifest, guides |
| Packaging: five distributions, `ffwf-tau` meta-package | `tau-*/pyproject.toml`, `tau-meta` | **built** (changed) | one `ffwf-tau` wheel (cp312-abi3 + cp314t), import `tau` (D10) |
| Wheels: Linux, macOS, Windows, py3.11-3.14 | `package.sh`, `docs/RELEASING.md` | **planned** | Phase 9: only Linux x86_64 today; Python floor is now 3.12 (0.15: 3.11); base tools Linux-only. macOS and `powershell` tool deferred (plan.md) |
| Docker image / entrypoint | `Dockerfile`, `docker/` | **REGRESSION** (packaging) | |
| Free-threaded (3.14t) support | `experiments/freethreading` | **built** | `gil_used = false`, tested in `tests/core/test_threads.py` |
| JMFTS integration: store backend, enrichment extension, importer, catalog, `jmfts` tools | `tau-jmfts/` (5 + 4 files, 109 tests) | **post-0.16** | plan.md Phase 9 Open: "move the JMFTS replica past 0.16.0, since nothing feeds it and no frontend needs it"; D12: JMFTS is a replica fed from the local log |
| Extension examples | `examples/` (40 numbered) | **built** (reduced) | `0.16: examples/` 11 files + `speculative/` 7 |
| Tectum integration contract (`--no-tools`, `PI-RPC-REPLACEMENT`, WIRE-CONTRACT) | `docs/TECTUM-NO-TOOLS-MIGRATION.md` | **REGRESSION** (external) | Depends on stdio RPC and CLI flags that 0.16 lacks until Phase 7/8 |
| 0.15 hotfix branch | n/a | **built** | branch `0.15.x` (plan.md Phase 6) |

## 3. Tally

Counted by the leading status token of each table row in section 2 (a few rows carry two statuses; each
counted once under its first).

| status | rows | of which |
|---|---|---|
| built | 52 | includes "built (changed)" and "built (new)" rows (0.16-only: trust, `config check`, `.env`, tracing) |
| planned | 60 | 26 are "planned (unspecified)": the owner phase exists but no doc says the capability is in it |
| dropped by design | 20 | |
| REGRESSION | 54 | 21 of them are CLI flag or subcommand rows (many follow one feature, see section 4) |
| post-0.16 | 5 | all JMFTS |
| **total** | **191** | |

## 4. Regressions to look at before release (not built, not planned)

By impact on a coding agent session:

1. ~~**`edit`, `find`, `ls` tools** (2.2)~~ `edit` and `find` built (Stage 8A); `ls` dropped (`bash`).
2. ~~**System prompt / context files**~~ built (D20).
3. **`@file` attachments, image input, skills, prompt templates** (2.2, 2.5, 2.9).
4. **Cost, session stats, cache-miss notice, context window** (2.1, 2.4, 2.10). Usage tokens are recorded; nothing reads them.
5. **Extension management without a program**: `tau ext *`, drop-in discovery, authorization; there is no way to run an extension from the `tau` command except an unscheduled "trusted loader" (2.5).
6. **Session leftovers**: `--fork`, HTML export, tree browser, `--session-dir`, session stats (2.3).
7. ~~**Headless approval**~~ built (Stage 8B): `--answer` (2.6).
8. **REPL head, web client, `serve --tail`** (2.8, 2.9).
9. **Token counter, constrained decoding, models.dev catalog, codemode, background tools, NATS bus** (2.1, 2.2, 2.5): extension-shaped or tooling; each can be a later extension but none is listed.
10. Cosmetic/minor: ~~`--version`, `--verbose`~~ built (8B), `--fun`, telemetry strip, devshot, Docker.

Accepted by design (not regressions, but behavior changes users will feel): provider retries (example only),
implicit per-vendor API key discovery, unified `--thinking` levels, steering/enqueue, suspension step-debugger,
0.15 session files (no converter), 0.15 extensions (no shim), JMFTS (post-0.16).

## 5. Sources read

0.15: `CLAUDE.md`, `docs/INDEX.md`, `tau-coding-agent/src/tau_coding_agent/{cli,ext_cli,serve/cli}.py`,
`tau-agent-core/src/tau_agent_core/{capabilities,session_log,settings,tools/*}.py`, `tau-llm/src/tau_llm/*`.
0.16: `docs/plan.md`, `docs/plan-phase5-fixes.md`, `docs/design/{model,glossary}.md`, decisions D3, D8, D13,
D15, D16, `python/tau/{cli,__init__,providers/*,_config}.py`, `examples/`, `tests/`, `crates/*`.
Counts are `grep` over test function definitions, not collected-test counts, so parametrized tests are 1 each.
