# τ (tau) — a Python agent harness

A programmable coding-agent library, in the OpenAI-compatible tool-calling
family, with an optional TUI. τ started as a Python port of the TypeScript
[pi-mono](https://github.com/badlogic/pi-mono) project — pi is still read as
the reference implementation when porting or debugging — but τ now diverges
from pi deliberately in several places (see "Where τ diverges from pi"
below). It is not a 1:1 clone.

## Install

```bash
python -m venv venv && source venv/bin/activate

# Headless: the agent, the CLI, `tau -p`. 15 packages, no interface libraries.
pip install -e ./tau-llm -e ./tau-agent-core -e ./tau-coding-agent

# Add the interface. Needed for `tau` with no arguments.
pip install -e './tau-coding-agent[tui]'
```

Everything beyond the headless core is an extra, so a lightweight install stays
lightweight:

| Extra | Adds | Needed for |
|---|---|---|
| `ffwf-tau-coding-agent[tui]` | textual, rich | the interactive TUI; `tau -p` runs without it |
| `ffwf-tau-coding-agent[jmfts]` | ffwf-tau-jmfts | `--store jmfts` session storage |
| `ffwf-tau-agent-core[bus]` | nats-py | the `nats_bus` extension and the Tectum integration |
| `ffwf-tau-agent-core[testing]` | pytest | running the store contract suites against your own store |

Each one reports its own absence: asking for the TUI, `--store jmfts`, or the bus
without the matching extra fails with the install command, not a traceback.

### Two names for the command

The install puts **both `tau` and `ffwf-tau`** on your PATH. They are the same
program, not a link — each is a wrapper pip owns and removes on uninstall.

Type `tau`. Use `ffwf-tau` when you need certainty.

PyPI reserves distribution names but not command names, and there is no registry
for `tau`. At least one unrelated project ships its own `tau`, so in an
environment holding both, whichever installed last owns the name and neither pip
nor uv says a word about it. `ffwf-tau` cannot be taken, which makes it the right
name inside scripts, systemd units, and Dockerfiles.

If `tau --version` does not report τ, `ffwf-tau --version` will.

## Run

```bash
tau                                 # interactive TUI
tau -p "list the files here"        # headless: prints a transcript, exits
tau -p --mode json "..."            # headless: JSONL lifecycle events instead of text
tau --mode rpc                      # headless: drive τ as a subprocess over stdio (ndjson)
```

`tau -p` and `tau --mode rpc` both write and resume real sessions
(`~/.tau/sessions/`) — a headless run shows up in the TUI's sidebar and can
be picked up interactively later.

## Packages

Four independently-installable packages, `src/`-layout, stacked bottom-up:

| Package | Imports as | Depends on | Responsibility |
|---|---|---|---|
| `tau-llm/` | `tau_llm` | — | OpenAI-compatible provider, streaming events, message/tool types |
| `tau-agent-core/` | `tau_agent_core` | `tau_llm` | Agent loop, tools, sessions, extensions, RPC, compaction (headless) |
| `tau-coding-agent/` | `tau_coding_agent` | `tau_agent_core` | Textual TUI (a fork of "Parley") + CLI + headless run paths |
| `tau-jmfts/` | `tau_jmfts` | `tau_agent_core` | Optional JMFTS-backed session store; a peer of `tau-coding-agent`, not a dependency of it — loaded lazily only when selected by config |

`tau_agent_core` never imports `tau_coding_agent` — it stays usable headless,
embedded in another Python app, or driven over RPC without a Textual
dependency in sight.

## Architecture, briefly

- **One door.** Every input source — TUI keystrokes, `tau -p`, the SDK, an
  extension, an RPC client — funnels through `AgentSession.submit()`. A real
  concurrency policy (`multitask_strategy`: reject/enqueue/steer/rollback/fork)
  replaces what used to be a different ad-hoc answer per caller. See
  `docs/SUBMISSION-LIFECYCLE.md`.
- **Sessions are a tree, not a chat log.** Append-only JSONL; a
  `ConversationTree` walks `parent_id` chains to build model input. Fork,
  branch, rollback, and running a second agent at an earlier point in the
  conversation all fall out of that structure rather than being bolted on.
  See `docs/NODE-ADDRESSABLE-AGENTS.md`.
- **Extensions are plain Python modules** — `importlib`, no compile step, no
  TypeScript. The API surface covers tool/command registration,
  session-lifecycle and mutating hooks, per-extension config, and a
  headless-safe UI layer. See `docs/EXTENSIONS-WALKTHROUGH.md`.
- **τ can be driven as a subprocess, not just imported.** `--mode rpc` speaks
  a versioned JSON-RPC 2.0 protocol over stdio, documented in a
  machine-generated, test-locked reference. See `docs/RPC-PROTOCOL.md` and
  `docs/REMOTE-CONTROL.md`.

## Where τ diverges from pi, on purpose

τ began as a straight port; these are the places it has since taken its own
position rather than mirroring pi's:

- **Reasoning replay defaults to `"turn"`, not pi's `"all"`.** τ does not
  resend a model's chain-of-thought from every prior turn on every new call
  by default — measured at 72%→28% of payload size on a real transcript.
  pi's behavior is available as an option, not the default.
- **Fail-Early over silent fallback, applied harder than pi does.** A
  headless extension dialog (`confirm`/`select`/`input`) raises
  (`HeadlessDialogError`) unless a policy is explicitly configured, rather
  than guessing an answer. A missing API key raises `No API key for
  provider: …` rather than running with a fabricated key.
- **`register_flag`/`get_flag` were deleted**, not kept as a pi-compatible
  no-op — they never worked, and were replaced with a real per-extension
  config object (`~/.tau/config.json` → `extensions.<name>`, overridable
  with `--ext-config`).
- **τ does not chase pi's full `ctx.ui` surface.** Roughly half of pi's
  ~60 example extensions exist mainly to demo custom UI panels; τ ships the
  pieces with a clear extension use case (`notify`, `panel`, `form`,
  `set_status`) and treats the rest as a deliberate non-goal, not a gap.
- **`tau-jmfts` has no pi equivalent at all** — an optional session-storage
  backend built for a sibling project's retrieval needs, selected by config,
  never a hard dependency of the TUI or CLI.

## Documentation

| Document | Description |
|---|---|
| `CLAUDE.md` | Repo map, commands, the streaming event pipeline — start here when reading code |
| `ROADMAP.md` | What's shipped, what's open, with evidence citations |
| `docs/tau-llm.md` | Provider layer: types, streaming, the `Provider` interface |
| `docs/tau-agent-core.md` | Agent loop, sessions, SDK entry point, compaction |
| `docs/tau-coding-agent.md` | TUI + CLI, what's really Parley vs. what τ built instead |
| `docs/extensions.md` | The canonical extension API — hooks, discovery, real examples |
| `docs/SUBMISSION-LIFECYCLE.md` | The one-door submission model, in full |
| `docs/NODE-ADDRESSABLE-AGENTS.md` | Session-tree invariants; what makes forking/rollback safe |
| `docs/RPC-PROTOCOL.md` | Generated RPC reference (verbs, wire shapes) |
| `docs/REMOTE-CONTROL.md` | RPC design rationale and scope |
| `docs/PI-RPC-REPLACEMENT.md` | The Tectum integration contract — what one real RPC consumer needs, verb-by-verb |
| `docs/NATS-BUS-EXTENSION.md` | The `nats_bus` extension: loading it, its config, its per-verb tool contract |
| `docs/WIRE-CONTRACT.md` | The `TectumEvent` wire format the NATS bus extension speaks |
| `docs/PI-TO-TAU-COMPATIBILITY.md` | What maps from pi to τ, and what doesn't |
