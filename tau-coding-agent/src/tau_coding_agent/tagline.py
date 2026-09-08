"""τ's startup tagline — and the only place randomness enters the TUI.

The tagline sits under the τ on the empty chat pane (:class:`~tau_coding_agent.app.ChatPlaceholder`).
It has exactly two behaviours:

- ``fun=False`` → ``TAGLINES[0]``, always. Every deterministic surface depends on
  this: ``tests/test_tui_snapshots.py`` compares rendered SVGs byte for byte, and
  ``python -m tau_coding_agent.devshot`` has to produce the same PNG twice. Those
  surfaces ASK for ``fun=False`` — ``TauApp.__init__`` takes the literal, not
  :data:`FUN_DEFAULT` — so their determinism does not depend on how this module's
  default happens to be set, in a checkout or in a wheel.
- ``fun=True`` → a uniform random pick from the whole list.

**This module is the entire blast radius of ``--fun``.** The flag is parsed in
``cli.py``, handed to ``TauApp.__init__``, and consumed here on the way to one
string. Nothing downstream — no backend, session, tool, or agent-loop code —
ever sees it. That containment is the point: a joke that can break a turn is not
worth telling.

Reference: docs/CLI-PLAN.md (Secondary flags).
"""

from __future__ import annotations

import random

__all__ = ["FUN_DEFAULT", "TAGLINES", "pick_tagline"]

TAGLINES: tuple[str, ...] = (
    "a coding agent you can take apart",
    "your loyal automaton",
    "hackity hack hack",
    "back in my day, we'd code by hand",
    "the superior circle constant",
    "Ask me about making an extension!",
    "git commit now or cuss later",
    "does NOT run on electron",
    "yes, TUIs are cool in 2026",
)

FUN_DEFAULT = True


def pick_tagline(fun: bool) -> str:
    """The tagline to print under the τ.

    Args:
        fun: ``True`` picks at random; ``False`` returns ``TAGLINES[0]``.

    Callers pick this ONCE per process (``TauApp.__init__``) rather than per
    render. A tagline that rerolled every time the pane reappeared would change
    under the user on every ``/clear``, which reads as a glitch rather than a
    joke.
    """
    return random.choice(TAGLINES) if fun else TAGLINES[0]
