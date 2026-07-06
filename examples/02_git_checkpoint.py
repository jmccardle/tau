"""Example 2: Git Checkpoint Extension

Automatically creates a git commit checkpoint after each agent turn.
This ensures that every meaningful action is backed up to version control.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.git_checkpoint import git_checkpoint_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["bash", "read", "write"],
    extensions=[git_checkpoint_extension],
)
```

## How It Works

1. The extension subscribes to `turn_end` events
2. After each turn, it checks if there are uncommitted changes
3. If there are, it runs: `git add -A && git commit -m "τ checkpoint: <summary>"`
4. If no changes, no commit is created (clean checkout)
"""

from __future__ import annotations

import subprocess
import os


def _has_git_repo(path: str) -> bool:
    """Check if path contains a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _has_uncommitted_changes(path: str) -> bool:
    """Check if there are uncommitted changes in the git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return len(result.stdout.strip()) > 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _git_add_and_commit(path: str, message: str) -> bool:
    """Run git add -A && git commit -m <message>."""
    try:
        subprocess.run(
            ["git", "-C", path, "add", "-A"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        result = subprocess.run(
            ["git", "-C", path, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def git_checkpoint_extension(api):
    """Extension that creates git checkpoints after each turn.

    After every agent turn completes, this extension:
    1. Checks if we're in a git repository
    2. Checks if there are uncommitted changes
    3. If so, commits them with a checkpoint message
    """

    def on_turn_end(event):
        """Create a git checkpoint after each turn."""
        cwd = os.getcwd()

        # Only checkpoint if we're in a git repo
        if not _has_git_repo(cwd):
            return

        # Only checkpoint if there are changes
        if not _has_uncommitted_changes(cwd):
            return

        # Get a summary from the last message
        summary = "τ checkpoint"
        if event.message:
            content = event.message.get("content", [])
            if content:
                # Extract text from last content block
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text and len(text) > 10:
                            summary = f"τ checkpoint: {text[:100]}"
                            break

        # Create the commit
        success = _git_add_and_commit(cwd, summary)
        if success:
            api.ui.notify(f"Checkpoint committed: {summary}")
        else:
            api.ui.notify("Git checkpoint: no commits created (conflict?)")

    api.on("turn_end", on_turn_end)
