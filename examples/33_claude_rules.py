"""Example 33: Claude Rules — a rules directory folded into the system prompt (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S60. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/claude-rules.ts``.

## What this shows

``session_start`` (E6 §2 / S41, notify-grade, error-surfaced) doing the setup
side effect pi's example exists to demonstrate: scan the project's
``.claude/rules/`` folder for ``*.md`` files once, at session start, and stash
the list. ``before_agent_start`` (E5 §1 / §3.1) then folds that list into the
running system prompt every turn (chained the same way ``32_pirate.py``'s
addendum is), so the model knows which rule files EXIST and can ``read`` the
relevant one on demand rather than the whole bank being force-fed into every
call.

## Why ``session_start``, not import-time scanning

Scanning at import time would read module-load-order-dependent state and can
never re-run; ``session_start`` fires once per session AFTER extensions load
(``runner.py`` lifecycle ordering, S41), giving a real hook point tied to the
session's own ``cwd`` (``ctx.cwd`` — an ``ExtensionContext`` property every
hook handler reaches) rather than the process's ambient working directory.
This is a faithful, non-fallback port: pi's original does the identical scan
in its own ``session_start`` handler.

## Field contract

The ``session_start`` event dict is ``{"type": "session_start", "reason":
str}`` (``agent_session.py`` ``emit_session_start``); no ``cwd`` field —
handlers read it off ``ctx.cwd`` instead (pi parity: ``ctx.cwd``, not on the
event). ``before_agent_start`` reads/returns ``system_prompt`` exactly as in
``32_pirate.py`` — see that module's docstring for the field contract.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.claude_rules import claude_rules_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read"],
    extensions=[claude_rules_extension],
)
```

Then, with a project containing ``.claude/rules/testing.md``::

    tau -e examples/33_claude_rules.py
"""

from __future__ import annotations

import os
from typing import Any

#: The rules directory, relative to the session's ``cwd`` (pi parity: ``.claude/rules``).
RULES_SUBDIR = os.path.join(".claude", "rules")


def find_markdown_files(root: str) -> list[str]:
    """Recursively find ``*.md`` files under ``root``, as paths relative to it.

    Returns ``[]`` when ``root`` does not exist — an absent rules folder is not
    an error, just "no rules declared" (pi parity: ``fs.existsSync`` guard).
    Results are sorted for a deterministic system-prompt listing.
    """
    if not os.path.isdir(root):
        return []
    results: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.endswith(".md"):
                full = os.path.join(dirpath, filename)
                results.append(os.path.relpath(full, root))
    return sorted(results)


def render_rules_addendum(rule_files: list[str]) -> str:
    """The system-prompt addendum listing available rule files (or ``""`` if none)."""
    if not rule_files:
        return ""
    rules_list = "\n".join(f"- {RULES_SUBDIR}/{f}" for f in rule_files)
    return f"""

## Project Rules

The following project rules are available in {RULES_SUBDIR}/:

{rules_list}

When working on tasks related to these rules, use the read tool to load the relevant rule files for guidance.
"""


class ClaudeRulesState:
    """Per-session scanned-rules state (rescanned each ``session_start``)."""

    def __init__(self) -> None:
        self.rule_files: list[str] = []

    def on_session_start(self, event: dict[str, Any], ctx: Any) -> None:
        """``session_start`` handler: scan ``.claude/rules/`` under ``ctx.cwd``."""
        rules_dir = os.path.join(ctx.cwd, RULES_SUBDIR)
        self.rule_files = find_markdown_files(rules_dir)
        if self.rule_files:
            ctx.ui.notify(f"Found {len(self.rule_files)} rule(s) in {RULES_SUBDIR}/", "info")

    def on_before_agent_start(self, event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        """``before_agent_start`` handler: fold the scanned list into the system prompt."""
        addendum = render_rules_addendum(self.rule_files)
        if not addendum:
            return None
        return {"system_prompt": event["system_prompt"] + addendum}


def claude_rules_extension(api: Any) -> None:
    """Extension entry point: register the rules scan + system-prompt fold-in."""
    state = ClaudeRulesState()
    api.on("session_start", state.on_session_start)
    api.on("before_agent_start", state.on_before_agent_start)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/33_claude_rules.py`` → ``getattr(module, "register")``).
register = claude_rules_extension
