"""The REPL head's one palette: every colour and glyph it prints, named once.

Reference: docs/REPL-HEAD.md §4. A ``rich`` style string per role and a glyph per
role, keyed the same way, so a renderer never spells a colour inline. There is no
head-agnostic palette to mirror — ``tau.tcss`` declares no role variables and the
TUI's colours stay in its stylesheet (docs/TUI-STYLE-GUIDE.md §5) — so unifying
the two is the deferred theme work, not this module's.

The role names are the REPL's own vocabulary, not the wire's: ``foreign`` is any
lane a human did not type at this prompt, and ``blocked`` is a tool an extension
vetoed. Three of them are also an extension's ``notify`` levels — ``extension``
is ``info``, and ``warning``/``error`` are themselves — so the level a delegate
is handed picks a style rather than a second colour table (§7).
"""

from __future__ import annotations

#: ``rich`` style per role. One place; nothing else names a colour.
ROLE_STYLE: dict[str, str] = {
    "user": "bold cyan",
    "assistant": "default",
    "reasoning": "dim italic magenta",
    "tool": "yellow",
    "tool_error": "bold red",
    "blocked": "bold magenta",
    "system": "dim",
    "extension": "green",
    "warning": "bold yellow",
    "foreign": "bold blue",
    "error": "bold red",
}

#: Leading glyph per role, keyed as :data:`ROLE_STYLE`.
GLYPH: dict[str, str] = {
    "user": "›",
    "assistant": "",
    "reasoning": "▸",
    "tool": "⚙",
    "tool_error": "✗",
    "blocked": "⛔",
    "system": "·",
    "extension": "◆",
    "warning": "!",
    "foreign": "──",
    "error": "[τ]",
}

#: ``chat_widgets.ROLE_LABELS["custom"]``, duplicated because that module imports Textual.
EXTENSION_LABEL = "Extension"

#: ``lane_end``'s tool glyph for a result that succeeded.
TOOL_OK = "✓"

#: The marker the head prints for a turn it aborted (nothing on the render stream says it).
ABORTED = "⏹"

#: The marker for a turn boundary inside one lane.
TURN = "↻"

#: The marker for a steering message woven into a running turn.
STEER = "↳"

#: The marker for a line the head is holding until its delivery point.
PENDING = "⏳"

#: The marker for the lines a fold left out — the transcript window's own (docs/TRANSCRIPT-WINDOW.md §4).
MORE = "⋯"

#: ``ROLE_LABELS``' foreign-source names, duplicated for the reason ``EXTENSION_LABEL`` is.
SOURCE_LABELS: dict[str, str] = {
    "interactive": "User",
    "rpc": "RPC",
    "extension": "Extension",
    "bus": "Bus",
    "timer": "Timer",
    "webhook": "Webhook",
    "voice": "Voice",
    "agent": "Sub-agent",
}


def lane_label(source: object, submitter: object) -> str | None:
    """The badge a foreign lane wears, or ``None`` for "a human typed it here".

    Jupyter's rule (``RenderRouter``'s docstring): a head filters on "is this
    mine?" to decide HOW to render and still renders the rest, so this returns a
    label and never a "drop it". The badge names the source in the reader's
    vocabulary rather than the wire's — a sub-agent lane reads ``Sub-agent ·
    fork:review`` — and it is the whole badge, so a caller never composes a
    second one around it.

    Args:
        source: The submission's ``source`` as ``lane_start`` carried it.
        submitter: The submission's ``submitter``.

    Returns:
        ``"{display source} · {submitter}"`` for a foreign lane; ``None`` for this head's own.
    """
    if source == "interactive" and submitter == "human":
        return None
    return f"{lane_role(source)} · {submitter}"


def lane_role(source: object) -> str:
    """The display name for a foreign lane's source.

    Args:
        source: The submission's ``source`` as ``lane_start`` carried it.

    Returns:
        The mapped label, or the source stringified when it is not one τ names.
    """
    return SOURCE_LABELS.get(str(source), str(source))
