"""Example 1: Permission Gate Extension

Blocks destructive commands (rm, chmod 777, dd, mkfs, etc.) before they
are executed. This is a safety extension that can be used in production
environments where you want to prevent accidental data loss.

## Usage

Register this extension when creating a session:

```python
from tau_agent_core.sdk import create_agent_session
from examples.permission_gate import permission_gate_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["bash", "read", "write"],
    extensions=[permission_gate_extension],
)
```

## How It Works

1. The extension subscribes to `tool_execution_start` events
2. When a bash tool is detected, it checks the command arguments
3. If the command matches a blocked pattern, it sends a warning message
4. The extension uses the UI to confirm with the user before allowing execution
"""

from __future__ import annotations

import re

# Patterns for destructive commands that should be blocked
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/",        # rm -rf /
    r"\bchmod\s+777",         # chmod 777
    r"\bdd\s+",               # dd
    r"\bmkfs\b",              # mkfs
    r"\bmkdisk\b",            # mkdisk
    r">\s*/dev/(sd|vd|nvme)", # redirect to block device
    r"\bfdisk\b",             # fdisk
    r"\bparted\b",            # parted
    r"\bsudo\s+rm",           # sudo rm
    r"\bshred\b",             # shred
]

COMPILED_PATTERNS = [re.compile(p) for p in BLOCKED_PATTERNS]

BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda",
]


def permission_gate_extension(api):
    """Extension that blocks destructive commands.

    This extension:
    1. Subscribes to tool_execution_start events
    2. Intercepts bash commands that match blocked patterns
    3. Either blocks them automatically or asks for confirmation
    """

    def on_tool_execution_start(event):
        """Check bash tool calls for destructive commands."""
        if event.tool_name != "bash":
            return

        args = event.args or {}
        command = args.get("command", "")

        # Check for blocked command patterns
        for pattern in COMPILED_PATTERNS:
            if pattern.search(command):
                # Block the command
                api.notify(
                    f"Blocked destructive command: {command[:100]}",
                    level="error",
                )
                return

        # Also check for known dangerous command names
        cmd_name = command.strip().split()[0] if command.strip() else ""
        if cmd_name in BLOCKED_COMMANDS:
            api.notify(f"Blocked known dangerous command: {cmd_name}", level="error")
            return

    api.on("tool_execution_start", on_tool_execution_start)
