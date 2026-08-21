"""τ's startup tagline — and the only place randomness enters the TUI.

The tagline sits under the τ on the empty chat pane (:class:`~tau_coding_agent.app.ChatPlaceholder`).
It has exactly two behaviours:

- ``fun=False`` → ``TAGLINES[0]``, always. Every deterministic surface depends on
  this: ``tests/test_tui_snapshots.py`` compares rendered SVGs byte for byte, and
  ``python -m tau_coding_agent.devshot`` has to produce the same PNG twice.
- ``fun=True`` → a uniform random pick from the whole list.

**This module is the entire blast radius of ``--fun``.** The flag is parsed in
``cli.py``, handed to ``Parley.__init__``, and consumed here on the way to one
string. Nothing downstream — no backend, session, tool, or agent-loop code —
ever sees it. That containment is the point: a joke that can break a turn is not
worth telling.

Reference: docs/CLI-PLAN.md (Secondary flags).
"""

from __future__ import annotations

import random

__all__ = ["FUN_DEFAULT", "TAGLINES", "pick_tagline"]

#: The taglines, in author's order. **Index 0 is load-bearing** — it is the
#: default tagline, the one every test and screenshot sees, and the one a
#: packaged user gets back with ``--no-fun``. Reorder the rest freely; do not
#: displace the first without meaning to change τ's default self-description.
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

# --- fun-default marker: package.sh rewrites the line below. Do not reformat. ---
#: Whether ``--fun`` is on when the user does not say. ``False`` in a source
#: checkout, so a developer's suite and screenshots are deterministic without
#: passing a flag; ``package.sh`` rewrites this literal to ``True`` in the staged
#: tarball, so a released τ is playful out of the box.
#:
#: The rewrite is a one-line ``sed`` against the exact text below, and package.sh
#: verifies the substitution landed rather than shipping a silently unpatched
#: build. Keep the assignment on one line, spelled exactly ``FUN_DEFAULT = False``.
FUN_DEFAULT = False
# --- end fun-default marker ---


def pick_tagline(fun: bool) -> str:
    """The tagline to print under the τ.

    Args:
        fun: ``True`` picks at random; ``False`` returns ``TAGLINES[0]``.

    Callers pick this ONCE per process (``Parley.__init__``) rather than per
    render. A tagline that rerolled every time the pane reappeared would change
    under the user on every ``/clear``, which reads as a glitch rather than a
    joke.
    """
    return random.choice(TAGLINES) if fun else TAGLINES[0]
