"""Example 4: Session Logger Extension

Writes every agent event to a JSONL file — one JSON object per line. Useful for
debugging a run after it finished, auditing what a session did, and replaying a
transcript offline.

## Notify event, not a hook

This uses ``api.on("all", ...)``, which is a **notify** subscription, not one of
the mutating hooks. The two have different handler contracts and mixing them up
is the most common extension bug:

* notify (``all``, ``agent_start``, ``message_update``, …) → ``handler(event)``,
  one argument, and ``event`` is an :class:`~tau_agent_core.events.AgentEvent`
  **object** with attributes.
* hook (``tool_call``, ``turn_end``, ``user_turn_end``, ``session_start``, …) →
  ``handler(event, ctx)``, two arguments, and ``event`` is a plain **dict**.

A pure observer like this one wants notify: it sees everything, including the
streaming ``message_update`` deltas that no hook fires for, and it cannot alter
the run. See ``02_git_checkpoint.py`` for the hook side of the same distinction.

## Where the log goes

The path is resolved in this order, first hit wins:

1. the ``log_file`` argument, when the extension is wired as a factory;
2. ``api.config["log_file"]`` — ``~/.tau/config.json`` under
   ``"extensions": {"04_session_logger": {"log_file": "..."}}``, or a per-run
   ``--ext-config 04_session_logger.log_file=...``;
3. ``<session cwd>/.tau/session.log``.

The default is derived from the session's own cwd rather than a hardcoded
system path, so the demo behaves the same on any machine and never writes
outside the tree the session is working in.

## Usage

```python
import importlib.util
import sys

from tau_agent_core.sdk import create_agent_session

# examples/ is not an importable package: there is no __init__.py and every
# filename starts with a digit. Load the file by path, as `tau -e` does.
_spec = importlib.util.spec_from_file_location("ext", "examples/04_session_logger.py")
ext = importlib.util.module_from_spec(_spec)
# Register BEFORE exec_module: a module using `from __future__ import
# annotations` resolves its own dataclass annotations through sys.modules.
sys.modules[_spec.name] = ext
_spec.loader.exec_module(ext)

session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[
        lambda api: ext.session_logger_extension(api, log_file="runs/today.jsonl"),
    ],
)
```

Or load the file directly through the public ``-e`` surface, which uses the
resolved default::

    tau -e examples/04_session_logger.py

## Log Format

Each line is one event, serialized from the ``AgentEvent`` model with its unset
fields omitted, plus the wall-clock time the line was written:

```json
{
  "type": "message_update",
  "timestamp": 1718668800000,
  "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
  "is_error": false,
  "logged_at": "2026-08-20T12:00:00+00:00"
}
```
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Path used when neither the ``log_file`` argument nor ``api.config`` supplies
#: one, resolved relative to the session's cwd.
DEFAULT_LOG_RELPATH = Path(".tau") / "session.log"


def resolve_log_path(api: Any, log_file: str | None = None) -> Path:
    """The file this extension will append to. See the module docstring's order."""
    configured = log_file or (api.config or {}).get("log_file")
    if configured:
        return Path(configured).expanduser()
    return Path(api.context.cwd) / DEFAULT_LOG_RELPATH


def serialize_event(event: Any) -> dict[str, Any]:
    """One event as a JSON-safe dict.

    Serialized off the model itself (``mode="json"`` converts nested types the
    way ``json`` needs) rather than by copying a hand-written list of attributes.
    A hand-written list silently omits every field added to ``AgentEvent`` after
    it was written, which is how a log quietly stops carrying the thing you
    later need. ``exclude_none=True`` keeps the lines readable — most events
    populate a small subset of the fields.
    """
    return dict(event.model_dump(mode="json", exclude_none=True))


def session_logger_extension(api: Any, log_file: str | None = None) -> None:
    """Extension entry point: append every agent event to a JSONL file.

    Args:
        api: The ``ExtensionAPI``.
        log_file: Explicit output path. When omitted the path is resolved from
            ``api.config`` and then from the session cwd — see
            :func:`resolve_log_path`.
    """
    path = resolve_log_path(api, log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    def on_all_events(event: Any) -> None:
        """Append one line per event.

        Opened per event rather than held open for the session: an append-mode
        write of a single short line is atomic enough for this, and the log stays
        complete and readable if the process is killed mid-session — which is
        exactly when a debug log earns its keep.

        No ``default=`` coercion on ``json.dumps``: an event this cannot serialize
        is a real defect, and the notify bus surfaces a raising handler (S44)
        instead of killing the session. Coercing it to ``str`` would write a line
        that looks like data and is not.
        """
        record = serialize_event(event)
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    api.on("all", on_all_events)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/04_session_logger.py`` → ``getattr(module, "register")``). ``log_file``
#: is optional, so the loader's one-argument ``register(api)`` call resolves the
#: path from ``api.config`` / the session cwd.
register = session_logger_extension
