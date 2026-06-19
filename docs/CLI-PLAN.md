# τ CLI Flag Plan (validated against pi)

**Status:** PLANNING / VALIDATION. No CLI code changed by this doc.
**Source of truth:** pi's `parseArgs()` in `~/Development/pi/packages/coding-agent/src/cli/args.ts` (full flag set + help text live there). pi's `cli.ts` just calls `main(process.argv.slice(2))`; `main.ts` consumes the parsed `Args`.
**Goal:** API *familiarity* with pi's core CLI surface, core features first — not 1:1 parity of every niche flag.

This plan supersedes the priority/mapping content in `docs/COMMAND_LINE.md`, which has factual errors (enumerated in §4). `COMMAND_LINE.md` should be corrected, not deleted.

---

## Implementation status (2026-06-19)

The **Core** set below is now implemented in `tau-coding-agent/src/tau_coding_agent/cli.py`
(argparse) + `headless.py` (the `--print` run path), with tests in
`tau-coding-agent/tests/test_cli.py`.

**Shipped (wired end-to-end, not inert):**
- `--print`/`-p` → headless run via `create_backend(model_config).stream_chat(...)`
  (the same path the TUI uses), printing to stdout and exiting. Note: built on the
  real backend path, **not** `run_agent_loop.py` (that file shells out to `pi` to
  *build* τ — it is not a headless τ runner; the original plan misidentified it).
- positional `messages` + `@file` inlining; message-eating works via argparse positionals.
- `--mode {text,json}` — text transcript, or JSONL of the backend's normalized
  lifecycle events (`turn_start`/`text_delta`/`tool_call`/`tool_result`) + a final
  `{"kind":"done", ...}`. (`rpc` deferred.)
- `--model`/`-m` with `provider/id` shorthand; resolves against the config `models`
  map, else constructs an ad-hoc entry. Wired into **both** headless and the TUI
  (via `Parley(cli_overrides=...)`).
- `--provider` (long-only), `--tools`/`-t`, `--no-tools`/`-nt`, `--system-prompt`.
- `--version`/`-v` (pi-aligned: `-v` is version; τ's old `-v`=verbose is dropped),
  `--verbose` (long-only), `--help`/`-h`.

**Deferred — Fail-Early, NOT stubbed (a `:level` thinking suffix or these flags
error clearly rather than silently no-op):**
- `--thinking` — needs a `reasoning_effort` send-path in τ-ai (`Model` has no
  reasoning field; `openai.py` only *reads* it). `--model x:high` raises until then.
- `--continue`/`-c`, `--resume`, `--session`, `--fork`, `--name` — `SessionManager`
  has `list()`/`load()`/`fork()`, but headless continuation also needs session→context
  wiring + tests. Tracked, not yet exposed.

Removed the old inert/non-pi flags (`--output`/`-o`, `-s`, `--config`, `--cwd`,
`--context-window`, `--max-tokens`) and the `-p`=provider / `-v`=verbose short-alias
collisions. `CLIArgs` was reshaped accordingly (see `cli.py`).

---

## 1. pi's actual CLI surface (the reference)

Every flag below is parsed in `args.ts:63-210` (`parseArgs`) and documented in `printHelp` (`args.ts:212-390`). Short aliases are *exactly* as pi defines them — note they differ from tau's current `cli.py`.

| pi flag | short | value | parse loc | notes |
|---|---|---|---|---|
| `--help` | `-h` | — | `args.ts:74` | |
| `--version` | `-v` | — | `args.ts:76` | **`-v` is version in pi, NOT verbose** |
| `--mode` | — | `text\|json\|rpc` | `args.ts:78` | default `text` (`Mode` type, `args.ts:10`) |
| `--continue` | `-c` | — | `args.ts:83` | |
| `--resume` | `-r` | — | `args.ts:85` | interactive session picker |
| `--provider` | — | name | `args.ts:87` | **no short alias in pi**; default provider `google` (`args.ts:238`) |
| `--model` | — | pattern | `args.ts:89` | **no short alias in pi**; supports `provider/id` and `:thinking` (`args.ts:239`) |
| `--api-key` | — | key | `args.ts:91` | requires a model also specified (`main.ts:694`) |
| `--system-prompt` | — | text | `args.ts:93` | |
| `--append-system-prompt` | — | text/file (repeatable) | `args.ts:95` | accumulates into array |
| `--name` | `-n` | name | `args.ts:98` | session display name; errors if value missing |
| `--no-session` | — | — | `args.ts:104` | ephemeral |
| `--session` | — | path\|partial-UUID | `args.ts:106` | |
| `--session-id` | — | id | `args.ts:108` | exact project session id, create-if-missing |
| `--fork` | — | path\|partial-UUID | `args.ts:110` | fork into new session |
| `--session-dir` | — | dir | `args.ts:112` | |
| `--models` | — | comma list | `args.ts:114` | Ctrl+P cycling patterns (globs/fuzzy) |
| `--no-tools` | `-nt` | — | `args.ts:116` | |
| `--no-builtin-tools` | `-nbt` | — | `args.ts:118` | keep extension/custom tools |
| `--tools` | `-t` | comma list | `args.ts:120` | allowlist |
| `--exclude-tools` | `-xt` | comma list | `args.ts:125` | denylist |
| `--thinking` | — | level | `args.ts:130` | `off\|minimal\|low\|medium\|high\|xhigh` (`args.ts:57`) |
| `--print` | `-p` | — (consumes inline msg) | `args.ts:140` | see "print message-eating" below |
| `--export` | — | file | `args.ts:147` | export session → HTML, exit |
| `--extension` | `-e` | path (repeatable) | `args.ts:149` | |
| `--no-extensions` | `-ne` | — | `args.ts:152` | |
| `--skill` | — | path (repeatable) | `args.ts:154` | |
| `--prompt-template` | — | path (repeatable) | `args.ts:157` | |
| `--theme` | — | path (repeatable) | `args.ts:160` | |
| `--no-skills` | `-ns` | — | `args.ts:163` | |
| `--no-prompt-templates` | `-np` | — | `args.ts:165` | |
| `--no-themes` | — | — | `args.ts:167` | |
| `--no-context-files` | `-nc` | — | `args.ts:169` | disables AGENTS.md **and** CLAUDE.md |
| `--list-models` | — | optional search | `args.ts:171` | `string \| true` |
| `--verbose` | — | — | `args.ts:178` | **long-only in pi; no `-v`** |
| `--approve` | `-a` | — | `args.ts:180` | trust project-local files this run |
| `--no-approve` | `-na` | — | `args.ts:182` | |
| `--offline` | — | — | `args.ts:184` | same as `PI_OFFLINE=1` (`args.ts:276`) |
| `@file` | — | — | `args.ts:186` | strips `@`, pushes to `fileArgs` |
| `--<unknown>` | — | — | `args.ts:188` | captured into `unknownFlags` map for extension flags |
| positional | — | — | `args.ts:204` | pushed to `messages` (multiple allowed) |

**Things pi does NOT have** (the tau doc invents pi-equivalents for these — see §4): `--cwd`, `--config`, `--context-window`, `--max-tokens`, `--output`/`-o`, `-m`, `-s`. pi expresses output mode as `--mode`, not `--output`.

**pi's `--print` "message-eating" rule** (`args.ts:140-146`): after `-p`/`--print`, if the *next* arg is present and is not `@…` and not a single-`-`/`--` flag (triple-dash `---` allowed), it is consumed as the prompt message. This lets `pi -p "prompt"` work without a separator. tau should replicate this exactly if it wants `tau -p "prompt"` to behave identically.

**Commands (subcommands, not flags)** — `args.ts:228-235`: `install`, `remove`/`uninstall`, `update`, `list`, `config`. These are the package-manager surface (`package-manager-cli.ts`). **Out of scope** for the core CLI plan; tau has no extension package manager yet.

### Model shorthand parsing (pi, authoritative)

Implemented in `~/Development/pi/packages/coding-agent/src/core/model-resolver.ts`, not in `args.ts` (args.ts stores the raw `--model` string; resolution happens later via `resolveCliModel`, `main.ts:31`).

- **Provider prefix** `provider/model`: split on first `/` (`model-resolver.ts:97`, `:387`). If no `--provider`, pi tries to interpret `provider/model` first (`model-resolver.ts:378`).
- **Thinking suffix** `model:level`: split on the **last** colon (`model-resolver.ts:204`, `:266`). If the suffix is a valid thinking level it's used; in strict CLI mode an *invalid* suffix makes the whole pattern fail rather than silently dropping (`model-resolver.ts:225-233`) — i.e. pi does NOT fabricate a fallback in `--model` strict mode. **This matches "Fail Early"; tau should copy it.**
- Default thinking level when unspecified: `medium` (`~/Development/pi/packages/coding-agent/src/core/defaults.ts:3`, `DEFAULT_THINKING_LEVEL`).

### Thinking → request param (pi)

`ThinkingLevel = "minimal"|"low"|"medium"|"high"|"xhigh"`; `ModelThinkingLevel` adds `"off"` (`~/Development/pi/packages/ai/src/types.ts:65-66`). `xhigh` is a **distinct level**, not an alias of `high`; `off` means "send no reasoning param". The per-provider mapping is `thinkingLevelMap` / `reasoningFormat` (`types.ts:406`, `:590`) and is clamped per model (`clampThinkingLevel`, `~/Development/pi/packages/ai/src/models.ts:64`). For the default OpenAI-compatible path the param is `reasoning_effort` (`types.ts:406`).

### Env vars (pi, authoritative — `config.ts`)

pi derives env-var names from `APP_NAME` (`config.ts:475`, default `"pi"`). The agent/session dir vars are **`PI_CODING_AGENT_DIR`** and **`PI_CODING_AGENT_SESSION_DIR`** (`config.ts:481-482`, referenced in help `args.ts:374-375`). pi also reads `PI_OFFLINE`, `PI_PACKAGE_DIR`, `PI_TELEMETRY`, `PI_SHARE_VIEWER_URL` (help `args.ts:377-379`) and ~40 provider `*_API_KEY` vars (`args.ts:336-373`). **pi has no `PI_MODEL`/`PI_PROVIDER`/`PI_THINKING`/`PI_TOOLS`/`PI_SYSTEM_PROMPT`/`PI_CWD`/`PI_PRINT`/`PI_CONTINUE`.** The `TAU_*` table in `COMMAND_LINE.md` is invented (see §4).

---

## 2. tau's current CLI surface (what `cli.py` actually does today)

`tau-coding-agent/src/tau_coding_agent/cli.py`, hand-rolled `parse_cli_args()` (`cli.py:35-87`). It builds a `CLIArgs` dataclass but **`main()` ignores almost all of it** — `main()` only checks `args.verbose` for debug prints, then launches `Parley()` with no arguments (`cli.py:90-111`). The TUI manages its own config/backend. So in practice the flags are *parsed but inert*.

| tau flag | short | wired into behavior? | divergence from pi |
|---|---|---|---|
| `--model` | `-m` | parsed, **unused** by `main()` | pi has no `-m` short alias |
| `--provider` | `-p` | parsed, **unused** | **conflict: pi's `-p` is `--print`** |
| `--session` | `-s` | parsed, **unused** | pi has no `-s`; pi's session flag is long-only |
| `--output` | `-o` | parsed, **unused** | **pi uses `--mode {text,json,rpc}`, not `--output {tui,json}`** |
| `--verbose` | `-v` | only flag honored | **pi's `-v` is `--version`**; pi's verbose is long-only |
| `--config` | — | parsed, **unused** | not a pi flag |
| `--cwd` | — | parsed, **unused** | not a pi flag |
| `--context-window` | — | parsed, **unused** | not a pi flag |
| `--max-tokens` | — | parsed, **unused** | not a pi flag |

Net: tau implements 9 flags, **all effectively no-ops** (except `--verbose`'s debug print), and 4 of them (`-m`,`-p`,`-s`,`-o`,`-v`) use short aliases that *collide semantically* with pi. There is **no print mode, no session continuation, no tool/thinking/system-prompt control, no positional messages, no `@file` handling**.

### Plumbing reality check (constrains the plan)
- **`--thinking` has no backend yet.** tau-ai's `Model` (`tau-ai/src/tau_ai/types.py:109-123`) has no reasoning/thinking field, and `tau-ai/src/tau_ai/providers/openai.py` only *reads* `reasoning` from responses (`openai.py:431`, `:701`); it never *sends* `reasoning_effort`. So `--thinking` requires a tau-ai change (add a request param), not just a CLI flag. Flag this; do not fake it.
- **Tools** resolve via `_resolve_tools(names)` (`tau-agent-core/src/tau_agent_core/sdk.py:104-155`) over the 7 built-ins `read,write,edit,bash,ls,grep,find`. `--tools`/`--no-tools` map cleanly onto this; `--no-builtin-tools`/`--exclude-tools` need extension tooling that tau lacks → lower priority.
- **Sessions** exist in `tau_agent_core.session_manager.SessionManager` (`new_session`, etc.), but `--continue`/`--fork`/`--resume`/`--session` are not surfaced. The current `TauBackend.__init__` always calls `self.session_manager.new_session()` (`backends.py:90`), so continuation needs a code path that loads instead of creating.
- **`-p`/print mode** requires a *headless* run path. tau's `run_agent_loop.py` is the closest existing headless driver and should be the basis, not the TUI.

---

## 3. Prioritized pi → tau flag plan

Priority tiers: **Core** (familiarity-critical, ship first) · **Secondary** (high value, not blocking) · **Nice-to-have**.
Status: ✅ in tau (even if inert) · ➖ divergent (exists but wrong semantics/alias) · ❌ missing.

### Core (ship first — these define "feels like pi")

| pi flag (cite) | proposed tau flag | status | notes |
|---|---|---|---|
| `--print`/`-p` (`args.ts:140`) | `--print` / `-p` | ❌ | The biggest gap. Headless run → stdout, exit. Replicate the message-eating rule (`args.ts:142-146`). **Frees `-p` from `--provider`.** Build on `run_agent_loop.py`, not the TUI. |
| `--mode` (`args.ts:78`) | `--mode {text,json,rpc}` | ➖ | Replaces tau's `--output {tui,json}`. Start with `text`+`json`; `rpc` is a separate phase. Keep `tui` as the *interactive default when no `--mode`/`-p`*, but don't expose `tui` as a `--mode` value (pi doesn't). |
| `--model` (`args.ts:89`) | `--model` (+ keep `-m` as tau extension) | ✅/➖ | Already present but inert and must actually drive config. Add `provider/id` + `:thinking` shorthand per `model-resolver.ts` (§1). pi has no `-m`; keeping `-m` is a deliberate, documented tau-only convenience. |
| `--provider` (`args.ts:87`) | `--provider` (long-only) | ➖ | **Drop tau's `-p` alias** (collides with print). Wire into model resolution. |
| `--continue`/`-c` (`args.ts:83`) | `--continue` / `-c` | ❌ | Load latest session for cwd instead of `new_session()`. |
| `--tools`/`-t` (`args.ts:120`) | `--tools` / `-t` | ❌ | Comma list → `_resolve_tools` allowlist. |
| `--no-tools`/`-nt` (`args.ts:116`) | `--no-tools` / `-nt` | ❌ | Empty tool list (read-only agent). |
| `--thinking` (`args.ts:130`) | `--thinking` | ❌ (**needs tau-ai work**) | Levels `off..xhigh` (`args.ts:57`). **Blocked on adding `reasoning_effort` send-path in tau-ai (`openai.py`); do not stub.** Default `medium` (`defaults.ts:3`). |
| `@file` (`args.ts:186`) + positional messages (`args.ts:204`) | same | ❌ | Core ergonomics: `tau @README.md "summarize"`. Without this, no initial-prompt UX. |
| `--help`/`-h` (`args.ts:74`) | `--help` / `-h` | ❌ | argparse gives this for free. |
| `--version`/`-v` (`args.ts:76`) | `--version` (consider freeing `-v`) | ➖ | **pi's `-v` = version.** tau currently uses `-v` for verbose. Decide: either (a) match pi (`-v`=version, verbose long-only) or (b) keep tau's `-v`=verbose and document the divergence. Recommend matching pi for familiarity. |
| `--verbose` (`args.ts:178`) | `--verbose` (long-only) | ➖ | Already honored; just drop the `-v` short if matching pi on version. |

### Secondary (high value, after Core)

| pi flag (cite) | proposed tau flag | status | notes |
|---|---|---|---|
| `--system-prompt` (`args.ts:93`) | `--system-prompt` | ❌ | Replace system prompt (text or file). Feeds `AgentSession(system_prompt=…)`. |
| `--append-system-prompt` (`args.ts:95`) | `--append-system-prompt` (repeatable) | ❌ | Accumulate; append after default + context files. |
| `--resume`/`-r` (`args.ts:85`) | `--resume` / `-r` | ❌ | Interactive session picker (TUI). Lower than `--continue` because it needs picker UI. |
| `--session` (`args.ts:106`) | `--session` (long-only) | ➖ | tau's `-s` alias is non-pi; make long-only, accept path or partial UUID. Currently inert "session_name". |
| `--no-session` (`args.ts:104`) | `--no-session` | ❌ | Ephemeral — skip the `new_session()`/persist path. |
| `--fork` (`args.ts:110`) | `--fork` | ❌ | Needs `SessionManager` fork support (verify it exists before promising). |
| `--name`/`-n` (`args.ts:98`) | `--name` / `-n` | ❌ | Session display name. Missing from the tau doc entirely. |
| `--exclude-tools`/`-xt` (`args.ts:125`) | `--exclude-tools` / `-xt` | ❌ | Denylist; pairs with `--tools`. Missing from the tau doc. |
| `--list-models` (`args.ts:171`) | `--list-models [search]` | ❌ | Reads from `~/.tau/config.json` models map; cheap to implement. |
| `--session-dir` (`args.ts:112`) | `--session-dir` | ❌ | Override session storage dir. |

### Nice-to-have (later / when subsystems exist)

| pi flag (cite) | proposed tau flag | status | notes |
|---|---|---|---|
| `--no-builtin-tools`/`-nbt` (`args.ts:118`) | same | ❌ | Meaningful only once tau has extension/custom tools; otherwise == `--no-tools`. |
| `--extension`/`-e`, `--no-extensions`/`-ne` (`args.ts:149,152`) | same | ❌ | Extension subsystem is partial in tau (`sdk.py:_load_extensions`); wire when stable. |
| `--skill`/`--no-skills` (`args.ts:154,163`) | same | ❌ | No skills subsystem in tau yet. |
| `--prompt-template`/`--no-prompt-templates` (`args.ts:157,165`) | same | ❌ | No template subsystem in tau yet. |
| `--theme`/`--no-themes` (`args.ts:160,167`) | same | ❌ | TUI theming; defer. |
| `--no-context-files`/`-nc` (`args.ts:169`) | same | ❌ | Once AGENTS.md/CLAUDE.md discovery lands. |
| `--export` (`args.ts:147`) | `--export` | ❌ | Session → HTML export; needs an exporter. |
| `--offline` (`args.ts:184`) | `--offline` | ❌ | Only meaningful once tau does startup network ops. |
| `--approve`/`-a`, `--no-approve`/`-na` (`args.ts:180,182`) | same | ❌ | Project-trust model; tau has no trust manager yet. |
| `--api-key` (`args.ts:91`) | `--api-key` | ❌ | Supported by pi; the tau doc calls it a "security risk / out of scope". **This is an opinionated divergence from pi, not a pi gap** — note it as a deliberate choice, not an omission. Env vars / `config.json` remain the primary path. |
| `--session-id` (`args.ts:108`) | `--session-id` | ❌ | Exact project session id (create-if-missing). Niche. |
| `--mode rpc` (`args.ts:80`) | `--mode rpc` | ❌ | Separate RPC phase (pi has whole `modes/` dir). |
| subcommands `install/remove/update/list/config` (`args.ts:228-235`) | — | ❌ | Package-manager surface; out of scope until tau has one. |

### Arg-parsing approach
Replace the hand-rolled `parse_cli_args()` with **`argparse`** (stdlib, no new dep). It gives `--help`/`-h`, type coercion, repeatable (`action="append"`) flags for `--append-system-prompt`/`--extension`/`--skill`/`--theme`, `nargs="*"` for positional `@files`/messages, and `choices=` validation for `--mode` and `--thinking`. Two argparse caveats to handle manually:
- pi's `--print` message-eating and the `@file` / unknown-extension-flag passthrough don't map to vanilla argparse; use `parse_known_args()` and post-process `@…` + leftover `--flags` into a tau equivalent of pi's `fileArgs` / `unknownFlags` (`args.ts:186-201`).
- Keep tau-only aliases (`-m`) explicitly and **document every place tau's short flags diverge from pi** (`-v`, `-p`, `-s`, `-o`).

### Relationship to `~/.tau/config.json`
Config (`~/.tau/config.json`: `models` map, `default_model: "local-llm"`, `system_prompt`) is the **baseline**; CLI flags **override per-invocation**. Precedence: CLI flag > config.json > built-in default. Concretely: `--model` selects/!overrides a key in the `models` map (and `provider/id:thinking` shorthand may bypass the map entirely, like pi's `resolveCliModel`); `--system-prompt`/`--append-system-prompt` override/extend `config.json.system_prompt`; `--tools` overrides `TauBackend`'s default 7-tool list (`backends.py:76`). For env-var overrides, if tau adds any, name them `TAU_CODING_AGENT_DIR` / `TAU_CODING_AGENT_SESSION_DIR` to mirror pi's `PI_CODING_AGENT_*` derivation (`config.ts:481-482`) — **not** the ad-hoc `TAU_MODEL`/`TAU_THINKING`/… set in the current doc, which has no pi analog.

### Open questions (Fail Early — do not invent)
- **`SessionManager.fork()` existence** in tau-agent-core is unverified here; confirm before committing `--fork`. The tau doc asserts "All session operations already exist" — that claim is unverified.
- **`--resume` picker**: pi opens a TUI selector (`cli/session-picker.ts`). tau's equivalent picker UI is unbuilt; scope the UI work before promising the flag.
- **`provider/id` resolution against tau's config**: pi resolves against a model *registry*; tau resolves against the `config.json` `models` map. The exact behavior when a `provider/id` shorthand has no matching config key is undefined in tau and must be decided (error vs. construct an ad-hoc `Model`).

---

## 4. Corrections `docs/COMMAND_LINE.md` needs (do not edit it from this task; listed for a follow-up)

1. **Title/scope mismatch.** It's titled a "Plan" but is named `COMMAND_LINE.md` and reads as a spec. Either rename or clearly mark it superseded by this file for the flag plan.
2. **`--output`/`-o` is not a pi flag.** Doc's "Current State" table and examples imply `--output tui|json`. pi uses **`--mode {text,json,rpc}`** (`args.ts:78`). `tui` is not a pi `--mode` value.
3. **`--cwd`, `--config`, `--context-window`, `--max-tokens` are not pi flags**, yet the doc lists pi equivalents / implies parity. They are tau-local (and currently inert). Remove the implied pi mapping or mark them "tau-only, not in pi".
4. **Short-flag collisions are not flagged.** tau's `-p`=`--provider`, `-s`=`--session`, `-o`=`--output`, `-v`=`--verbose`, `-m`=`--model` all diverge from pi (`-p`=print, `-v`=version, `-m`/`-s`/`-o` absent). The doc proposes `-p`=`--print` and `-v`=`--version` in the same breath as claiming the current `-p`/`-v` are "Done" — internally inconsistent.
5. **"Everything else from pi … ~30+ flags … Missing"** undercounts/miscounts and, more importantly, the per-flag "Pi Equivalent" column contains non-existent pi flags (see #3) and **omits real pi flags**: `--exclude-tools`/`-xt`, `--name`/`-n`, `--session-id`, `--approve`/`-a`, `--no-approve`/`-na`.
6. **Thinking map is wrong.** Doc's `THINKING_MAP` collapses `minimal→low` and `xhigh→high` and maps `off→None`. In pi, `minimal` and `xhigh` are **distinct levels** (`types.ts:65`, `models.ts:51`); only `off` means "no reasoning param". Don't lossy-map. Also: tau-ai has **no send-path for `reasoning_effort` yet**, so the doc's "just map to `reasoning_effort`" understates the work.
7. **Env-var table is fabricated.** `TAU_DIR`, `TAU_SESSION_DIR`, `TAU_MODEL`, `TAU_PROVIDER`, `TAU_THINKING`, `TAU_TOOLS`, `TAU_SYSTEM_PROMPT`, `TAU_CWD`, `TAU_PRINT`, `TAU_CONTINUE` have **no pi analog**. pi's dir vars are `PI_CODING_AGENT_DIR`/`PI_CODING_AGENT_SESSION_DIR` (`config.ts:481-482`); pi has no model/provider/thinking/tools/print/continue env vars. If tau wants env overrides, mirror pi's naming (`TAU_CODING_AGENT_*`) and don't claim "already match pi".
8. **"All session operations already exist … just needs CLI bridge" is unverified.** `--fork`/`--resume` depend on `SessionManager` capabilities and a picker UI that are not confirmed present (see §3 open questions). Don't assert availability.
9. **Default provider.** Doc's model-shorthand pseudocode defaults provider to `"openai"`. pi's documented default provider is **`google`** (`args.ts:238`); pi also *infers* provider from `provider/id` before defaulting (`model-resolver.ts:378`). State tau's chosen default explicitly rather than implying it matches pi.
10. **`--api-key` framing.** Doc lists it "P3 / out of scope — security risk." Fine as a *tau choice*, but it should be labeled a deliberate divergence from pi (which fully supports `--api-key`, `args.ts:91`), not a missing/niche pi flag.
11. **Print mode JSON example uses tau-agent-core event names** (`agent_start`, `message_update`, `tool_execution_end`, …). That's tau's `AgentEvent` vocabulary, which is fine — but note it differs from pi's `--mode json` schema; if json-mode familiarity with pi matters, pi's event shape (in `modes/`) should be checked before locking the format. (Not validated here.)

---

## 5. Summary of the Core set (the must-haves for pi familiarity)

`--print`/`-p` (+ message-eating) · `--mode {text,json}` (replacing `--output`) · `--model` with `provider/id:thinking` shorthand · `--provider` (long-only, drop tau's `-p`) · `--continue`/`-c` · `--tools`/`-t` · `--no-tools`/`-nt` · `--thinking` (gated on tau-ai `reasoning_effort` send-path) · `@file` + positional messages · `--help`/`-h` · `--version` (resolve the `-v` collision).

Biggest single gap, same as the existing doc concluded: **print mode** — but it must be built on a headless path (`run_agent_loop.py`), and the surrounding flag set must be corrected against pi as above.
