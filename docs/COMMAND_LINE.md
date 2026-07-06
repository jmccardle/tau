# τ Command Line Interface — status & history

> **⚠️ Superseded for the flag *plan*; corrected here for the factual record (2026-06-21).**
>
> This file began as a *pre-implementation* CLI plan and accumulated factual
> errors about pi's surface (invented flags, a lossy thinking map, a fabricated
> env-var table). The authoritative, pi-validated flag plan is now
> **`docs/CLI-PLAN.md`**; the *implemented* surface is **`cli.py`** +
> **`headless.py`** (tests in `tests/test_cli.py`, `tests/test_headless_resume.py`).
> This doc is kept (not deleted) as the historical CLI narrative, with every known
> error corrected. The 11 corrections applied here are enumerated in
> `docs/CLI-PLAN.md` §4.
>
> When this doc and `CLI-PLAN.md`/`cli.py` disagree, **they win** — fix this file.

## Overview

This document narrates τ's CLI: what shipped, how it maps to `pi`'s flag set, and
where τ deliberately diverges. The goal is *familiarity* with pi's core surface
(model selection, tool control, session management, output modes) — **not** 1:1
parity of every niche flag. pi is the source of truth: its parser is
`~/Development/pi/packages/coding-agent/src/cli/args.ts` (`parseArgs`,
`args.ts:63-210`).

## Current state (2026-06-21) — what `cli.py` actually implements

argparse-based (`build_parser()` in `cli.py`); `--print` runs headless via
`headless.py` (the same backend path as the TUI). Short aliases are **pi-aligned**
(the old tau collisions below are gone).

| Flag | short | τ status | notes |
|------|-------|----------|-------|
| `--print` | `-p` | ✅ wired | headless run → stdout, then exit. **`-p` is print (pi-aligned), no longer `--provider`.** |
| `--mode {text,json}` | — | ✅ wired | text transcript or JSONL lifecycle events. (`rpc` deferred.) |
| `--model` | `-m` | ✅ wired | config `models` key, or `provider/id[:thinking]` shorthand. `-m` is a documented **tau-only** convenience (pi has no `-m`). |
| `--provider` | — | ✅ wired | long-only (pi-aligned). |
| `--tools` | `-t` | ✅ wired | comma-separated allowlist over the 7 built-ins. |
| `--no-tools` | `-nt` | ✅ wired | read-only agent (empty tool list). |
| `--system-prompt` | — | ✅ wired | replace the system prompt for this run. |
| `--thinking {off…xhigh}` | — | ✅ wired | sends `reasoning_effort` (clamped, gated on `Model.reasoning`). See "Thinking" below. |
| `--continue` | `-c` | ✅ wired (headless) | continue the most recent `~/.tau/chats` session. |
| `--session REF` | — | ✅ wired (headless) | resume a specific session (`.json` path or filename **stem**). |
| `--fork REF` | — | ✅ wired (headless) | continue a session into a **new** file; source untouched. |
| `--name` | `-n` | ✅ wired | session display title. |
| `--resume` | `-r` | ⛔ deferred (Fail-Early) | pi's *interactive* picker (`args.ts:85`); no headless meaning, TUI resumes from the sidebar, so `main()` rejects it with a pointer to `--continue`/`--session`. |
| `@file` + positional messages | — | ✅ wired | `tau -p @README.md "summarize"`. |
| `--help` | `-h` | ✅ (argparse) | |
| `--version` | `-v` | ✅ wired | **pi-aligned: `-v` is version.** τ's old `-v`=verbose is dropped. |
| `--verbose` | — | ✅ wired | long-only (pi-aligned). |

**Removed as inert/non-pi** (they were parsed-but-unused and had no pi analog or a
colliding alias): `--output`/`-o` (pi expresses output as `--mode`), `-s`
(`--session` is long-only), `--config`, `--cwd`, `--context-window`,
`--max-tokens`, and the `-p`=provider / `-v`=verbose short-alias collisions.

## Mapping to pi (corrected)

pi's full surface is tabulated in `docs/CLI-PLAN.md` §1 (every flag with its
`args.ts` line). The corrections that matter for this doc:

- **`--output`/`-o` is not a pi flag.** pi uses `--mode {text,json,rpc}`
  (`args.ts:78`); `tui` is not a pi `--mode` value (it's the interactive default
  when neither `--mode` nor `-p` is given).
- **`--cwd`, `--config`, `--context-window`, `--max-tokens` are not pi flags.**
  They were tau-only and inert; removed.
- **Short-alias collisions (now resolved).** τ previously used `-p`=`--provider`,
  `-s`=`--session`, `-o`=`--output`, `-v`=`--verbose`, `-m`=`--model`. pi uses
  `-p`=print, `-v`=version, and has no `-m`/`-s`/`-o`. τ now matches pi on
  `-p`/`-v`, drops `-s`/`-o`, and keeps `-m` as a *documented* tau-only alias.
- **Real pi flags this doc originally omitted:** `--exclude-tools`/`-xt`
  (`args.ts:125`), `--name`/`-n` (`args.ts:98`, now shipped), `--session-id`
  (`args.ts:108`), `--approve`/`-a` (`args.ts:180`), `--no-approve`/`-na`
  (`args.ts:182`). The "~30+ flags missing" line was an uncounted hand-wave; see
  CLI-PLAN §3 for the actual prioritized list.
- **Default provider.** pi's documented default provider is **`google`**
  (`args.ts:238`) and it *infers* provider from a `provider/id` shorthand before
  defaulting (`model-resolver.ts:378`). The earlier "default to `openai`"
  pseudocode was wrong about pi. τ resolves `--model` against the `config.json`
  `models` map (or constructs an ad-hoc entry); state τ's own default explicitly
  rather than implying pi-parity.
- **`--api-key` is a deliberate τ divergence, not a pi gap.** pi fully supports
  `--api-key` (`args.ts:91`). τ keeps keys in env vars / `config.json` by choice;
  label it a *deliberate* divergence, not a "missing/niche" pi flag.

## Thinking (corrected mapping)

pi levels: `off | minimal | low | medium | high | xhigh`
(`ModelThinkingLevel`, pi `ai/types.ts:65`). **`minimal` and `xhigh` are
*distinct* levels — not aliases of `low`/`high`.** Only `off` means "send no
reasoning param". The earlier `THINKING_MAP` that collapsed `minimal→low` and
`xhigh→high` was lossy and wrong.

τ's actual behavior (shipped): `tau_ai/models.py` ports pi's `clampThinkingLevel`
(`models.ts:64`) and clamps per-model via `thinking_level_map`; `openai.py` emits
`reasoning_effort` for the default OpenAI-compatible path (`types.ts:406`), gated
on `Model.reasoning` so it's never sent to a non-reasoning model. Default level
when unspecified is `medium` (pi `defaults.ts:3`).

> **Test-rig caveat:** against the local llama.cpp + Qwen3 GGUF server,
> `reasoning_effort` is a silent no-op (HTTP 200, never validated, dropped before
> templating). τ puts it on the wire correctly and the unit tests assert the
> *payload*, but the local rig has no server-side effect. See ROADMAP Tier 3 #4.

## Sessions (corrected)

The earlier claim — "all session operations already exist … just needs a CLI
bridge" over `tau_agent_core.session_manager` — pointed at the **wrong layer**.
Tracing the code: `TauBackend.stream_chat(messages, …)` treats its `messages` arg
as the authoritative context (`backends.py:162,241`); the internal
`SessionManager`/`new_session()` is vestigial on the TUI/headless path. Both the
TUI and `tau -p` persist/resume via the **`Chat` store** (`~/.tau/chats/*.json`,
`session_store.py`).

So headless resume = load a `Chat`, prepend its `.messages` as context, append the
new user turn, run, save back — **in place** for `--continue`/`--session`, a
**new file** for `--fork` (with a same-second collision guard so a fork never
clobbers its source). A resumed run keeps the session's stored model unless
`--model` overrides; combining `--system-prompt` with a resume raises (the session
already has one). `--resume`'s interactive picker is TUI-only and stays deferred.
See `headless.py` (`_select_chat`/`_resolve_selector`/`_persist_session`).

## Output modes & the JSON schema caveat

- `text` (default): plain transcript with inline tool-call / tool-result
  annotations.
- `json` (`-p --mode json`): one JSON object per line — τ's **`AgentEvent`**
  vocabulary (`agent_start`, `turn_start`, `message_start/update/end`,
  `tool_execution_start/end`, `turn_end`, `agent_end`) plus a final
  `{"kind":"done", …}`. This is τ's own event shape and **differs from pi's
  `--mode json` schema** (pi's lives in its `modes/` dir). If json-mode
  familiarity with pi ever matters, pi's schema must be checked before locking
  τ's format — it has not been validated against pi here.

## Environment variables (corrected — the earlier table was fabricated)

The earlier `TAU_DIR`/`TAU_SESSION_DIR`/`TAU_MODEL`/`TAU_PROVIDER`/`TAU_THINKING`/
`TAU_TOOLS`/`TAU_SYSTEM_PROMPT`/`TAU_CWD`/`TAU_PRINT`/`TAU_CONTINUE` table had **no
pi analog** and was not implemented. pi derives env-var names from `APP_NAME`
(`config.ts:475`); its dir vars are `PI_CODING_AGENT_DIR` /
`PI_CODING_AGENT_SESSION_DIR` (`config.ts:481-482`). pi has **no**
model/provider/thinking/tools/print/continue env vars.

What τ actually reads today:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key (read by the provider when no config/CLI key is given). |

If τ later adds dir/session env overrides, mirror pi's derivation as
`TAU_CODING_AGENT_DIR` / `TAU_CODING_AGENT_SESSION_DIR` — **not** the ad-hoc
`TAU_MODEL`/`TAU_THINKING`/… set, which has no pi analog. Config precedence today
is: CLI flag > `~/.tau/config.json` > built-in default.

## History

This doc originally proposed print mode, session flags, tool/thinking control, and
an argparse migration as *future* work (a "~10 day" plan). All of the **Core** set
and the headless session-continuation flags have since shipped (see Current State
above and `docs/CLI-PLAN.md` §3). The prioritized backlog and pi citations now live
in `CLI-PLAN.md`; this file is the corrected narrative record.
