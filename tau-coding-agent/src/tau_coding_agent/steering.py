"""Where a line typed while a turn is running goes, and when it is delivered.

Reference: docs/TUI-STEERING.md §2, docs/REPL-HEAD.md §5. The config key, the two
strategies and the startup check are the TUI's (``app.py`` 108–112, 318–343); this
module is where a head that must not import Textual reads them, and
``test_repl_steering.py`` asserts the two copies have not drifted.

The buffer is the HEAD's and not the core's, for the reason docs/TUI-STEERING.md
§2 gives: once ``AgentSession.submit`` has taken content, no frontend can get it
back, and an abort has to be able to hand a line the user typed back to them.
"""

from __future__ import annotations

from typing import Any

from tau_coding_agent.config import ConfigError

#: The ``config.json`` key that picks a delivery point.
STEERING_CONFIG_KEY = "steering_strategy"

#: The two values, spelled as ``MultitaskStrategy`` spells them because they ARE that field.
STEERING_STRATEGIES = ("steer", "enqueue")

#: What an unset key means: the running turn takes the message at its next tool call.
DEFAULT_STEERING_STRATEGY = "steer"

#: "All at once, and only all at once" (docs/TUI-STEERING.md §2): the buffer is ONE message.
STEER_JOIN = "\n\n"


def configured_steering_strategy(config: dict[str, Any]) -> str:
    """The delivery point for text typed during a turn.

    Fail-Early: an unrecognised value RAISES rather than falling back to the
    default. The two strategies put the message in a different place at a
    different time, so a typo that silently selected the other one would be
    invisible until a steering message did not land where it was aimed.

    Args:
        config: The loaded ``config.json``.

    Returns:
        ``"steer"`` or ``"enqueue"``.

    Raises:
        ConfigError: When the key holds anything else.
    """
    configured = config.get(STEERING_CONFIG_KEY, DEFAULT_STEERING_STRATEGY)
    if configured not in STEERING_STRATEGIES:
        raise ConfigError(
            f"config key {STEERING_CONFIG_KEY!r} = {configured!r} is not a "
            f"steering strategy. Use one of {', '.join(sorted(STEERING_STRATEGIES))}."
        )
    return str(configured)


def steering_note(strategy: str) -> str:
    """What the head promises about a held line, in words.

    Both strategies can end up delivering at the turn edge — ``"steer"`` does when
    the running turn makes no further tool call — so this names the strategy's own
    delivery point and lets the delivery report what actually happened.

    Args:
        strategy: A member of :data:`STEERING_STRATEGIES`.

    Returns:
        The one-line promise printed under a held line.
    """
    if strategy == "steer":
        return "steering, at this turn's next tool call"
    return "waiting for this turn to end"


class SteeringBuffer:
    """Everything typed mid-turn that the model has not been shown yet.

    Two lists because an undelivered line has two states, and only the first is
    the head's to give back on its own: ``pending`` is text no door has taken, and
    ``delivered`` is the raw text of a ``"steer"`` submission the core accepted
    with ``messages=[]`` — parked in ``_pending_steer_messages`` until the running
    loop weaves it in, and cleared by ``AgentSession.abort`` without telling
    anyone (agent_session.py:3353). Keeping it here until the ``steer_message``
    render event confirms the weave is what lets an abort reclaim it.

    Attributes:
        pending: Lines held for the next delivery point, in the order typed.
        delivered: Raw texts handed to the core and not yet woven in, oldest first.
    """

    def __init__(self) -> None:
        self.pending: list[str] = []
        self.delivered: list[str] = []

    def hold(self, text: str) -> None:
        """Add one typed line to the buffer."""
        self.pending.append(text)

    def take(self) -> str | None:
        """Empty the buffer into ONE message, or ``None`` when it is empty."""
        if not self.pending:
            return None
        text = STEER_JOIN.join(self.pending)
        self.pending.clear()
        return text

    def await_weave(self, text: str) -> None:
        """Record that the core accepted *text* and has not woven it in yet."""
        self.delivered.append(text)

    def confirm(self) -> None:
        """A ``steer_message`` render event proved the oldest delivery landed."""
        if self.delivered:
            self.delivered.pop(0)

    def discard(self, text: str) -> None:
        """Forget a delivery that will never produce a ``steer_message``.

        Two cases reach here: the submission was refused, and the turn had already
        ended so the core ran it as an ordinary turn of its own.
        """
        if text in self.delivered:
            self.delivered.remove(text)

    def reclaim(self) -> str | None:
        """Take back everything the model has not been shown, oldest text first.

        ``delivered`` before ``pending`` because that is the order the user typed
        them in, which is the ordering docs/TUI-STEERING.md §4 gives for putting
        reclaimed text in front of a draft.

        Returns:
            The whole buffer as one message, or ``None`` when there is nothing.
        """
        held = self.delivered + self.pending
        self.delivered.clear()
        self.pending.clear()
        return STEER_JOIN.join(held) if held else None
