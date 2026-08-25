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
    original — never replaced by a tree. It now mounts **closed**; ctrl+b
    opens it and the choice sticks.
  - `SessionPickerModal` (`session_picker.py`, added 0.9.3) also picks which
    session to open, and is what `--resume`, `/resume` and the command palette
    all reach — one handler, three bindings. It needs nothing from the app: a
    `SessionCatalog` and a cwd are its whole input. Fuzzy filter over name
    plus first and last message; Tab widens the scope from this cwd to all.
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
| `--continue` / `--session REF` / `--fork REF` | `-c` | mutually exclusive with each other and with `--resume` |
| `--resume` | `-r` | opens `SessionPickerModal` at TUI startup. Raises under `--print`, which has no screen to open a picker on — use `--continue` or `--session REF` there |
| `--no-context-files` | `-nc` | turn off `AGENTS.md`/`CLAUDE.md` discovery. Run-level, so a mid-session `/model` switch cannot hand the files back |
| `--fun` / `--no-fun` | | pick the startup tagline at random rather than always the same one. **On by default**, in a checkout and in every built artifact alike; `--no-fun` pins it to the first tagline. A `Parley` built in-process (tests, `testing.scenes`, `devshot`) defaults it OFF instead, which is what keeps rendered scenes byte-stable |
| `--theme NAME` | | TUI colour theme for **this run only** — see the config table below. Never written to `config.json`, which is the whole difference between it and picking a theme from the command palette. Not validated here: an unknown name reaches the app, which raises an error toast and starts in `mocha` |
| `--name` | `-n` | session display title |
| `--no-session` | | ephemeral, no persistence |
| `--thinking {off,minimal,low,medium,high,xhigh}` | | requires a reasoning-capable model |
| `--store {file,jmfts}` | | session backend for this run |
| `--session-dir DIR` | | file store only; default differs by mode (`~/.tau/sessions` vs. a private RPC temp dir) |
| `--import-session PATH` / `--export-session REF PATH` | | JMFTS store transfer, then exit |
| `--verbose` | | long-only — **`-v` is `--version`**, not verbose |
| `--help` / `--version` | `-h` / `-v` | |

## Colour themes

τ ships four: `mocha` (the default), `latte`, `gruvbox`, and `ansi`. A theme is
a palette, not a stylesheet — `parley.tcss` holds the structure and contains no
colour literal, and each theme supplies the 25 `$tau-*` role variables the sheet
names plus the Textual design tokens that colour the Footer, the scrollbars and
the tree cursor.

`ansi` is the odd one. Every colour in it is an ANSI *name*, so the 16 colours
already configured in the terminal emulator decide what τ looks like, and it
paints no backgrounds at all — `ansi_black` would be a black sidebar on a light
terminal and invisible on a dark one. Two costs come with that: the six-step
text ramp collapses to three, and `border` and `border-subtle` become the same
colour.

Three ways to select one, in increasing durability:

1. `tau --theme gruvbox` — this run only. Never written to disk.
2. The command palette (`ctrl+p`, "Theme: …") — swaps live and **saves**.
3. `"theme": "gruvbox"` in `~/.tau/config.json` — the standing choice.

A theme that cannot be loaded does not stop τ from starting. Each failure — a
name nothing answers to, a `theme` key that is not a string, a file in
`~/.tau/themes` that will not parse — raises an error toast naming the problem,
and the app runs in `mocha`. A broken file named after a built-in leaves the
built-in standing, and one broken file does not cost the others.

### Writing one

Drop a `<name>.json` in `~/.tau/themes/`. The file's stem is the theme's name,
and a file named after a built-in replaces it.

```json
{
  "extends": "mocha",
  "palette": { "bg": "#000000", "bg-alt": "#050508" },
  "textual": { "background": "#000000" }
}
```

`extends` names a built-in and supplies both halves. `palette` overrides τ's
colours by the role names in `themes.TAU_PALETTE_KEYS`. The optional `textual`
block overrides Textual's own design tokens, for the widgets `parley.tcss` does
not reach — `"dark": false` there is what sends Textual's built-in widgets down
their light branch, so a light palette does not end up under a dark Footer.

## Other config keys

There is no complete `config.json` reference yet. `models`, `default_model`,
`system_prompt`, `session_store` and `theme` are the top-level keys the TUI
reads directly; most CLI flags have a matching key, and the packaged
`tau_default_config.json` is what a first run writes.

## Dependencies

`ffwf-tau-agent-core` is the only hard one. `textual` and `rich` are the
`[tui]` extra, and `cli.py:519` is the sole import of the Textual app, so
`tau -p` and `tau --mode rpc` run a full turn with neither installed. `typer`
was removed at 0.9.2; argument parsing is `argparse`.
