"""Example 35: Auto-Commit on Exit — an exit-time side effect (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/auto-commit-on-exit.ts``.

## What this shows

The ``session_shutdown`` lifecycle hook (E6 §2 / S41, notify-grade,
error-surfaced; fires on TUI quit, headless completion, and SIGINT/SIGTERM) doing
real teardown work: if the session's ``cwd`` is a git repo with uncommitted
changes, stage everything and commit with a message built from the last
assistant reply. A session that produced real edits leaves a real commit behind
even if nobody remembered to run ``git commit`` — pi's original motivation,
unchanged.

## Field contract

pi reads the last assistant message via ``ctx.sessionManager.getEntries()`` and
filters ``entry.type === "message" && entry.message.role === "assistant"``. τ's
direct equivalent is ``ctx.entries()`` (``ExtensionContext.entries`` — E3-ctx
§S19, a thin pass-through to ``SessionLog.entries()``): each entry is
``{"type": "message", "message": {...}}`` and an assistant message's
``content`` is a list of blocks; a ``{"type": "text", "text": ...}`` block is
read exactly like pi's ``content.filter(c => c.type === "text")``. ``ctx.cwd``
stands in for pi's ambient process cwd (τ threads the session's own cwd through
every hook context — same reasoning as ``33_claude_rules.py``). Git itself is
invoked via ``subprocess`` (pi: ``pi.exec("git", [...])``) — τ's extension API
has no process-exec primitive of its own (non-goal G-adjacent; not something
this demo needs to add), so it shells out directly, the same way
``02_git_checkpoint.py`` already does in this repo.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.auto_commit_on_exit import auto_commit_on_exit_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[auto_commit_on_exit_extension],
)
```

Or load directly through the public ``-e`` surface::

    tau -e examples/35_auto_commit_on_exit.py
"""

from __future__ import annotations

import subprocess
from typing import Any

#: Commit-message subject cap (pi parity: ``firstLine.slice(0, 50)``).
_SUBJECT_MAX_LEN = 50


def last_assistant_text(entries: list[dict[str, Any]]) -> str:
    """The most recent assistant message's concatenated text blocks, or ``""``.

    Walks ``entries`` from the tail (pi parity: ``for (i = entries.length - 1; ...)``),
    returning the first ``role: "assistant"`` message entry found — joining its
    ``{"type": "text"}`` content blocks with newlines. Returns ``""`` when there
    is no assistant message yet (a session with no completed turn).
    """
    for entry in reversed(entries):
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(texts)
        return ""
    return ""


def build_commit_message(last_text: str) -> str:
    """The ``[tau] <first line, truncated>`` commit subject (pi parity: ``[pi] ...``).

    Falls back to ``"Work in progress"`` when there is no assistant text yet
    (pi parity: ``lastAssistantText.split("\\n")[0] || "Work in progress"``) —
    a session that ends before any reply still gets a legible commit subject
    rather than an empty one.
    """
    first_line = last_text.split("\n")[0] if last_text else ""
    if not first_line:
        first_line = "Work in progress"
    subject = first_line[:_SUBJECT_MAX_LEN]
    if len(first_line) > _SUBJECT_MAX_LEN:
        subject += "..."
    return f"[tau] {subject}"


def _git_status_porcelain(cwd: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, result.stdout


def _git_add_and_commit(cwd: str, message: str) -> int:
    subprocess.run(["git", "-C", cwd, "add", "-A"], capture_output=True, text=True, timeout=10)
    result = subprocess.run(
        ["git", "-C", cwd, "commit", "-m", message],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode


def on_session_shutdown(event: dict[str, Any], ctx: Any) -> None:
    """``session_shutdown`` handler: commit uncommitted work under ``ctx.cwd``."""
    status_code, status = _git_status_porcelain(ctx.cwd)
    if status_code != 0 or status.strip() == "":
        # Not a git repo, or nothing to commit — pi parity: silent no-op.
        return

    message = build_commit_message(last_assistant_text(ctx.entries()))
    commit_code = _git_add_and_commit(ctx.cwd, message)
    if commit_code == 0:
        ctx.ui.notify(f"Auto-committed: {message}", "info")


def auto_commit_on_exit_extension(api: Any) -> None:
    """Extension entry point: commit outstanding work when the session ends."""
    api.on("session_shutdown", on_session_shutdown)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/35_auto_commit_on_exit.py`` → ``getattr(module, "register")``).
register = auto_commit_on_exit_extension
