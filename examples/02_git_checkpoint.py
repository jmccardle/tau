"""Example 2: Git Checkpoint Extension

Commits the working tree once per user turn, so a session that edits files leaves
a reviewable history behind instead of one large diff at the end.

## Which hook, and why not ``turn_end``

This registers ``user_turn_end``, which fires **once per**
``AgentSession.prompt()`` — once per thing the user actually asked for.

``turn_end`` is the tempting choice and it is the wrong one here: it fires once
per *agent-loop* turn, so one request resolved in six tool round-trips fires it
six times and produces six commits. The runner documents that distinction
directly (``extensions/runner.py``, ``emit_user_turn_end``: "choose by cadence —
per completion, or per utterance"). A checkpoint is per utterance.

``35_auto_commit_on_exit.py`` is the third cadence: one commit for the whole
session, at shutdown. Pick the one that matches how you want to read the history
back; loading both this and 35 gives you per-turn commits plus a final one.

## Hook signature

Every hook handler is called ``handler(event, ctx)`` and ``event`` is a **dict**,
not an object. A ``user_turn_end`` event carries ``{"type", "firing_unit",
"loop_turns", "messages"}``. Returning ``None`` makes the handler a pure observer,
which is what a checkpoint is — it writes to git, never to the conversation.

``ctx.cwd`` is the session's own working directory. Use it rather than
``os.getcwd()``: τ threads the session cwd through every hook context, and the
process cwd may be somewhere else entirely.

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/02_git_checkpoint.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    tools=["bash", "read", "write"],
    extensions=[ext.register],
)
```

Or load the file directly through the public ``-e`` surface::

    tau -e examples/02_git_checkpoint.py

## What it does each turn

1. Checks that ``ctx.cwd`` is inside a git work tree. If not, does nothing.
2. Checks for uncommitted changes. If the tree is clean, does nothing.
3. Runs ``git add -A`` and commits with a message built from the turn's last
   assistant text.
"""

from __future__ import annotations

import subprocess
from typing import Any

#: Commit-message subject cap, matching ``35_auto_commit_on_exit.py``.
_SUBJECT_MAX_LEN = 50

#: The message used when the turn produced no assistant text to name it by.
_FALLBACK_SUBJECT = "τ checkpoint"


def _git(cwd: str, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``cwd`` and return the completed process."""
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_git_repo(cwd: str) -> bool:
    """Whether ``cwd`` is inside a git work tree."""
    try:
        result = _git(cwd, "rev-parse", "--is-inside-work-tree", timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_uncommitted_changes(cwd: str) -> bool:
    """Whether the work tree at ``cwd`` has staged or unstaged changes."""
    try:
        result = _git(cwd, "status", "--porcelain", timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return bool(result.stdout.strip())


def checkpoint_subject(messages: list[Any]) -> str:
    """Build the commit subject from the last assistant text in ``messages``.

    ``messages`` is the ``user_turn_end`` event's message list in persisted order.
    Each entry is a dict whose ``content`` is a list of blocks; a
    ``{"type": "text", "text": ...}`` block is the assistant's prose. Falls back to
    a bare checkpoint label when the turn produced no assistant text — a tool-only
    turn does happen, and it is still worth committing.
    """
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for block in reversed(message.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                first_line = (block.get("text") or "").strip().splitlines()
                if first_line and first_line[0]:
                    return f"τ checkpoint: {first_line[0][:_SUBJECT_MAX_LEN]}"
    return _FALLBACK_SUBJECT


def commit_checkpoint(cwd: str, subject: str) -> bool:
    """Stage everything in ``cwd`` and commit it. Returns whether a commit landed."""
    try:
        _git(cwd, "add", "-A")
        result = _git(cwd, "commit", "-m", subject, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def git_checkpoint_extension(api: Any) -> None:
    """Extension entry point: register the per-turn git checkpoint."""

    def on_user_turn_end(event: dict[str, Any], ctx: Any) -> None:
        """``user_turn_end`` (pure observer — returns nothing)."""
        cwd = ctx.cwd
        if not is_git_repo(cwd) or not has_uncommitted_changes(cwd):
            return

        subject = checkpoint_subject(event.get("messages") or [])
        if commit_checkpoint(cwd, subject):
            api.ui.notify(f"Checkpoint committed: {subject}")
        else:
            api.ui.notify("Git checkpoint: nothing committed (see git output)")

    api.on("user_turn_end", on_user_turn_end)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/02_git_checkpoint.py`` → ``getattr(module, "register")``), so the demo
#: is loadable through the public ``-e`` surface, not only by importing
#: ``git_checkpoint_extension`` directly.
register = git_checkpoint_extension
