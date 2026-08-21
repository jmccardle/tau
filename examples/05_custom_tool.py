"""Example 5: Custom Tool Extension

The smallest complete ``api.register_tool`` demo: one tool, ``greet``, that the
model can call. Read this before the tool-registering demos that do real work
(``20_delegate.py``, ``23_context_surgeon.py``, ``38_todo.py``) — they all use
the same three pieces, just with more of them.

## The three pieces

1. A **JSON Schema** in ``parameters`` describing the arguments. The model reads
   this to decide how to call the tool, and τ validates every call against it.
2. An **execute function** with the five-argument extension signature
   ``execute(tool_call_id, params, signal, on_update, ctx)``. It may be sync or
   async; τ awaits it either way.
3. A **result dict** ``{"content": [{"type": "text", "text": ...}]}``. τ turns it
   into the ``toolResult`` message the model reads on its next turn. Set
   ``"is_error": True`` to tell the model the call failed.

``execute`` is a required key. Defining the function and forgetting to put it in
the dict raises ``ValueError: register_tool: missing required key 'execute'`` at
registration — before the model can ever call a tool that does nothing.

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/05_custom_tool.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[ext.register],
)
```

Or load the file directly through the public ``-e`` surface::

    tau -e examples/05_custom_tool.py

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
```
"""

from __future__ import annotations

from typing import Any

#: The greeting text for each supported tone. ``friendly`` is the tone used when
#: the model omits the optional ``tone`` argument.
GREETINGS: dict[str, str] = {
    "formal": "Good day, {name}. It is a pleasure to make your acquaintance.",
    "casual": "What's up, {name}! How's it going?",
    "friendly": "Hello, {name}! How can I help you today?",
    "humorous": "Well hello there, {name}! Ready to have a fantastic conversation?",
}

DEFAULT_TONE = "friendly"


def greet(name: str, tone: str = DEFAULT_TONE) -> str:
    """Build the greeting. Pure — no τ types, so it is directly testable."""
    template = GREETINGS.get(tone, GREETINGS[DEFAULT_TONE])
    return template.format(name=name)


def greet_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Any,
    ctx: Any,
) -> dict[str, Any]:
    """The registered ``execute``: τ's five-argument extension tool signature.

    Only ``params`` is used here. The other four are the rest of the contract and
    are named so the shape is visible: ``tool_call_id`` identifies this call,
    ``signal`` is the :class:`~tau_llm.abort.AbortSignal` a long-running tool
    should poll, ``on_update`` streams partial output to the UI, and ``ctx`` is
    the live ``ExtensionContext``.

    ``name`` is read without a default because the schema marks it required, so
    τ has already rejected any call that omits it — see ``validate_tool_arguments``.
    """
    return {
        "content": [
            {"type": "text", "text": greet(params["name"], params.get("tone", DEFAULT_TONE))}
        ]
    }


#: The definition handed to ``api.register_tool``. Every key the model sees comes
#: from here; ``execute`` is the one key that never reaches the model.
GREET_TOOL: dict[str, Any] = {
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
                "enum": sorted(GREETINGS),
            },
        },
        "required": ["name"],
    },
    "execute": greet_execute,
    "prompt_snippet": "greet: Greet someone by name",
    "prompt_guidelines": [
        "Use this tool to greet a specific person.",
        "The tool returns a personalized greeting message.",
        "You can specify the tone of the greeting (formal, casual, friendly, humorous).",
    ],
    "execution_mode": "sequential",
}


def greet_tool_extension(api: Any) -> None:
    """Extension entry point: register the ``greet`` tool."""
    api.register_tool(GREET_TOOL)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/05_custom_tool.py`` → ``getattr(module, "register")``), so the demo is
#: loadable through the public ``-e`` surface, not only by importing
#: ``greet_tool_extension`` directly.
register = greet_tool_extension
