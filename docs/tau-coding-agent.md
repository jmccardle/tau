# τ-coding-agent Design — TUI (fork of Parley)

## Scope

τ-coding-agent is the interactive terminal interface, plus the `tau` CLI
that also drives headless (`-p`) and RPC (`--mode rpc`) runs. It's a fork
of a prior Textual app called Parley, kept where Parley's design already
fit and replaced where τ's agent model needed something Parley didn't have.

## What's genuinely still Parley

- **30Hz streaming throttle** — carried forward as-is; still the right
  answer for not thrashing the terminal on token-by-token deltas.
- **Catppuccin Mocha palette** — real, hardcoded hex values throughout the
  single stylesheet `parley.tcss` (e.g. `#1e1e2e` base, `#89dceb` on
  user-message borders, `#f38ba8` on errors). This is true at the color
  level; it is **not** a swappable theme system — there is no `themes/`
  directory and no second theme anywhere in the source.
- **Command palette (Ctrl+P)** — Textual's built-in, listing τ's own
  commands (`app.py`'s `get_system_commands`).

## What τ actually built instead of the original plan

An early design sketched a `widgets/` directory with ten small files
(`tool_call_widget.py`, `session_tree.py`, `footer.py`, an `@`-file-ref /
`!bash` input bar, and so on). None of that file layout exists. What's
real:

- **Rendering** lives in a flat `chat_widgets.py` (three classes:
  `ReasoningRegion`, `ToolBox`, `ExchangeBox`) plus a large set of widget
  and modal classes defined directly in `app.py` — `ExtensionStatusBar`,
  `LaneStrip`, `ExtensionPanel`, `SessionTreeModal`, `ChatSidebar`,
  `ChatDisplay`, `ChatInput`.
- **Input** is a plain `textual.widgets.TextArea` (`ChatInput`) with
  up/down history navigation and `Ctrl+Enter` to submit. There is no `@`
  fuzzy file reference, no `!command`/`!!command` bash escape, and no tab
  completion for paths — none of these were built. Multi-line entry is
  just `TextArea`'s own default Enter-inserts-newline behavior, not a
  τ-specific Shift+Enter feature.
- **Session browsing is two distinct, unrelated things** — an earlier
  design conflated them into one "session tree":
  - `ChatSidebar` picks *which session to open*. It's still a flat list
    grouped by date ("Today"/"Yesterday"/"Older"), exactly like Parley's
    original — never replaced by a tree.
  - `SessionTreeModal` (opened with `Ctrl+G`) is a real `Tree` widget, but
    it browses the **branch structure inside the current conversation** —
    message/compaction/branch/navigate nodes, with a `◀ current` marker
    on the active leaf — to pick a point for a follow-up action
    (`TreeModeModal`: navigate, summarize a branch, summarize with custom
    instructions, or elide a span). It has no bookmarks/labels, doesn't
    show compaction summaries inline (only after you pick a node), and
    isn't where fork/clone live — `/fork` is a separate command, dispatched
    through `AgentSession.submit()`, not reachable from this modal.
- **Status display** is the window `Header`'s subtitle — model name plus
  one aggregate label (`"{N} tools · {tokens} tok"`, a single summed token
  count). There is no separate footer widget, no context-window
  percentage anywhere, and no session-name or thinking-level indicator in
  any status area. `ExtensionStatusBar` and `LaneStrip` exist but serve
  different purposes (extension-declared status slots; which forked/bus
  lanes are currently streaming), not session stats.

## Event flow

`ChatInput` submit → `app.py`'s `on_input_submitted` → `TauBackend`'s
`submit_turn` → `AgentSession.submit()` (the same one door every other
input source uses) → a persistent render subscription
(`subscribe_render`/`RenderRouter`) turns backend events into widget
updates. The shape an earlier sketch drew (submit → agent loop → event
handler → widget) still holds; only the exact names changed.

## Modes

Interactive (default), headless print (`-p`/`--print`), headless JSON
(`--mode json`), and RPC (`--mode rpc`, a persistent JSON-RPC 2.0 server
over stdio — see `docs/REMOTE-CONTROL.md`).

## CLI flags

`tau --help` is the authoritative source — this table is a snapshot and
*will* drift if hand-maintained separately, the same way an earlier
version of this reference did (three of its flag claims were wrong: a
missing `--thinking` level, a `-r`/`--resume` description that doesn't
hold headlessly, and a `-v` alias claimed for two different flags). Treat
disagreements between this table and `tau --help` as this table being
stale.

| Flag | Short | Notes |
|---|---|---|
| `--print` | `-p` | run one turn headlessly, print, exit |
| `--mode {text,json,rpc}` | | headless output format; `rpc` doesn't combine with `--print` |
| `--model` | `-m` | config key or `provider/id` shorthand |
| `--provider` | | long-only |
| `--tools`/`--no-tools` | `-t`/`-nt` | allowlist / offer the model **zero** tools — built-ins *and* extension-registered ones. Extensions still load: hooks, commands, injections and subscriptions are untouched, only callable tools are withheld |
| `--exclude-tools` | `-xt` | denylist (built-ins only) |
| `--no-builtin-tools` | `-nbt` | drops the built-in set; extension-registered tools survive and are offered |
| `--extension PATH` (repeatable) | `-e` | explicit load, always runs even under `--no-extensions` |
| `--no-extensions` | `-ne` | disables discovery only |
| `--bus` | | declare this run may reach a message bus, so `TOUCHES_BUS` extensions (e.g. `nats_bus`) are allowed to load |
| `--ext-config NAME.KEY=VALUE` (repeatable) | | per-extension config override, CLI > `config.json` |
| `--ui-defaults METHOD=ANSWER,...` | | headless dialog auto-answers; otherwise a headless dialog raises. `--print` only |
| `--system-prompt` / `--append-system-prompt` (repeatable) | | |
| `--continue` / `--resume` / `--session REF` / `--fork REF` | `-c` / `-r` | mutually exclusive. **`--resume` is TUI-sidebar-only — it raises at the CLI, it does not open a picker headlessly** |
| `--name` | `-n` | session display title |
| `--no-session` | | ephemeral, no persistence |
| `--thinking {off,minimal,low,medium,high,xhigh}` | | requires a reasoning-capable model |
| `--store {file,jmfts}` | | session backend for this run |
| `--session-dir DIR` | | file store only; default differs by mode (`~/.tau/sessions` vs. a private RPC temp dir) |
| `--import-session PATH` / `--export-session REF PATH` | | JMFTS store transfer, then exit |
| `--verbose` | | long-only — **`-v` is `--version`**, not verbose |
| `--help` / `--version` | `-h` / `-v` | |

## Dependencies

`textual>=0.47`, `tau-agent-core` (local), `typer`.
