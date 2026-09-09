"""``--mode repl`` at the argv boundary — docs/REPL-HEAD.md §2.

Two things are asserted here and nowhere else: the four flags this mode REFUSES,
each with the sentence that says what to do instead, and the fact that the repl
branch sits BEFORE the ``--continue/--session/--fork require --print`` gate — a
head with a prompt line resumes a session the way the TUI does, and the gate
would otherwise refuse the flags this mode accepts.
"""

from __future__ import annotations

import pytest

from tau_coding_agent import cli
from tau_coding_agent.cli import parse_cli_args


def test_repl_is_a_valid_mode() -> None:
    assert parse_cli_args(["--mode", "repl"]).mode == "repl"


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """Dispatch ``argv`` with ``run_repl`` replaced by a recorder."""
    calls: list[tuple] = []

    async def fake_run_repl(args, config, **kwargs):
        calls.append((args, config))
        return 0

    monkeypatch.setattr("tau_coding_agent.repl.run_repl", fake_run_repl)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    assert cli.main(argv) == 0
    assert len(calls) == 1
    return calls[0][0]


def test_mode_repl_dispatches_to_run_repl(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _run(monkeypatch, ["--mode", "repl"])
    assert args.mode == "repl"


def test_continuation_flags_no_longer_need_print(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repl branch precedes the ``require --print`` gate, so ``--continue``
    reaches ``run_repl`` instead of the TUI-only refusal."""
    args = _run(monkeypatch, ["--mode", "repl", "--continue"])
    assert args.continue_session is True


def test_resume_reaches_the_repl(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _run(monkeypatch, ["--mode", "repl", "--resume"])
    assert args.resume is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--mode", "repl", "-p"], "--mode repl is an interactive prompt loop"),
        (["--mode", "repl", "hello"], "reads prompts from its own prompt line"),
        (["--mode", "repl", "--ui-defaults", "confirm=yes"], "has one at the prompt"),
        (["--mode", "repl", "--theme", "latte"], "renders with rich in the terminal's own"),
    ],
)
def test_refusals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, argv: list[str], expected: str
) -> None:
    """Each refusal names the flag's real purpose and what to do instead."""
    monkeypatch.setattr(cli, "load_config", lambda: {})
    assert cli.main(argv) == 2
    assert expected in capsys.readouterr().err


def test_ui_defaults_help_no_longer_says_print_only() -> None:
    """The string said "Headless (--print) only" while ``--mode rpc`` consumed it
    too; the repl refusal is what made the inaccuracy load-bearing."""
    actions = {action.dest: action for action in cli.build_parser()._actions}
    help_text = actions["ui_defaults"].help or ""
    assert "Headless (--print) only" not in help_text
    assert "--print and --mode rpc only" in help_text
