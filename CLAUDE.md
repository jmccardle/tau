# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

τ ("tau") is a Python agent harness — a programmable coding-agent library with an optional TUI — ported from the TypeScript [pi-mono](https://github.com/badlogic/pi-mono) project. **pi is the source of truth**: a local checkout lives at `~/Development/pi`, and unless a divergence is intentional, τ's behavior should match pi's. When porting or debugging, read the corresponding pi file before changing τ.

## Monorepo layout

Three independently-installable packages, each `src/`-layout, stacked bottom-up:

| Package | Imports as | Depends on | Responsibility |
|---|---|---|---|
| `tau-ai/` | `tau_ai` | — | OpenAI-compatible provider, streaming events, message/tool types |
| `tau-agent-core/` | `tau_agent_core` | `tau_ai` | Agent loop, tools, sessions, extensions, compaction (headless) |
| `tau-coding-agent/` | `tau_coding_agent` | `tau_agent_core` | Textual TUI (a fork of "Parley") |

pi parity map: `tau-ai` ↔ `packages/ai`, `tau-agent-core` ↔ `packages/agent`, `tau-coding-agent` ↔ `packages/coding-agent`.

## Commands

```bash
# Setup (editable installs into the in-repo venv)
python -m venv venv && source venv/bin/activate
pip install -e ./tau-ai -e ./tau-agent-core -e ./tau-coding-agent

# Tests — pytest config (testpaths, asyncio_mode=auto) lives in the ROOT pyproject.toml,
# so run from the repo root:
pytest                                              # whole suite
pytest tau-ai/tests/test_openai_provider.py         # one file
pytest tau-ai/tests/test_openai_provider.py::test_x # one test
pytest -k tool_call                                 # by name substring

# Types (config in root pyproject.toml [tool.mypy])
mypy tau-ai/src tau-agent-core/src tau-coding-agent/src

# Run the TUI (console script defined in tau-coding-agent)
tau

# Run headless (no TUI) — root-level demo/eval driver of the agent loop
python run_agent_loop.py
```

There is no separate lint step (no ruff/flake8 config); `mypy` is the only static check. `async` tests need no `@pytest.mark.asyncio` decorator — `asyncio_mode=auto` is set globally.

## Architecture: the streaming event pipeline

The single most important thing to understand — because it spans four files and **two distinct event vocabularies** — is how one user turn flows from HTTP bytes to rendered tool calls:

1. **`tau-ai/.../providers/openai.py` → `OpenAICompletionsProvider.stream_chat`**
   Posts to `/chat/completions` with `stream:true`, parses the SSE lines, and accumulates deltas into an `_Accumulator`. Emits **τ-ai streaming events**: `TextDeltaEvent`, `ToolCallDeltaEvent`, `DoneEvent`, `ErrorEvent` (see `tau_ai/streaming.py`). The terminal `DoneEvent.final` is the authoritative `AssistantMessage` — its `ToolCall` blocks carry the parsed `arguments` that actually get executed.

2. **`tau-ai/.../client.py → stream_simple`** is the thin wrapper the loop calls.

3. **`tau-agent-core/.../agent_loop.py → AgentLoop.run`** consumes those streaming events and re-emits a *different* vocabulary, **`AgentEvent`s** (`tau_agent_core/events.py`): `agent_start`, `turn_start`, `message_start/update/end`, `tool_execution_start/end`, `turn_end`, `agent_end`. It pulls tool calls off the final `AssistantMessage` via `get_tool_calls()`, runs them (`_execute_tool_calls`, sequential or parallel), appends `toolResult` messages, and loops until there are no tool calls or `max_turns`.

4. **`tau-agent-core/.../agent_session.py` + `sdk.py`** wrap the loop with a session + an async event bus; `_resolve_tools(names)` maps `["read","write",...]` to `AgentTool` instances.

5. **`tau-coding-agent/.../backends.py → TauBackend`** adapts the AgentSession to Parley's `stream_chat(messages, callback)` contract by `subscribe()`-ing to the event bus, turning `message_update` text deltas into `callback(delta)` and collecting `toolCall`/`tool_execution_end` blocks for display.

6. **`tau-coding-agent/.../app.py → Parley`** is the Textual app; it renders streamed text at 30 Hz and shows tool-call / tool-result blocks.

Key consequence: a "tool call" is transformed several times (provider `ToolCall` → loop `message` dict block `{"type":"toolCall",...}` → backend `tool_calls_info` dict → TUI widget). When tool calling misbehaves, trace the `arguments` value through all four hops; the conversion of an `AssistantMessage`'s pydantic blocks to dicts happens via `model_dump()` at the loop boundary.

## Conventions & gotchas

- **OpenAI streams tool-call arguments as incremental *fragments***, one piece per chunk in `delta.tool_calls[].function.arguments`; they must be **concatenated** (pi: `openai-completions.ts:363`). Any logic that treats a chunk's `arguments` as the *complete cumulative* string is wrong and will corrupt the JSON. See `docs/TOOL-CALL-PARSING-BUG.md`.
- **"Fail Early" — avoid silent fallbacks.** Per the repo owner's standing rule, fallbacks/placeholders are an anti-pattern. The codebase currently violates this in several spots that actively hide bugs: `arguments = {"raw": <string>}` when JSON parsing fails (it should raise on a *complete* invalid payload), and `OPENAI_API_KEY` defaulting to `"sk-fake-key-for-testing"` (`openai.py:124`). Prefer raising over fabricating data. See `docs/CODE-QUALITY-NOTES.md`.
- Every source file cites the spec doc it implements (e.g. `Reference: PHASE-1-SUBPHASE-2.md`). The `docs/PHASE-*-SUBPHASE-*.md` files are the design spec; `docs/SUBPHASE-0.0.md` defines the core data contracts (Message/Tool/Event shapes).
- The default model in `~/.tau/config.json` is `local-llm`, pointing at a local OpenAI-compatible server (e.g. vLLM/Ollama). Local servers stream argument fragments aggressively, so they exercise the accumulation path that single-chunk cloud responses can mask.

## Reference docs (this repo)

- `docs/TOOL-CALL-PARSING-BUG.md` — root-cause diagnosis of the tool-call argument corruption (+ reproduction and fix).
- `docs/TOOL-CALL-PIPELINE.md` — the end-to-end tool-call streaming flow across all four files.
- `docs/CODE-QUALITY-NOTES.md` — ranked code-quality findings.
- `docs/PI-TO-TAU-COMPATIBILITY.md`, `docs/PI-TO-TAU-MIGRATION-GUIDE.md` — what maps from pi to τ.
- `docs/tau-ai.md`, `docs/tau-agent-core.md`, `docs/tau-coding-agent.md` — per-package design.
