"""Example 5: Custom Tool Extension

Demonstrates how to register a simple custom tool with the agent.
This "greet" tool shows the basic pattern for building any custom tool.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.custom_tool import greet_tool_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[greet_tool_extension],
)
```

## How It Works

1. The extension defines a tool specification (name, description, parameters)
2. It registers the tool with the agent via `api.register_tool()`
3. The tool's `execute` function is called when the LLM decides to use it
4. The result is returned to the LLM as a tool result message

## Example Agent Interaction

```
User: Say hi to Alice
Agent: [decides to use greet tool]
Tool call: greet(name="Alice")
Tool result: Hello, Alice! How can I help you today?

User: Greet everyone in the team: Alice, Bob, and Charlie
Agent: [decides to use greet tool multiple times]
Tool call: greet(name="Alice")
Tool result: Hello, Alice!
Tool call: greet(name="Bob")
Tool result: Hello, Bob!
Tool call: greet(name="Charlie")
Tool result: Hello, Charlie!
"""

from __future__ import annotations

from typing import Any


# Tool definition for the greet tool
GREET_TOOL = {
    "name": "greet",
    "label": "Greet",
    "description": (
        "Greet someone by name. This is a simple tool that generates "
        "a personalized greeting message for the specified person."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the person to greet.",
            },
            "tone": {
                "type": "string",
                "description": (
                    "The tone of the greeting. Options: "
                    "formal, casual, friendly, humorous. Default: friendly."
                ),
                "enum": ["formal", "casual", "friendly", "humorous"],
            },
        },
        "required": ["name"],
    },
    "prompt_snippet": "greet: Greet someone by name",
    "prompt_guidelines": [
        "Use this tool to greet a specific person.",
        "The tool returns a personalized greeting message.",
        "You can specify the tone of the greeting (formal, casual, friendly, humorous).",
    ],
    "execution_mode": "sequential",
}


def _greet_execute(args: dict[str, Any]) -> str:
    """Execute the greet tool logic."""
    name = args.get("name", "there")
    tone = args.get("tone", "friendly")

    greetings = {
        "formal": f"Good day, {name}. It is a pleasure to make your acquaintance.",
        "casual": f"What's up, {name}! How's it going?",
        "friendly": f"Hello, {name}! How can I help you today?",
        "humorous": f"Well hello there, {name}! Ready to have a fantastic conversation?",
    }

    return greetings.get(tone, greetings["friendly"])


# Store the tool definition for later use
GREET_TOOL_DEFINITION = GREET_TOOL
GREET_TOOL_EXECUTE = _greet_execute


def greet_tool_extension(api):
    """Extension that registers a simple greet tool.

    This extension:
    1. Registers the 'greet' tool with the agent
    2. The tool accepts a name and optional tone parameter
    3. Returns a personalized greeting message

    Args:
        api: ExtensionAPI instance to register the tool with
    """

    api.register_tool(GREET_TOOL)
