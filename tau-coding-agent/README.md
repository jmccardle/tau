# ffwf-tau-coding-agent

The interface of **Tau**, a programmable coding agent harness: the `tau`
command, its Textual terminal UI, and the headless run paths behind `tau -p` and
`tau --mode rpc`.

Tau began as a Python port of the TypeScript project
[pi-mono](https://github.com/badlogic/pi-mono), which is still read as the
reference implementation when porting or debugging; it now diverges from pi
deliberately in several places.

## Install

```bash
pip install ffwf-tau-coding-agent            # headless: the CLI, `tau -p`, `--mode rpc`
pip install 'ffwf-tau-coding-agent[tui]'     # add the interactive TUI
pip install ffwf-tau                         # the same thing, under the guessable name
```

Python 3.11 or newer. Pulls in `ffwf-tau-agent-core`, and through it
`ffwf-tau-llm`. Note the `ffwf-` prefix: `tau`, `tau-llm`, and `tau-ai` on PyPI
are unrelated projects.

`ffwf-tau` is a metapackage holding no code; it depends on
`ffwf-tau-coding-agent[tui]` and nothing else. Use it when you want one short
name, and this distribution when you want to choose extras.

| Extra | Adds | Needed for |
|---|---|---|
| `ffwf-tau-coding-agent[tui]` | `textual`, `rich` | the interactive TUI. `tau -p` runs a full turn without it |
| `ffwf-tau-coding-agent[jmfts]` | `ffwf-tau-jmfts` | `--store jmfts` session storage |

**The interface is an extra, not a base dependency.** A headless install is 15
packages and 13 MB; adding `[tui]` makes it 27 packages and 31 MB. Both extras
report their own absence — asking for the TUI or for `--store jmfts` without the
matching extra fails with the install command you need, not a traceback.

### Two names for one command

The install puts **both `tau` and `ffwf-tau`** on your PATH. They are the same
entry point, not a link: each is a wrapper pip owns and removes on uninstall.

Type `tau`. PyPI reserves distribution names but not command names, and at least
one unrelated project ships its own `tau`; in an environment holding both,
whichever installed last owns the name and neither pip nor uv says a word.
`ffwf-tau` cannot be taken, which makes it the right name inside scripts,
systemd units, and Dockerfiles. If `tau --version` does not report τ,
`ffwf-tau --version` will.

## Run

```bash
tau                                 # interactive TUI
tau -p "list the files here"        # headless: print a transcript, exit
tau -p --mode json "..."            # headless: JSONL lifecycle events instead of text
tau --mode rpc                      # drive τ as a subprocess over stdio (JSON-RPC 2.0)
tau -e permission_gate.py           # load an extension from a path
tau --help                          # the exact contract; treat this page as the stale one
```

`tau -p` and `tau --mode rpc` both write and resume real sessions under
`~/.tau/sessions/`, so a headless run appears in the TUI's sidebar and can be
picked up interactively later. `--no-session` means nothing is persisted.

Flags worth knowing: `-m/--model`, `-t/--tools` and `-nt/--no-tools`,
`-e/--extension PATH`, `--thinking {off,minimal,low,medium,high,xhigh}`,
`--max-turns N`, `-c/--continue`, `--session REF`, `--fork REF`,
`--store {file,jmfts}`.

**There is no turn limit by default.** An agent run ends when the model stops
calling tools, a tool terminates it, an extension's budget guard aborts it, or
you press Escape. `--max-turns N` adds a ceiling for one run, and
`"max_turns": N` in `~/.tau/config.json` sets a standing one.

## Configure

On first run τ writes `~/.tau/config.json` from a default shipped inside the
package. It holds the `models` map that `--model` resolves names against, plus
per-extension config and the `session_store` block. A model entry names the
provider, the base URL, and the context window, so pointing τ at a local
OpenAI-compatible server (vLLM, llama.cpp, Ollama) is a config edit rather than
a code change.

## The TUI

Built on [Textual](https://textual.textualize.io/). Streamed text renders at
30 Hz; tool calls and their results appear as their own blocks.

| Key | Action |
|---|---|
| `Ctrl+Enter` | send the message |
| `Ctrl+B` | toggle the session sidebar |
| `Ctrl+N` | new chat |
| `Ctrl+G` | browse the conversation tree |
| `Ctrl+Z` | roll back the last turn |
| `Ctrl+R` / `Ctrl+T` | show or hide reasoning / tool blocks |
| `Ctrl+E` | extensions |
| `Ctrl+P` | command palette |
| `Escape` | cancel the current generation |

**The sidebar and the tree browser are two different things.** The sidebar picks
*which session to open* — a flat list grouped by date. `Ctrl+G` opens a browser
over the branch structure *inside the current conversation*, where you navigate
to an earlier node, summarise a branch, or elide a span. Typed commands cover
the rest: `/compact`, `/tree`, `/fork`, `/extensions`.

## Why it is a separate package

Everything here is presentation and process management. The agent lives in
`ffwf-tau-agent-core`, which has no Textual dependency and never imports this
package. `ffwf-tau-jmfts` is a peer rather than a dependency: `--store jmfts`
imports it lazily and fails at startup if it is absent, never falling back to
files without saying so.

If you want the agent rather than the interface, use the runtime directly:

```python
import asyncio
from tau_agent_core import create_agent_session


async def main():
    session = create_agent_session(model="gpt-4o", tools=["read", "bash"])
    print(await session.prompt("What is in this directory?"))


asyncio.run(main())
```

## Docs

- `docs/tau-coding-agent.md` — design notes for this package.
- `docs/CLI-PLAN.md` — the flag set and its status.
- `docs/REMOTE-CONTROL.md`, `docs/RPC-PROTOCOL.md` — `--mode rpc`.
- `docs/EXTENSIONS-WALKTHROUGH.md` — writing an extension to load with `-e`.

Repository: <https://github.com/jmccardle/tau>

## The rest of Tau

| Distribution | Imports as | What it is |
|---|---|---|
| `ffwf-tau-llm` | `tau_llm` | the provider and streaming layer |
| `ffwf-tau-agent-core` | `tau_agent_core` | the agent loop, tools, sessions, extensions |
| `ffwf-tau-jmfts` | `tau_jmfts` | a JMFTS-backed session store |

MIT © Fight Fire with Fire Robotics, LLC
