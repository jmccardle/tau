"""Example 34: Desktop Notify — an OSC 777 terminal ping on completion (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/notify.ts``.

## What this shows

The notify-grade ``agent_end`` event (works on today's API — no E6 hook needed)
driving a raw terminal side effect: an OSC 777 desktop-notification escape
sequence written straight to ``stdout`` when the agent finishes and is waiting
for input again. This is the "isolated agent / event stream" atom (§1.1) at its
simplest — a pure observer, no veto, no durable node, nothing tree-shaped.

## Scope note (faithful, not lazy)

pi's original branches on three terminal protocols (OSC 777, Kitty's OSC 99, and
a Windows Terminal PowerShell toast) picked by environment variable. The
roadmap entry for this step names exactly one: "terminal OSC 777 ping on
completion" (§5 S61). OSC 777 is also the widest-supported of the three
(Ghostty, iTerm2, WezTerm, rxvt-unicode, and it round-trips harmlessly as inert
bytes on a terminal that doesn't understand it). Porting only the terminal this
step calls out is a deliberate scope match to the plan, not a shortcut around a
subproblem — there is no Windows-toast/Kitty subprocess plumbing being routed
around; the plan simply doesn't ask for it here.

## Field contract

``agent_end`` is a plain notify event dispatched through the ``EventBus``
(``events.py`` — NOT one of ``ExtensionRunner.HOOK_EVENTS``/``LIFECYCLE_EVENTS``),
so the handler receives an ``AgentEvent`` object (not a dict): ``event.type ==
"agent_end"``. No field of the event is read — the notification fires
unconditionally whenever a ``prompt()`` call finishes producing its response,
mirroring pi's ``pi.on("agent_end", async () => notify(...))``.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.desktop_notify import desktop_notify_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read"],
    extensions=[desktop_notify_extension],
)
```

Or load directly through the public ``-e`` surface::

    tau -e examples/34_desktop_notify.py
"""

from __future__ import annotations

import sys
from typing import Any

#: OSC 777 desktop-notification escape sequence: ``ESC ] 777 ; notify ; <title> ; <body> BEL``
#: (pi parity: ``notifyOSC777`` in ``notify.ts``). Supported by Ghostty, iTerm2,
#: WezTerm, and rxvt-unicode; a terminal that doesn't understand OSC 777 simply
#: ignores the bytes.
_OSC777_TEMPLATE = "\x1b]777;notify;{title};{body}\x07"


def notify_osc777(title: str, body: str, *, stream: Any = None) -> None:
    """Write an OSC 777 desktop-notification escape sequence to ``stream``.

    Defaults to ``sys.stdout`` (pi parity: ``process.stdout.write``) — the
    escape sequence goes straight to the terminal regardless of frontend
    (TUI or headless), exactly like pi's direct write bypasses its own UI
    layer for this one demo.
    """
    out = stream if stream is not None else sys.stdout
    out.write(_OSC777_TEMPLATE.format(title=title, body=body))
    out.flush()


def on_agent_end(event: Any, *, stream: Any = None) -> None:
    """``agent_end`` handler: ping the terminal that τ is ready for input."""
    notify_osc777("tau", "Ready for input", stream=stream)


def desktop_notify_extension(api: Any) -> None:
    """Extension entry point: notify the terminal whenever an agent turn ends."""
    api.on("agent_end", on_agent_end)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/34_desktop_notify.py`` → ``getattr(module, "register")``).
register = desktop_notify_extension
