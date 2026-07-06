"""Example 36: File Trigger — external world -> conversation (E9, pi port).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S61. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/file-trigger.ts``.

## What this shows

``session_start`` (E6 §2 / S41) standing up a background watcher on a plain
file, and ``api.send_user_message`` (the existing durable message-queue seam)
carrying whatever an external process writes there into the live conversation.
``session_shutdown`` tears the watcher down. This is the canonical "backplane
crossing" demo: something outside the agent process (a script, a webhook
receiver, a human at a shell) drops text into a file, and it shows up as if the
user had typed it — pi's own usage note, unchanged::

    echo "Run the tests" > /tmp/agent-trigger.txt

## Field contract / scope adaptation (faithful, not lazy)

pi's ``fs.watch`` fires an OS-level filesystem event; τ has no such primitive
on its extension API (Node ships ``fs.watch``, Python's stdlib does not, and
this repo carries no ``watchdog``-style dependency — see the survey in
``docs/EXTENSIONS-DEMO-ROADMAP.md`` §0, which does not list a watch facility as
an existing atom). Rather than add a new dependency for one demo, this port
uses a small polling thread (mtime-based, default 0.5s) started in
``session_start`` and stopped in ``session_shutdown`` — the same "watch a file"
*behavior* pi demos, adapted to what τ's stdlib-only extension surface
actually offers.

pi's ``pi.sendMessage(..., {triggerTurn: true})`` forces a turn to start with no
new user prompt — τ's extension API has no such out-of-band turn-starter (a
mutating hook may only append a durable node or transform the CURRENT prompt
pre-node — §1.3 of the roadmap; there is no hook site to fire between prompts
with no caller-initiated turn at all). The roadmap entry for this step names
the mechanism explicitly: "S41 watcher + ``send_user_message``" (§5 S61) — so
this port uses ``api.send_user_message(content, deliver_as="nextTurn")``, which
queues the trigger content to ride along with the NEXT ``prompt()`` call
(``AgentSession._queue_message`` / ``agent_session.py:1074``), exactly the
"queued for the next prompt()" semantics the roadmap names, rather than
inventing a new immediate-turn primitive this step does not ask for.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.file_trigger import file_trigger_extension

session = create_agent_session(
    model="gpt-4o",
    tools=["read"],
    extensions=[file_trigger_extension],
)
```

Or load directly through the public ``-e`` surface::

    tau -e examples/36_file_trigger.py

Then, from another shell, while a session is running::

    echo "Run the tests" > /tmp/tau-agent-trigger.txt
"""

from __future__ import annotations

import threading
from typing import Any

#: Default trigger-file path (pi parity: ``/tmp/agent-trigger.txt``, renamed to
#: avoid colliding with a real pi install watching the same path on one box).
DEFAULT_TRIGGER_FILE = "/tmp/tau-agent-trigger.txt"

#: Polling interval, in seconds, for the mtime-based watcher (see the module
#: docstring's scope-adaptation note — τ has no ``fs.watch`` equivalent).
DEFAULT_POLL_INTERVAL = 0.5


def check_trigger_once(trigger_file: str, api: Any) -> bool:
    """Read+consume ``trigger_file`` once; queue its content if non-empty.

    Returns ``True`` when a message was queued. Mirrors pi's ``fs.watch``
    callback body: read the file, and if it holds non-whitespace content,
    queue ``"External trigger: <content>"`` (pi parity: identical prefix) via
    ``api.send_user_message`` with ``deliver_as="nextTurn"`` (see the module
    docstring for why ``nextTurn`` stands in for pi's ``triggerTurn``), then
    truncate the file so the same content is not re-consumed on the next poll.
    A missing file is not an error — pi's original catches exactly this case
    (``catch { /* file might not exist yet */ }``).
    """
    try:
        with open(trigger_file, encoding="utf-8") as f:
            content = f.read().strip()
    except FileNotFoundError:
        return False
    if not content:
        return False
    api.send_user_message(f"External trigger: {content}", deliver_as="nextTurn")
    with open(trigger_file, "w", encoding="utf-8") as f:
        f.write("")
    return True


class FileTriggerWatcher:
    """The background polling loop's start/stop lifecycle (session-scoped)."""

    def __init__(
        self,
        trigger_file: str = DEFAULT_TRIGGER_FILE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.trigger_file = trigger_file
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self, api: Any) -> None:
        while not self._stop.wait(self.poll_interval):
            check_trigger_once(self.trigger_file, api)

    def start(self, api: Any) -> None:
        """Start the daemon polling thread (idempotent — a second call is a no-op)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(api,), daemon=True, name="file-trigger-watcher"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the polling thread to stop and join it."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None


def file_trigger_extension(
    api: Any,
    trigger_file: str = DEFAULT_TRIGGER_FILE,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Extension entry point: watch ``trigger_file`` and feed it into the session."""
    watcher = FileTriggerWatcher(trigger_file, poll_interval)

    def on_session_start(event: dict[str, Any], ctx: Any) -> None:
        watcher.start(api)
        ctx.ui.notify(f"Watching {trigger_file}", "info")

    def on_session_shutdown(event: dict[str, Any], ctx: Any) -> None:
        watcher.stop()

    api.on("session_start", on_session_start)
    api.on("session_shutdown", on_session_shutdown)


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/36_file_trigger.py`` → ``getattr(module, "register")``).
register = file_trigger_extension
