"""The REPL head's source-level invariants — docs/REPL-HEAD.md §2, §4.

Two claims that decay silently rather than failing: one palette (the next inline
``style="bold red"`` renders perfectly and is found only when someone wants to
change the theme) and no Textual (an import added four modules away costs a
``rich``-only head its whole reason to exist, and nothing at runtime says so).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tau_coding_agent import repl, repl_input, repl_theme

#: Style and colour words ``rich`` understands; a string holding one is a palette decision.
_COLOUR_WORDS = re.compile(
    r"\b(bold|dim|italic|underline|reverse|blink|strike"
    r"|red|green|blue|yellow|cyan|magenta|white|black"
    r"|bright_\w+|grey\d*|gray\d*)\b"
)


def _docstrings(tree: ast.AST) -> set[int]:
    """Line numbers of every docstring constant, which prose may use any word in."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                lines.add(body[0].value.lineno)
    return lines


@pytest.mark.parametrize("module", [repl, repl_input])
def test_no_module_but_the_palette_names_a_colour(module) -> None:
    """Every style string the head prints with comes from ``repl_theme``."""
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    skip = _docstrings(tree)
    offenders = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.lineno not in skip
        and _COLOUR_WORDS.search(node.value)
    ]
    assert offenders == []


def test_importing_the_head_imports_no_textual() -> None:
    """Measured in a fresh interpreter, because this one has a TUI test's imports."""
    probe = (
        "import sys; import tau_coding_agent.repl;"
        " print(any(m == 'textual' or m.startswith('textual.') for m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"


def test_a_role_has_both_a_style_and_a_glyph() -> None:
    """The two tables are keyed the same, so ``line`` cannot half-know a role."""
    assert set(repl_theme.ROLE_STYLE) == set(repl_theme.GLYPH)


def test_a_lane_a_human_typed_at_this_prompt_wears_no_badge() -> None:
    assert repl_theme.lane_label("interactive", "human") is None


def test_a_foreign_lane_is_badged_with_its_source_and_submitter() -> None:
    """One badge, in the reader's vocabulary — a caller never composes a second."""
    assert repl_theme.lane_label("timer", "cron:nightly") == "Timer · cron:nightly"
    assert repl_theme.lane_label("agent", "fork:review") == "Sub-agent · fork:review"
    assert repl_theme.lane_label("odd", "x") == "odd · x"
