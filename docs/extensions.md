# Extension System Design

## Overview

τ's extension system is its killer feature. Users write Python modules that:
1. Register custom tools callable by the LLM
2. Intercept agent events (tool calls, messages, lifecycle)
3. Add commands and keyboard shortcuts
4. Modify session state
5. Interact with the user via the TUI

Extensions are loaded at runtime from configured directories, just like pi's TypeScript extensions.

## Why Python Extensions Are Easier Than TypeScript

| Concern | pi (TypeScript/jiti) | τ (Python) |
|---------|---------------------|------------|
| Module loading | jiti transpiler | `importlib` — native |
| Hot reload | File system watch + cache bust | `importlib.reload()` — built-in |
| Dependencies | npm packages per extension | `requirements.txt` per extension |
| Type safety | TypeBox schemas | pydantic — native Python |
| Async | Native | `asyncio` — native |

## Extension File Structure

### Single File Extension

```
~/.tau/extensions/
└── my_tool.py
```

```python
# ~/.tau/extensions/my_tool.py
from tau_agent_core import ExtensionAPI, define_tool
from pydantic import BaseModel

class GreetParams(BaseModel):
    name: str = "world"

greet_tool = define_tool({
    "name": "greet",
    "label": "Greet",
    "description": "Greet someone by name",
    "parameters": GreetParams.model_json_schema(),
    "execute": greet_execute,
})

async def greet_execute(tool_call_id, params, signal, on_update, ctx):
    return {
        "content": [{"type": "text", "text": f"Hello, {params.name}!"}],
        "details": {"greeted": params.name},
    }

def register(pi: ExtensionAPI):
    pi.register_tool(greet_tool)

    pi.on("tool_call", async def(event, ctx):
        if event.tool_name == "bash":
            print(f"🔧 Bash tool called: {event.input.command[:80]}")
```

### Directory Extension

```
~/.tau/extensions/my_extension/
├── __init__.py        # Entry point (calls register())
├── tools.py           # Tool definitions
├── handlers.py        # Event handlers
└── utils.py           # Helper functions
```

### Package Extension (with dependencies)

```
~/.tau/extensions/my_package/
├── __init__.py
├── requirements.txt   # pip installable
├── package.json       # Optional: for npm-equivalent metadata
└── my_tool.py
```

## Extension API (`tau_agent_core.extensions.types`)

```python
class ExtensionAPI:
    """Public API exposed to all extension modules."""

    # ── Event Subscription ─────────────────────────────────────

    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to an event.

        Events:
            agent_start / agent_end
            turn_start / turn_end
            message_start / message_update / message_end
            tool_execution_start / tool_execution_update / tool_execution_end
            tool_call (before execution, can block)
            tool_result (after execution, can modify)
            session_start / session_shutdown
            resources_discover
        """
        ...

    # ── Tool Registration ──────────────────────────────────────

    def register_tool(self, definition: ToolDefinition) -> None:
        """Register a tool callable by the LLM.

        Can be called at module load time or from event handlers
        (for dynamic tools registered at runtime).
        """
        ...

    def get_all_tools(self) -> list[ToolInfo]:
        """Get all registered tools (built-in + extension)."""
        ...

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name."""
        ...

    # ── Command Registration ───────────────────────────────────

    def register_command(self, name: str, command: CommandDefinition) -> None:
        """Register a slash command (e.g., /mycommand)."""
        ...

    # ── Session State ──────────────────────────────────────────

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist extension state (does not appear in LLM context)."""
        ...

    def set_session_name(self, name: str) -> None:
        """Set the session display name."""
        ...

    # ── Messaging ──────────────────────────────────────────────

    def send_user_message(self, content: str, deliver_as: str = "steer") -> None:
        """Send a user message to the agent.

        deliver_as: "steer" (while streaming), "followUp" (after finish)
        """
        ...

    def send_message(self, message: dict, options: dict) -> None:
        """Send a custom message into the session."""
        ...

    # ── CLI Flags ──────────────────────────────────────────────

    def register_flag(self, name: str, options: dict) -> None:
        """Register a CLI flag (e.g., --my-flag)."""
        ...

    def get_flag(self, name: str) -> Any:
        """Get the value of a CLI flag."""
        ...

    # ── UI Methods (TUI-only, no-ops in headless mode) ────────

    @property
    def ui(self) -> ExtensionUI:
        ...


class ExtensionContext:
    """Context passed to event handlers and tools."""

    @property
    def cwd(self) -> str:
        """Current working directory."""
        ...

    @property
    def session_manager(self) -> SessionManager:
        """Access to session state."""
        ...

    @property
    def signal(self) -> AbortSignal | None:
        """Abort signal for the current turn (None when idle)."""
        ...

    @property
    def is_idle(self) -> bool:
        """Whether the agent is currently idle."""
        ...

    def abort(self) -> None:
        """Abort the current agent turn."""
        ...

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        ...

    def get_context_usage(self) -> dict:
        """Get current context window usage."""
        ...


class ExtensionUI:
    """User interaction methods (TUI only)."""

    async def confirm(self, title: str, message: str) -> bool:
        """Show confirmation dialog. Returns True if confirmed."""
        ...

    async def select(self, title: str, items: list[str]) -> str | None:
        """Show selection dialog. Returns selected item or None."""
        ...

    async def input(self, title: str, default: str = "") -> str:
        """Show input dialog. Returns user input."""
        ...

    def notify(self, message: str, level: str = "info") -> None:
        """Show a non-modal notification."""
        ...
```

## Tool Definition

```python
def define_tool(definition: dict) -> AgentTool:
    """Define a tool for the LLM to call.

    Returns a tool definition compatible with the agent loop.

    Args:
        definition: Dict with keys:
            name: str - Tool name (called by LLM)
            label: str - Human-readable label for UI
            description: str - Tool description (sent to LLM)
            parameters: dict - JSON Schema (pydantic model_json_schema())
            execute: Callable - Async function to run when tool is called
            prompt_snippet: str | None - One-line summary for system prompt
            prompt_guidelines: list[str] | None - Guidelines for using this tool
            execution_mode: str - "sequential" or "parallel"

    Returns:
        AgentTool instance

    Example:
        tool = define_tool({
            "name": "greet",
            "label": "Greet",
            "description": "Greet someone by name",
            "parameters": GreetParams.model_json_schema(),
            "execute": greet_execute,
        })
    """
    ...


# execute signature
async def execute(
    tool_call_id: str,
    params: object,                    # pydantic model instance or dict
    signal: AbortSignal | None,        # For abort-aware async work
    on_update: Callable | None,        # Call to stream partial results
    ctx: ExtensionContext,             # Session/state context
) -> dict:
    """
    Returns:
        {
            "content": [{"type": "text", "text": "..."}],
            "details": {"key": "value"},   # Arbitrary metadata
            "terminate": False,            # Hint to stop after this batch
        }
    """
```

## Event Handlers

### Tool Call Interception (Blocking)

```python
# Block destructive bash commands
def register(pi: ExtensionAPI):
    pi.on("tool_call", async def(event, ctx):
        if event.tool_name == "bash":
            cmd = event.input.command
            dangerous = ["rm -rf", "sudo", "dd if=", "mkfs"]
            if any(d in cmd for d in dangerous):
                ok = await ctx.ui.confirm(
                    "Destructive Command",
                    f"Allow: {cmd[:100]}...",
                )
                if not ok:
                    return {"block": True, "reason": "User denied"}
```

### Tool Result Modification

```python
# Summarize large bash outputs
def register(pi: ExtensionAPI):
    pi.on("tool_result", async def(event, ctx):
        if event.tool_name == "bash" and len(event.content) > 10000:
            # Summarize with LLM or truncate
            return {
                "content": [{"type": "text", "text": "[Output truncated: ...]"}],
            }
```

### Session State Management

```python
# Persist a counter across reloads
def register(pi: ExtensionAPI):
    state = {"count": 0}

    pi.on("session_start", async def(event, ctx):
        # Restore state from session entries
        for entry in ctx.session_manager.get_entries():
            if entry.custom_type == "my_counter":
                state["count"] = entry.data.get("count", 0)

    pi.on("turn_end", async def(event, ctx):
        state["count"] += 1
        pi.append_entry("my_counter", {"count": state["count"]})
```

## Extension Discovery Order

1. Global extensions (`~/.tau/extensions/`) — loaded first
2. Project extensions (`<cwd>/.tau/extensions/`) — loaded second

This means project extensions can override global extension behavior via event ordering.

## Extension Lifecycle

```
τ starts
  │
  ├─ load global extensions
  ├─ load project extensions
  ├─ register all tools
  │
  ▼
session_start → extensions initialize
  │
  ├─ user sends prompt
  │     │
  │     ├─ tool_call (can block)
  │     ├─ tool execution
  │     ├─ tool_result (can modify)
  │     └─ agent_end
  │
  ├─ /reload → session_shutdown → reload extensions → session_start
  │
  └─ quit → session_shutdown
```

## Example Extensions

### Permission Gate

```python
# ~/.tau/extensions/permission_gate.py
from tau_agent_core import ExtensionAPI

def register(pi: ExtensionAPI):
    # List of patterns that trigger confirmation
    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "sudo",
        "mkfs",
        "dd if=/dev/zero",
        "wget | sh",
        "curl | sh",
    ]

    pi.on("tool_call", async def(event, ctx):
        if event.tool_name == "bash":
            cmd = event.input.get("command", "")
            for pattern in DANGEROUS_PATTERNS:
                if pattern in cmd:
                    ok = await ctx.ui.confirm(
                        "⚠️  Destructive Command",
                        f"Block pattern: {pattern}\nCommand: {cmd[:200]}\n\nAllow?",
                    )
                    if not ok:
                        return {"block": True, "reason": f"Blocked dangerous pattern: {pattern}"}

    # Also block writes to sensitive files
    SENSITIVE_PATHS = [".env", ".git/", "node_modules/", "venv/"]

    pi.on("tool_call", async def(event, ctx):
        if event.tool_name == "write":
            path = event.input.get("path", "")
            for sensitive in SENSITIVE_PATHS:
                if sensitive in path:
                    ok = await ctx.ui.confirm(
                        "⚠️  Sensitive File",
                        f"Writing to: {path}\n\nAllow?",
                    )
                    if not ok:
                        return {"block": True, "reason": f"Blocked write to sensitive path: {path}"}
```

### Git Checkpoint

```python
# ~/.tau/extensions/git_checkpoint.py
from tau_agent_core import ExtensionAPI
import asyncio

def register(pi: ExtensionAPI):
    pi.on("agent_end", async def(event, ctx):
        # Auto-commit after each successful agent turn
        result = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ctx.cwd,
        )
        stdout, _ = await result.communicate()
        if stdout.strip():  # Only commit if there are changes
            await asyncio.create_subprocess_exec(
                "git", "add", ".",
                cwd=ctx.cwd,
            )
            await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"τ checkpoint: {ctx.session_manager.get_session_name()}",
                cwd=ctx.cwd,
            )
```

### Dynamic Tool Registration

```python
# ~/.tau/extensions/dynamic_env_tool.py
from tau_agent_core import ExtensionAPI, define_tool
from pydantic import BaseModel
import os

class EnvParams(BaseModel):
    name: str

env_tool = define_tool({
    "name": "get_env",
    "label": "Get Environment Variable",
    "description": "Read an environment variable from the process environment",
    "parameters": EnvParams.model_json_schema(),
    "execute": async def(tool_call_id, params, signal, on_update, ctx):
        value = os.environ.get(params.name, "<not set>")
        return {
            "content": [{"type": "text", "text": f"{params.name}={value}"}],
            "details": {},
        }
})

def register(pi: ExtensionAPI):
    pi.register_tool(env_tool)
```
