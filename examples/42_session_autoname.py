"""Example 42: Session Autoname — ambient metadata via ``message_end`` (E9, S64).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S64 row 2. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/session-name.ts``.

## What pi's original does

A single ``/session-name [new name]`` command: with an argument it calls
``pi.setSessionName(name)``; with none it reads back ``pi.getSessionName()`` and
toasts the current value (or "No session name set"). Purely manual — pi never
names a session on its own.

## What this port adds (the roadmap's "ambient metadata")

The manual command is ported faithfully (same usage, same fallback message),
PLUS a passive ``message_end`` observer: the first time an assistant turn
finishes and no name has been set yet (manually or otherwise), the extension
derives a short title from the conversation's first user message and calls
``api.set_session_name(...)`` itself — the auto-titling behavior most chat UIs
give you for free. This is "ambient" in the precise sense the roadmap names it
(contrast ``41_bookmarks``, which is durable TREE state read through
``ctx.entries()``/``TreeStore``): a session name is NOT a tree waypoint or a
``customEntry`` — it is a single ``session_info`` metadata entry the file-backed
``tau_coding_agent.session_store.Session`` already renders as its
``.name``/``display_title()`` (the session selector's title, the TUI's window
title), sourced passively off an event stream rather than pulled from the tree
on demand. Once a name exists (manual or auto), the observer never overwrites
it — a later ``/session-name`` call is still the one way to rename.

## Field contract

``message_end`` is a plain notify event on the ``EventBus`` (NOT one of
``ExtensionRunner.HOOK_EVENTS``/``LIFECYCLE_EVENTS``), so the handler receives a
bare ``AgentEvent`` — no ``ctx`` — exactly like ``34_desktop_notify``'s
``agent_end``. ``ExtensionAPI.set_session_name``/``get_session_name`` (S64) are
top-level ``api`` methods, not ``ExtensionContext`` ones (pi's own
``session-name.ts`` calls ``pi.setSessionName``/``pi.getSessionName`` off the
extension object for the same reason), so both the command handler and the
notify observer close over the registration-time ``api`` reference rather than
reaching through the per-call ``ctx`` (the same closure pattern
``38_todo``'s ``TreeStore`` uses).

Naming is durable via ``append_session_info`` — a ``session_info`` entry
``ConversationTree`` never folds into context, so it is persisted and
reload-invariant but never model input, exactly like a
``model_change``/``thinking_change`` entry. It needs a file-backed session log
(``tau_coding_agent.session_store.Session``); the SDK's RAM-only
``InMemorySessionLog`` has no durable name slot and ``set_session_name`` raises
there (Fail-Early) rather than silently doing nothing.

## Usage

    tau -e examples/42_session_autoname.py
    > let's refactor the auth module
    ... (assistant replies; the session is now auto-named) ...
    > /session-name
    Session: let's refactor the auth module
    > /session-name Auth refactor
    Session named: Auth refactor
"""

from __future__ import annotations

from typing import Any

#: Auto-derived names are truncated to this many characters (pi parity:
#: ``Session.display_title()``'s own first-user-message fallback truncates at 50).
_AUTO_NAME_MAX_LEN = 50


def _extract_text(message: dict[str, Any]) -> str:
    """Flatten a τ message's content blocks to plain text (session_store parity)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        ]
        return " ".join(parts)
    return ""


def _first_user_text(entries: list[dict[str, Any]]) -> str | None:
    """The first user turn's flattened text, or ``None`` if there isn't one yet."""
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _extract_text(message).strip().replace("\n", " ")
        if text:
            return text
    return None


def _auto_name(text: str) -> str:
    """Truncate a candidate name to :data:`_AUTO_NAME_MAX_LEN`, ``...``-suffixed."""
    if len(text) <= _AUTO_NAME_MAX_LEN:
        return text
    return text[:_AUTO_NAME_MAX_LEN] + "..."


def _make_message_end_handler(api: Any) -> Any:
    """The ``message_end`` observer: auto-name once, off the first user turn."""

    def on_message_end(event: Any) -> None:
        message = getattr(event, "message", None)
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        if api.get_session_name():
            return  # already named (manually or by a prior auto-name) — don't overwrite
        text = _first_user_text(api.context.entries())
        if not text:
            return
        api.set_session_name(_auto_name(text))

    return on_message_end


def _make_session_name_command(api: Any) -> Any:
    """The ``/session-name [name]`` handler (pi port): set with an argument,
    show the current value with none."""

    def session_name_command(args: str, ctx: Any) -> str:
        name = args.strip()
        if name:
            api.set_session_name(name)
            return f"Session named: {name}"
        current = api.get_session_name()
        return f"Session: {current}" if current else "No session name set"

    return session_name_command


def session_autoname_extension(api: Any) -> None:
    """Extension entry point: the ``/session-name`` command + the ``message_end``
    auto-naming observer."""
    api.on("message_end", _make_message_end_handler(api))
    api.register_command(
        "session-name",
        {
            "description": "Set or show session name (usage: /session-name [new name])",
            "handler": _make_session_name_command(api),
        },
    )


#: Module-level ``register`` the file-path loader looks up (``tau -e
#: examples/42_session_autoname.py`` → ``getattr(module, "register")``).
register = session_autoname_extension
