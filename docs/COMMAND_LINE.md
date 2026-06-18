# τ Command Line Interface Plan

## Overview

This document plans the τ CLI flags and behaviors, mapping `pi`'s rich flag set to τ's implementation priorities. The goal is a CLI that feels natural for an agent harness: model selection, tool control, session management, and multiple output modes at minimum.

## Current State

| Flag | τ Status | Notes |
|------|----------|-------|
| `--model, -m` | ✅ Implemented | Sets model name |
| `--provider, -p` | ✅ Implemented | Sets provider name |
| `--session, -s` | ✅ Implemented | Session name (TUI-only) |
| `--output, -o` | ✅ Implemented | `tui`, `json` |
| `--verbose, -v` | ✅ Implemented | Debug output |
| `--config` | ✅ Implemented | Config file path |
| `--cwd` | ✅ Implemented | Working directory |
| `--context-window` | ✅ Implemented | Model context window |
| `--max-tokens` | ✅ Implemented | Max response tokens |
| **Everything else from pi** | ❌ Missing | ~30+ flags |

## Flag Categories (by urgency)

### P0 — Core Agent Harness Behavior (Must Have for MVP)

These are the absolute minimum. Without them, τ cannot function as a usable agent harness.

| Flag | Pi Equivalent | Purpose | Priority |
|------|--------------|---------|----------|
| `--model` | `--model` | Select model (supports `provider/id` shorthand) | ✅ Done |
| `--provider` | `--provider` | Override provider (used with model shorthand) | ✅ Done |
| `-p, --print` | `--print, -p` | **Non-interactive mode** — process prompt and exit | **🔴 URGENT** |
| `--continue` | `--continue, -c` | **Continue previous session** — essential UX for agent harness | **🔴 URGENT** |
| `--tools` | `--tools, -t` | **Tool allowlist** — critical for security/control | **🔴 URGENT** |
| `--thinking` | `--thinking` | **Thinking/reasoning level** — core agent behavior | **🔴 URGENT** |

**Why `-p` (print mode) is critical:**
- Enables scripting and automation (`tau -p "List files in src/"`)
- Required for CI/CD pipelines
- Core testing/evaluation workflow (see `run_agent_loop.py`)
- pi users consider this a first-class mode, equal to interactive

**Why `--continue` is critical:**
- Agent conversations are iterative by nature
- pi's `-c` is one of its most-used flags
- τ already has session management in tau-agent-core; just needs CLI bridge
- Without it, every invocation starts a blank slate

**Why `--tools` is critical:**
- Security: restrict agent to read-only mode (`--tools read,ls`)
- Testing: limit tools for evals
- Custom workflows: enable only needed tools
- pi uses this heavily in examples (`--tools read,grep,find,ls`)

**Why `--thinking` is critical:**
- Maps to `reasoning_effort` in OpenAI
- Core model behavior parameter
- pi has 6 levels: off, minimal, low, medium, high, xhigh

### P1 — Essential Usability (High Value, Not Blocking)

These make τ feel polished and competitive with pi for day-to-day use.

| Flag | Pi Equivalent | Purpose | Priority |
|------|--------------|---------|----------|
| `--session, -s` | `--session` | Specific session file or ID | Already exists (TUI-only), needs expansion |
| `--fork` | `--fork` | Fork session into new branch | 🔶 Important |
| `--resume` | `--resume, -r` | Interactive session picker to resume | 🔶 Important |
| `--no-session` | `--no-session` | Ephemeral session (no persistence) | 🔶 Important |
| `--model, -m` (expanded) | `--model` | Support `:high` thinking shorthand (`sonnet:high`) | 🔶 Important |
| `--system-prompt` | `--system-prompt` | Override system prompt from CLI | 🔶 Important |
| `--append-system-prompt` | `--append-system-prompt` | Append context files to system prompt | 🔶 Important |
| `--no-builtin-tools` | `--no-builtin-tools, -nbt` | Disable built-in tools, keep extensions | 🔶 Important |
| `--no-tools` | `--no-tools, -nt` | Disable ALL tools (read-only agent) | 🔶 Important |

**Why `--fork` matters:**
- Core session branching concept in pi
- τ's session manager already supports this; CLI just needs to expose it
- Enables "try a different approach" workflows

**Why `--resume` matters:**
- Interactive mode with session picker
- pi's `-r` opens a TUI to choose sessions
- τ already has session listing; just needs the flag + picker

**Why `--no-session` matters:**
- Quick experiments without polluting session history
- Testing and evals
- `tau -p --no-session "what's 2+2?"`

### P2 — Nice to Have (Low Effort, High Polish)

| Flag | Pi Equivalent | Purpose | Priority |
|------|--------------|---------|----------|
| `--models` | `--models` | Limit model cycling patterns (TUI Ctrl+P) | 🟢 Nice to have |
| `--list-models` | `--list-models` | List available models | 🟢 Nice to have |
| `--session-dir` | `--session-dir` | Custom session storage directory | 🟢 Nice to have |
| `--verbose` | `--verbose` | Verbose startup | ✅ Done |
| `--offline` | `--offline` | Disable network operations | 🟢 Nice to have |
| `--export` | `--export` | Export session to HTML/markdown | 🟢 Nice to have |
| `--extension, -e` | `--extension, -e` | Load extension file | 🟢 Nice to have |
| `--skill` | `--skill` | Load skill file | 🟢 Nice to have |
| `--theme` | `--theme` | Load theme file | 🟢 Nice to have |
| `--version` | `--version, -v` | Version | 🟢 Nice to have |

### P3 — Out of Scope for MVP

| Flag | Pi Equivalent | Purpose | Why Out of Scope |
|------|--------------|---------|-----------------|
| `--api-key` | `--api-key` | Pass API key via CLI | **Security risk** — env vars are standard; storing keys on CLI is dangerous |
| `--prompt-template` | `--prompt-template` | Prompt templates | Extension system covers this; lower priority |
| `--no-themes` | `--no-themes` | Disable themes | Only 1 theme (catppuccin) in MVP |
| `--no-skills` | `--no-skills` | Disable skills | Skills not in MVP |
| `--no-extensions` | `--no-extensions, -ne` | Disable extensions | Extensions not in MVP |
| `--no-prompt-templates` | `--no-prompt-templates` | Disable templates | Templates not in MVP |
| `--no-context-files` | `--no-context-files, -nc` | Disable context files | Context files (AGENTS.md) are in MVP |
| `--mode rpc` | `--mode rpc` | RPC mode | Phase 6 (separate doc) |

## Implementation Plan

### Phase A: Print Mode (`-p`) — The Biggest Gap

**Status:** ❌ Missing | **Effort:** ~1 day | **Impact:** Very High

This is the single biggest gap between τ and pi. `pi -p "prompt"` is the most common programmatic interaction pattern.

**Implementation:**

```
tau -p "What files are in src/?"
```

Behavior:
1. Parse CLI args (minimal set: model, provider, tools, thinking, session, continue)
2. Build agent session (discover extensions, tools, context files)
3. If `--continue`, load last session from current working directory
4. Send prompt text as user message
5. Run agent loop (with tool calls, streaming output)
6. Stream output to stdout (text mode) or JSONL (json mode)
7. Save session
8. Exit

**Output modes:**
- `text` (default): Plain text output with tool call annotations
- `json`: Each event as a JSON line on stdout

**Example text output:**
```
[tool:read] File: package.json
{"name": "tau"}

[tool:bash] Command: ls -la
total 42

I see two files in the project: package.json and README.md. Would you like me to
read either of them?

[done] 42 tokens, 12 tool calls
```

**Example JSON output (`-p --mode json`):**
```json
{"type": "agent_start"}
{"type": "message_start", "role": "user", "content": "What files are in src/?"}
{"type": "message_update", "role": "assistant", "delta": "Let me check"}
{"type": "message_end", "role": "assistant", "content": "..."}
{"type": "tool_execution_start", "tool_name": "bash", "args": {"command": "ls -la"}}
{"type": "tool_execution_end", "tool_name": "bash", "result": "..."}
{"type": "agent_end", "tokens": {"input": 100, "output": 42}}
```

**Design decisions:**
- No TUI widgets at all in print mode
- Tool calls shown inline (not collapsible)
- Stderr for errors, warnings, progress
- Exit code 0 on success, 1 on error
- Stdin pipe support: `cat file.py | tau -p "Review this"`

### Phase B: Session Management Flags

**Status:** Partially done (exists as TUI-only) | **Effort:** ~2 days | **Impact:** High

| Flag | Implementation |
|------|---------------|
| `--continue, -c` | Load last session, append new prompt, continue loop |
| `--fork <id>` | Fork session at entry ID (or latest) into new file |
| `--resume, -r` | Open session picker in TUI, then resume chosen session |
| `--session <path\|id>` | Load specific session file (full path or partial UUID) |
| `--no-session` | Set `ephemeral=True`, don't persist to disk |
| `--session-dir <dir>` | Override `~/.tau/sessions/` |

**Implementation:**
- All session operations already exist in `tau_agent_core.session_manager`
- CLI just needs to call `SessionManager.list()`, `SessionManager.fork()`, etc.
- For `--resume`, need a simple interactive session picker (can reuse TUI or build minimal)
- For `--fork` in print mode: fork session, run prompt on fork, save as new file, print result

### Phase C: Tool & Thinking Flags

**Status:** ❌ Missing | **Effort:** ~1 day | **Impact:** High

| Flag | Implementation |
|------|---------------|
| `--tools, -t <list>` | Filter `create_all_tools(cwd)` to allowlist |
| `--no-tools, -nt` | Pass empty tool list (LLM gets no tool definitions) |
| `--no-builtin-tools, -nbt` | Skip built-in tool registration, keep extensions |
| `--thinking <level>` | Map to model's `reasoning_effort` parameter |

**Thinking level mapping (OpenAI):**
```python
THINKING_MAP = {
    "off": None,         # No reasoning
    "minimal": "low",    # Minimal effort
    "low": "low",
    "medium": "medium",  # Medium effort
    "high": "high",
    "xhigh": "high",     # Max available
}
```

### Phase D: System Prompt Flags

**Status:** ❌ Missing | **Effort:** ~0.5 day | **Impact:** Medium

| Flag | Implementation |
|------|---------------|
| `--system-prompt <text>` | Replace default system prompt entirely |
| `--system-prompt <file>` | Load from file if argument contains path separators |
| `--append-system-prompt <text>` | Append text to system prompt (repeatable) |
| `--append-system-prompt <file>` | Append file contents to system prompt |

**Precedence:**
1. `--system-prompt` (full replacement) — highest priority
2. Default system prompt + `--append-system-prompt` additions
3. Context file discovery (AGENTS.md, .tau/SYSTEM.md)

### Phase E: Model Shorthand & Extensions

**Status:** Partially done | **Effort:** ~1 day | **Impact:** Medium

**Model shorthand** (`--model` expanded):
```python
# These should all work:
--model gpt-4o                    # plain model ID
--model openai/gpt-4o             # provider/model
--model gpt-4o:high              # model:thinking
--model openai/gpt-4o:high       # provider/model:thinking

# Parsing logic:
if ":" in model:
    model_name, thinking_level = model.rsplit(":", 1)
else:
    model_name = model
    thinking_level = None

if "/" in model_name:
    provider, model_id = model_name.split("/", 1)
else:
    provider = provider or "openai"  # default
    model_id = model_name
```

**Extension loading flags** (`--extension`):
```python
--extension ./my_ext.py           # Load single file
--no-extensions                   # Disable auto-discovery (explicit -e still works)
--skill ./my_skill.md             # Load skill file
--theme ./my_theme.tcss           # Load theme
```

These are lower priority since extension system is Phase 3, but the flags should be parsed early so they don't conflict with core flags.

## Flag Parsing Architecture

τ should use `argparse` for structured, self-documenting CLI parsing. The current `parse_cli_args()` manual parser is fragile and doesn't support:
- `--continue` (flag with no value)
- `--tools a,b,c` (comma-separated list)
- `--append-system-prompt` (repeatable)
- Auto-generated help text

**Proposed argparse structure:**

```python
import argparse

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tau",
        description="τ — programmable agent harness",
        add_help=True,
    )

    # Model selection
    parser.add_argument("--model", "-m", help="Model ID or provider/id[:thinking_level]")
    parser.add_argument("--provider", help="Provider name (used with --model)")
    parser.add_argument("--models", help="Comma-separated model patterns for cycling")
    parser.add_argument("--list-models", nargs="?", const="", help="List available models")
    parser.add_argument("--thinking", choices=["off", "minimal", "low", "medium", "high", "xhigh"],
                        help="Thinking/reasoning level")

    # API key (low priority — recommend env vars instead)
    parser.add_argument("--api-key", help="API key (use env vars instead)")

    # Output mode
    parser.add_argument("--mode", choices=["text", "json", "rpc"], default="text",
                        help="Output mode")
    parser.add_argument("--print", "-p", action="store_true",
                        help="Non-interactive: process prompt and exit")

    # Session management
    parser.add_argument("--continue", "-c", action="store_true",
                        help="Continue previous session")
    parser.add_argument("--resume", "-r", action="store_true",
                        help="Pick a session to resume")
    parser.add_argument("--session", help="Specific session file or partial UUID")
    parser.add_argument("--fork", help="Fork session into new branch")
    parser.add_argument("--no-session", action="store_true",
                        help="Don't persist session")
    parser.add_argument("--session-dir", help="Session storage directory")

    # System prompt
    parser.add_argument("--system-prompt", help="Replace system prompt (file or text)")
    parser.add_argument("--append-system-prompt", action="append",
                        help="Append text or file to system prompt (repeatable)")

    # Tool control
    parser.add_argument("--no-tools", "-nt", action="store_true",
                        help="Disable all tools")
    parser.add_argument("--no-builtin-tools", "-nbt", action="store_true",
                        help="Disable built-in tools")
    parser.add_argument("--tools", "-t",
                        help="Comma-separated tool allowlist")

    # Extension/skill/theme loading
    parser.add_argument("--extension", "-e", action="append",
                        help="Load extension file (repeatable)")
    parser.add_argument("--no-extensions", "-ne", action="store_true",
                        help="Disable extension discovery")
    parser.add_argument("--skill", action="append", help="Load skill file (repeatable)")
    parser.add_argument("--no-skills", action="store_true", help="Disable skills")
    parser.add_argument("--prompt-template", action="append", help="Load template (repeatable)")
    parser.add_argument("--no-prompt-templates", action="store_true", help="Disable templates")
    parser.add_argument("--theme", action="append", help="Load theme file (repeatable)")
    parser.add_argument("--no-themes", action="store_true", help="Disable themes")
    parser.add_argument("--no-context-files", "-nc", action="store_true",
                        help="Disable AGENTS.md discovery")

    # Misc
    parser.add_argument("--export", help="Export session to file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--offline", action="store_true", help="Disable network operations")
    parser.add_argument("--cwd", help="Working directory")
    parser.add_argument("--config", help="Config file path")
    parser.add_argument("--context-window", type=int, help="Model context window")
    parser.add_argument("--max-tokens", type=int, help="Max response tokens")

    # Positional: @files... and messages...
    parser.add_argument("files", nargs="*", default=[],
                        help="@files to include, then messages")

    return parser
```

**Positional arguments** (pi's `@files... [messages...]`):
- Files starting with `@` are read and included as file attachments in the prompt
- Remaining positional args are user messages (can be multiple)
- Example: `tau @README.md "What does this project do?"`

**Stdin pipe support:**
- If stdin is a pipe (not a terminal) and no positional args: read stdin as the prompt
- `cat file.py | tau -p "Review this code"` — stdin becomes first message
- `cat file.py | tau -p` — stdin IS the prompt

## Environment Variables (already match pi)

τ should support the same env vars as pi for maximum compatibility:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key (tau MVP only uses this) |
| `TAU_DIR` | Override `~/.tau` base directory |
| `TAU_SESSION_DIR` | Override session directory |
| `TAU_MODEL` | Default model (overridden by `--model`) |
| `TAU_PROVIDER` | Default provider (overridden by `--provider`) |
| `TAU_THINKING` | Default thinking level |
| `TAU_TOOLS` | Default tool allowlist (comma-separated) |
| `TAU_SYSTEM_PROMPT` | Default system prompt |
| `TAU_CWD` | Default working directory |
| `TAU_PRINT` | Equivalent to `-p` flag |
| `TAU_CONTINUE` | Equivalent to `-c` flag |

## Help Text Example

```
Usage: tau [OPTIONS] [@files...] [messages...]

τ — programmable agent harness

Options:
  -m, --model TEXT          Model ID or provider/id[:thinking_level]
  --provider TEXT            Provider name
  --models TEXT              Model patterns for cycling (supports globs)
  --thinking [off|minimal|low|medium|high|xhigh]
                             Reasoning/thinking level
  -p, --print                Non-interactive: process prompt and exit
  --mode [text|json|rpc]     Output mode (default: text)
  -c, --continue             Continue previous session
  -r, --resume               Pick session to resume
  --session PATH             Specific session file or partial UUID
  --fork PATH                Fork session into new branch
  --no-session               Don't persist session
  --session-dir PATH         Session storage directory
  --tools, -t TEXT           Comma-separated tool allowlist
  --no-tools, -nt            Disable all tools
  --no-builtin-tools, -nbt   Disable built-in tools
  --system-prompt TEXT       Replace system prompt (file or text)
  --append-system-prompt     Append text or file to system prompt (repeatable)
  --extension, -e FILE       Load extension file (repeatable)
  --no-extensions, -ne       Disable extension discovery
  --skill FILE               Load skill file (repeatable)
  --theme FILE               Load theme file (repeatable)
  --list-models [SEARCH]     List available models
  --export FILE              Export session to file
  --verbose, -v              Verbose output
  --offline                  Disable network operations
  --cwd PATH                 Working directory
  --config FILE              Config file path
  --help, -h                 Show help
  --version, -v              Show version

Examples:
  tau                              Interactive mode
  tau "What files are in src/?"    With initial prompt
  tau @README.md "Summarize"       Include file in prompt
  tau -p "List files"              Non-interactive mode
  tau --continue "What did we do?" Continue last session
  tau --tools read,ls              Read-only mode
  tau -m gpt-4o:high "Think deeply" Model with thinking level
  tau -c "What's next?"            Continue previous session
  tau --fork abc123 "Try again"    Fork session and continue
  tau -p --tools read "Review this" Read-only, non-interactive
  tau --resume                     Pick session from list
  tau @prompt.md @image.png "Describe this" Multiple file refs

Environment Variables:
  OPENAI_API_KEY          OpenAI API key
  TAU_DIR                 τ data directory (default: ~/.tau)
  TAU_MODEL               Default model
  TAU_PROVIDER            Default provider
  TAU_THINKING            Default thinking level
  TAU_TOOLS               Default tool allowlist
  TAU_SYSTEM_PROMPT       Default system prompt
  TAU_CWD                 Default working directory
```

## Priority Summary

| Priority | Flags | Effort | Blocker? |
|----------|-------|--------|----------|
| **P0** | `-p`, `--continue`, `--tools`, `--thinking` | ~3 days | ✅ Yes |
| **P1** | `--fork`, `--resume`, `--no-session`, `--session`, `--system-prompt`, `--append-system-prompt`, `--no-tools`, `--no-builtin-tools`, model shorthand | ~4 days | No |
| **P2** | `--models`, `--list-models`, `--session-dir`, `--verbose`, `--offline`, `--export`, `--extension`, `--skill`, `--theme`, `--version` | ~3 days | No |
| **P3** | `--api-key`, `--prompt-template`, `--no-skills`, `--no-extensions`, `--no-prompt-templates`, `--no-themes`, `--no-context-files`, `--mode rpc` | N/A | No |

**Total estimated effort: ~10 days** for full flag parity with pi's core feature set.

## Implementation Order

1. **Print mode (`-p`)** — biggest gap, enables all automation
2. **`--continue`** — essential UX, builds on print mode
3. **`--tools` / `--thinking`** — agent behavior flags, needed for all modes
4. **Session flags** (`--fork`, `--resume`, `--no-session`, `--session`) — builds on print mode
5. **System prompt flags** — small, straightforward
6. **Argparse migration** — refactor manual parser into argparse (can do in parallel)
7. **Positional args** (`@files`, messages, stdin pipe) — polish
8. **Extension/skill/theme flags** — when those subsystems are implemented
9. **Model shorthand** — `provider/id:thinking` parsing
10. **Everything else** — polish
