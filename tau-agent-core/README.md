# ffwf-tau-agent-core

The runtime of **Tau**, a programmable coding agent harness. `tau_agent_core` is
the loop that drives a conversation: it calls the model, executes tools, appends
entries to a session, dispatches extension hooks, and compacts context when it
grows too large.

It is headless. No Textual, no stdout assumptions, and `tau_agent_core` never
imports `tau_coding_agent`. Embed it in your own program, drive it as a
subprocess over RPC, or run it under Tau's TUI.

Tau began as a Python port of the TypeScript project
[pi-mono](https://github.com/badlogic/pi-mono), which is still read as the
reference implementation when porting or debugging; it now diverges from pi
deliberately in several places.

## What is in it

- **`AgentSession`** — the object you hold. **The one door:** every input source
  — TUI keystrokes, `tau -p`, the SDK, an extension, an RPC client — funnels
  through `AgentSession.submit()`. `prompt()` is a thin wrapper that builds a
  `Submission` and calls `submit()`, not a second door. Concurrent submissions
  have a stated policy (`multitask_strategy`: reject, enqueue, steer, rollback,
  fork) rather than an answer improvised per caller.
- **`create_agent_session()`** — the SDK factory. Resolves a model name, built-in
  tool names, and extension callables into a working session.
- **Built-in tools** — `read`, `write`, `edit`, `bash`, `ls`, `grep`, `find`.
- **Sessions are a tree, not a chat log.** Entries are append-only;
  `ConversationTree` walks `parent_id` chains to build model input for the
  active leaf. Fork, branch, rollback, and running a second agent from an
  earlier point in the conversation all fall out of that structure instead of
  being bolted on.
- **Storage is a seam.** `SessionLog` and `SessionCatalog` are protocols. An
  in-memory log ships here, a file store ships with `ffwf-tau-coding-agent`, and
  a JMFTS-backed store ships with `ffwf-tau-jmfts`.
- **Extensions are plain Python modules** — `importlib`, no compile step, no
  manifest language. They register tools and commands, subscribe to lifecycle
  events, mutate what the loop is about to do, carry per-extension config, and
  can veto a tool call.
- **Compaction is LLM-backed with no fabricated-summary fallback.** A compaction
  error raises rather than silently truncating the conversation.
- **RPC** — a versioned JSON-RPC 2.0 command surface, so τ can be driven as a
  process rather than imported.
- **Export** — a session to Markdown or HTML.

## Why it is a separate package

The runtime and the terminal interface are different concerns with different
dependency footprints. Keeping them apart is what lets a server, a bot, or a
test harness run the same agent without a UI toolkit in sight.

## Install

```bash
pip install ffwf-tau-agent-core
```

Python 3.11 or newer. Pulls in `ffwf-tau-llm`.

Two extras, both off by default:

| Extra | Adds | Needed for |
|---|---|---|
| `ffwf-tau-agent-core[bus]` | `nats-py` | the built-in `nats_bus` extension, which publishes to NATS subjects |
| `ffwf-tau-agent-core[testing]` | `pytest` | importing `tau_agent_core.testing`, the store contract suites |

A plain install stays pytest-free and NATS-free.

## Example

```python
import asyncio
from tau_agent_core import create_agent_session


async def main():
    session = create_agent_session(
        model="gpt-4o",
        tools=["read", "grep", "bash"],
    )
    session.subscribe(lambda event: print(event.type))

    messages = await session.prompt("What files are in this directory?")
    for message in messages:
        print(message.get("role"), message.get("content"))


asyncio.run(main())
```

`prompt()` returns the messages produced by **this turn**, not the whole
conversation. The full history lives in the session's `SessionLog`.

`tools=` takes built-in name strings only, and raises on a name it does not
recognise. A custom `AgentTool` goes through the `AgentSession` constructor
directly, or is registered by an extension.

## Writing an extension

An extension is a module with a `register` callable that receives an
`ExtensionAPI`. This one refuses a destructive shell command before it runs:

```python
def permission_gate_tool_call(event, ctx):
    command = (event.get("input") or {}).get("command", "")
    if event["tool_name"] == "bash" and "rm -rf /" in command:
        return {"block": True, "reason": "destructive command refused"}
    return None


def register(api):
    api.on("tool_call", permission_gate_tool_call)
```

**`tool_call` is a mutating hook, not a notification.** Its return value is
honoured, and a `block` becomes an error tool result the model can react to.
Notify-only events such as `tool_execution_start` have their return value
discarded and cannot stop anything — a gate written against one prints a warning
and then lets the command run.

A module written this way loads from a path: `tau -e permission_gate.py`. The
repository's `examples/` directory holds around thirty working extensions.

## Testing your own store

`tau_agent_core.testing` ships the conformance suites for the two storage seams,
so a store written elsewhere can be held to the same contract. The contract is
the code, not a document.

```python
from tau_agent_core.testing import SessionCatalogContractTests, SessionLogContractTests


class TestMyLog(SessionLogContractTests):
    def make_log(self):
        return MyStore(...)


class TestMyCatalog(SessionCatalogContractTests):
    def make_catalog(self):
        return MyCatalog(...)
```

Install `ffwf-tau-agent-core[testing]` to import that module.

## Docs

- `docs/tau-agent-core.md` — design notes for this package.
- `docs/SUBMISSION-LIFECYCLE.md` — `submit()` and the concurrency strategies.
- `docs/NODE-ADDRESSABLE-AGENTS.md` — the session-tree invariants.
- `docs/extensions.md`, `docs/EXTENSIONS-WALKTHROUGH.md` — the extension API.
- `docs/REMOTE-CONTROL.md`, `docs/RPC-PROTOCOL.md` — driving τ as a subprocess.

Repository: <https://github.com/jmccardle/tau>

## The rest of Tau

| Distribution | Imports as | What it is |
|---|---|---|
| `ffwf-tau-llm` | `tau_llm` | the provider and streaming layer this sits on |
| `ffwf-tau-coding-agent` | `tau_coding_agent` | the `tau` command and the Textual TUI |
| `ffwf-tau-jmfts` | `tau_jmfts` | a JMFTS-backed session store |

MIT © Fight Fire with Fire Robotics, LLC
