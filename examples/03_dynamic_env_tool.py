"""Example 3: Dynamic Environment Tool Extension

Registers a custom tool that reads environment variables.
This demonstrates how to register a custom tool that provides
real-time system information to the agent.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.dynamic_env_tool import dynamic_env_tool_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[dynamic_env_tool_extension],
)
```

## How It Works

1. The extension registers a custom tool called `env_vars`
2. The tool reads all environment variables from os.environ
3. It returns them as a formatted string, optionally filtered by prefix

## Example Agent Interaction

```
User: What's the PYTHONPATH?
Agent: [calls env_vars tool with prefix=PYTHON]
Tool result: PYTHONPATH=/usr/lib/python3.11:...

User: Show all env vars
Agent: [calls env_vars tool with no prefix]
Tool result: ALL_ENV_VARS...
```
"""

from __future__ import annotations

import os
from typing import Any


# Tool definition for the dynamic environment tool
ENV_VAR_TOOL = {
    "name": "env_vars",
    "label": "Environment Variables",
    "description": (
        "Read environment variables. Optionally filter by a prefix "
        "to show only variables starting with that prefix."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "description": (
                    "Optional prefix to filter environment variables. "
                    "If omitted, all variables are returned."
                ),
            },
        },
        "required": [],
    },
    "prompt_snippet": "env_vars: Read environment variables",
    "prompt_guidelines": [
        "Use this tool to check the current environment configuration.",
        "Pass a prefix (e.g., 'PYTHON') to see only matching variables.",
        "Without a prefix, all environment variables are shown.",
    ],
}


def _execute_env_vars(args: dict[str, Any]) -> str:
    """Execute the env_vars tool logic."""
    prefix = args.get("prefix", "")

    if prefix:
        # Filter by prefix
        filtered = {
            k: v for k, v in os.environ.items()
            if k.startswith(prefix)
        }
        if not filtered:
            return f"No environment variables found with prefix '{prefix}'."

        lines = [f"Environment variables matching '{prefix}':"]
        for k, v in sorted(filtered.items()):
            # Mask sensitive values
            if any(sensitive in k.upper() for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD"]):
                masked = "*" * 8
                lines.append(f"  {k}={masked}")
            else:
                lines.append(f"  {k}={v}")
        return "\n".join(lines)
    else:
        # Return all variables
        lines = ["All environment variables:"]
        count = 0
        for k, v in sorted(os.environ.items()):
            # Mask sensitive values
            if any(sensitive in k.upper() for sensitive in ["KEY", "SECRET", "TOKEN", "PASSWORD"]):
                masked = "*" * 8
                lines.append(f"  {k}={masked}")
            else:
                lines.append(f"  {k}={v}")
            count += 1
            if count >= 20:  # Limit output for safety
                lines.append(f"  ... ({len(os.environ) - 20} more variables)")
                break
        return "\n".join(lines)


# Store the tool definition for later use
ENV_TOOL_DEFINITION = ENV_VAR_TOOL
ENV_TOOL_EXECUTE = _execute_env_vars


def dynamic_env_tool_extension(api):
    """Extension that registers a dynamic environment variables tool.

    This extension:
    1. Registers the 'env_vars' tool with the agent
    2. The tool reads environment variables from os.environ
    3. Supports optional prefix filtering
    4. Masks sensitive values (KEY, SECRET, TOKEN, PASSWORD)
    """

    api.register_tool(ENV_VAR_TOOL)
