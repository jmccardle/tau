"""Visual-inspection helpers for the τ TUI.

The counterpart of :mod:`tau_agent_core.testing` (contract suites for stores):
this package holds what a developer — or a coding agent — needs to *look at*
the TUI without ever attaching it to a real terminal.

Three pieces, each usable on its own:

* :mod:`~tau_coding_agent.testing.render` — turn a running app into a plain-text
  screen grid, an SVG, a PNG, or a measured layout dump.
* :mod:`~tau_coding_agent.testing.sandbox` — build a ``Parley`` whose every
  ``~/.tau`` read and write lands in a temporary directory. ``tests/conftest.py``
  and the ``devshot`` CLI both go through this, so they cannot drift apart.
* :mod:`~tau_coding_agent.testing.scenes` — named app states worth looking at
  (empty screen, an exchange with tool calls, each modal), driven by
  :mod:`tau_coding_agent.devshot`.

Reference: docs/textual-headless-testing.md
"""

from tau_coding_agent.testing.render import (
    dump_layout,
    render_png,
    render_svg,
    render_text,
    save_render,
)
from tau_coding_agent.testing.sandbox import build_parley, sandbox_tau_home

__all__ = [
    "build_parley",
    "dump_layout",
    "render_png",
    "render_svg",
    "render_text",
    "sandbox_tau_home",
    "save_render",
]
