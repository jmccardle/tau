"""Example 32: Pirate — a ``before_agent_start`` system-prompt chain (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/pirate.ts``.

## What this shows

The smallest possible demo of ``before_agent_start``'s ``system_prompt`` chain
(E5 §1 / §3.1): a ``/pirate`` slash command toggles in-memory state, and a
``before_agent_start`` handler appends pirate-speak instructions to the running
system prompt when that state is on. Per-turn framing (E5 §1): the appended
text applies to every subsequent turn while pirate mode is on, and is rebuilt
from the base prompt each ``prompt()`` call — never persisted as its own tree
node (unlike an injected *message*, a ``system_prompt`` return is a per-call
frame, not a durable node; see ``agent_session.py`` around the
``before_agent_start`` call-site).

## Field contract

The ``before_agent_start`` event dict carries ``{"type", "prompt", "images",
"system_prompt"}`` (``runner.py`` ``emit_before_agent_start``); the handler
reads the RUNNING value off ``event["system_prompt"]`` (already chained through
any earlier handler) and returns ``{"system_prompt": ...}`` to replace it for
this turn, or ``None``/no return to leave it untouched.

``register_command`` handlers run as ``handler(args, ctx)`` where ``ctx`` is the
session's live ``ExtensionContext`` (``agent_session.py``
``run_extension_command``) — the same ``ctx.ui`` every hook reaches, so
``/pirate``'s toggle confirmation paints in the same TUI (or stderr headless).

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.pirate import pirate_extension

session = create_agent_session(
    model="gpt-4o",
    extensions=[pirate_extension],
)
```

Then, interactively::

    tau -e examples/32_pirate.py
    > /pirate
    Arrr! Pirate mode enabled!
"""

from __future__ import annotations

from typing import Any

#: Appended to the running system prompt for every turn while pirate mode is on.
PIRATE_SYSTEM_PROMPT_ADDENDUM = """

IMPORTANT: You are now in PIRATE MODE. You must:
- Speak like a stereotypical pirate in all responses
- Use phrases like "Arrr!", "Ahoy!", "Shiver me timbers!", "Avast!", "Ye scurvy dog!"
- Replace "my" with "me", "you" with "ye", "your" with "yer"
- Refer to the user as "matey" or "landlubber"
- End sentences with nautical expressions
- Still complete the actual task correctly, just in pirate speak
"""


class PirateState:
    """Per-session toggle state (in-memory; resets on reload, like pi's closure)."""

    def __init__(self) -> None:
        self.enabled = False

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled

    def on_before_agent_start(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``before_agent_start`` handler: chain the pirate addendum onto the prompt."""
        if not self.enabled:
            return None
        return {"system_prompt": event["system_prompt"] + PIRATE_SYSTEM_PROMPT_ADDENDUM}

    def on_pirate_command(self, args: str, ctx: Any) -> str:
        """``/pirate`` command handler: toggle the mode and notify + return a message."""
        enabled = self.toggle()
        message = "Arrr! Pirate mode enabled!" if enabled else "Pirate mode disabled"
        ctx.ui.notify(message, "info")
        return message


def pirate_extension(api: Any) -> None:
    """Extension entry point: register ``/pirate`` and the system-prompt chain."""
    state = PirateState()
    api.register_command(
        "pirate",
        {
            "description": "Toggle pirate mode (agent speaks like a pirate)",
            "handler": state.on_pirate_command,
        },
    )
    api.on("before_agent_start", state.on_before_agent_start)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/32_pirate.py`` → ``getattr(module, "register")``).
register = pirate_extension
