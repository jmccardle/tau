# τ (tau-agent-core) — Python Agent Harness

A programmable coding agent harness inspired by [pi-agent](https://github.com/badlogic/pi-mono), but designed from the ground up for Python.

> **Goal**: A headless-first agent library with an optional TUI. Python-native extensions.

## Quick Overview

```
tau-ai/               → OpenAI provider (streaming, tool calls, types)
tau-agent-core/       → Agent loop, tools, sessions, extensions (headless)
tau-coding-agent/     → Interactive TUI (fork of Parley, built on τ-agent-core)
```

## Key Design Decisions

1. **OpenAI-first, OpenAI-compatible only** — The OpenAI API is the lingua franca of LLM tool calling. Everything else is a convenience.

2. **Agent library first, TUI second** — τ-agent-core works perfectly headless. The TUI is one consumer. You can embed τ in any Python app.

3. **Python extensions** — Users write Python modules that register tools, intercept events, and persist state. No TypeScript, no jiti, no compilation. Just `importlib`.

4. **Session tree over flat chat** — Sessions are JSONL files with a tree structure (parent_id), enabling fork, branch, and compaction.

5. **30Hz TUI rendering** — Parley's performance philosophy carried forward: streaming updates throttled to 30Hz to prevent UI thrashing.

## Documentation

| Document | Description |
|----------|-------------|
| [MONOREPO-STRUCTURE.md](MONOREPO-STRUCTURE.md) | Package layout, dependencies, naming |
| [ARCHITECTURE.md](ARCHITECTURE.md) | High-level architecture and data flow |
| [docs/tau-ai.md](docs/tau-ai.md) | τ-ai design (OpenAI provider) |
| [docs/tau-agent-core.md](docs/tau-agent-core.md) | τ-agent-core design (agent loop, tools) |
| [docs/tau-coding-agent.md](docs/tau-coding-agent.md) | τ-coding-agent design (TUI) |
| [docs/extensions.md](docs/extensions.md) | Extension system design |
| [docs/IMPLEMENTATION-PLAN.md](docs/IMPLEMENTATION-PLAN.md) | Phased implementation plan |
| [docs/tau-api-reference.md](docs/tau-api-reference.md) | API reference for all public interfaces |
| [docs/PI-TO-TAU-COMPATIBILITY.md](docs/PI-TO-TAU-COMPATIBILITY.md) | What maps from pi to τ |

## Getting Started (When Ready)

```bash
git clone [...TBD...]

# Create virtual environment
python -m venv venv && source venv/bin/activate

# Install packages in development mode
pip install -e ./tau-ai
pip install -e ./tau-agent-core
pip install -e ./tau-coding-agent

# Run the agent
tau "What files are in this directory?"
```
