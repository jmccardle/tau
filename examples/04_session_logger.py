"""Example 4: Session Logger Extension

Logs all agent events to a file. Useful for debugging, auditing,
and replaying conversations.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.session_logger import session_logger_extension

# Log to a specific file
session = create_agent_session(
    model="gpt-4o",
    tools=["read", "write", "bash"],
    extensions=[
        lambda api: session_logger_extension(api, log_file="/tmp/tau_session.log"),
    ],
)
```

## How It Works

1. The extension subscribes to all agent events via `api.on("all", ...)`
2. Each event is serialized to JSON and written to the log file
3. Events include: agent_start, turn_start, message_start, message_update,
   message_end, tool_execution_start, tool_execution_update, tool_execution_end,
   turn_end, agent_end
4. The log file is append-only — safe to run multiple sessions

## Log Format

Each line is a JSON object:

```json
{
  "type": "message_update",
  "timestamp": 1718668800000,
  "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
  "is_error": false
}
```
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


def _serialize_event(event: Any) -> dict[str, Any]:
    """Serialize an AgentEvent to a dict for JSON serialization."""
    data = {
        "type": event.type,
        "timestamp": event.timestamp,
        "is_error": event.is_error if hasattr(event, "is_error") else False,
    }

    # Add event-specific fields
    if hasattr(event, "message") and event.message is not None:
        data["message"] = event.message
    if hasattr(event, "turn_index") and event.turn_index is not None:
        data["turn_index"] = event.turn_index
    if hasattr(event, "tool_name") and event.tool_name is not None:
        data["tool_name"] = event.tool_name
    if hasattr(event, "tool_call_id") and event.tool_call_id is not None:
        data["tool_call_id"] = event.tool_call_id
    if hasattr(event, "args") and event.args is not None:
        data["args"] = event.args
    if hasattr(event, "result") and event.result is not None:
        data["result"] = event.result
    if hasattr(event, "tool_results") and event.tool_results is not None:
        data["tool_results"] = event.tool_results
    if hasattr(event, "messages") and event.messages is not None:
        data["messages"] = event.messages

    return data


def session_logger_extension(api, log_file: str = "/tmp/tau_session.log"):
    """Extension that logs all agent events to a file.

    This extension:
    1. Subscribes to all agent events
    2. Serializes each event to JSON
    3. Appends to the specified log file

    Args:
        api: ExtensionAPI instance
        log_file: Path to the log file (default: /tmp/tau_session.log)
    """

    def on_all_events(event):
        """Log every agent event to the file."""
        data = _serialize_event(event)
        data["datetime"] = datetime.utcnow().isoformat() + "Z"

        # Ensure the directory exists
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        # Append to log file (thread-safe with os-level locking)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    api.on("all", on_all_events)
