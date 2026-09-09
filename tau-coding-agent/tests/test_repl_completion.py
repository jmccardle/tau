"""Tab at the REPL prompt — docs/REPL-HEAD.md §6, docs/SLASH-COMMANDS.md §3.

Three vocabularies in one order: the ``@…`` the cursor is in, else the VALUE of a
command argument, else the command WORD. The candidates come from the same pure
functions the TUI's popup reads, so what the two heads offer cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.commands import complete_command
from tau_coding_agent.backends import make_model_resolver
from tau_coding_agent.repl import REPL_COMMAND_DESCRIPTIONS, ReplCompleter

from repl_fakes import CONFIG, FakeBackend, ReplEnv, env  # noqa: F401


def _completer(*, session: bool = True, runtime: Any = None) -> ReplCompleter:
    """A completer over a fake backend, with the resolver ``run_repl`` binds."""
    backend = FakeBackend(dict(CONFIG))
    if session:
        backend.agent_session.model_resolver = make_model_resolver(CONFIG["models"])
    else:
        backend.agent_session = None  # type: ignore[assignment]
    return ReplCompleter(backend, runtime)


@pytest.mark.parametrize("prefix", ["/", "/c", "/re"])
def test_the_command_word_offers_exactly_what_complete_command_offers(prefix: str) -> None:
    """One matcher: this head adds its own words to the table and reads the same
    function, rather than filtering a list of its own."""
    expected = complete_command(prefix, REPL_COMMAND_DESCRIPTIONS)
    assert expected is not None
    candidates = _completer()(prefix, len(prefix))
    assert [c.text for c in candidates] == [m.name for m in expected.matches]
    assert all(c.start == 1 for c in candidates)


def test_this_heads_own_words_are_offered_beside_the_builtins() -> None:
    """``/reasoning`` resolves to nothing, so without this the completer would
    call a command it does implement unknown."""
    assert "reasoning" in [c.text for c in _completer()("/re", 3)]


def test_a_slash_that_names_nothing_says_so_instead_of_offering_nothing() -> None:
    """The one warning line: an unknown slash is SENT AS TEXT, which is correct
    and, until it is said, invisible."""
    candidates = _completer()("/zzz", 4)
    assert [c.text for c in candidates] == ["zzz"]
    assert "not a command τ knows" in candidates[0].meta


def test_an_argument_value_replaces_the_span_the_core_named() -> None:
    """``complete_command_argument`` says WHICH argument and over what span; the
    head enumerates it and hands back a replacement for exactly that span."""
    candidates = _completer()("/model ", 7)
    assert [c.text for c in candidates] == ["local-llm", "other"]
    assert all(c.start == 7 for c in candidates)


def test_a_domain_that_cannot_be_listed_says_why_in_the_meta_text() -> None:
    """This runs on a keypress, where a traceback and an empty list are both wrong."""
    candidates = _completer(session=False)("/model ", 7)
    assert len(candidates) == 1
    assert "needs a session" in candidates[0].meta


def test_an_attachment_completes_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``@…`` wins over the other two vocabularies, decided by the cursor."""
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "other.md").write_text("hi", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    candidates = _completer()("read @not", len("read @not"))
    assert [c.text for c in candidates] == ["notes.txt"]
    assert candidates[0].start == 6


def test_nothing_is_offered_for_ordinary_prose() -> None:
    """A line that is not a command and not an ``@…`` has nothing to complete."""
    assert _completer()("summarize the file", 18) == []


async def test_the_head_installs_a_completer_and_takes_it_away_again(env: ReplEnv) -> None:
    """A completer left behind would complete against a session that is gone."""
    await env.run(["hello"])
    assert env.reader.completers[0] is not None
    assert env.reader.completers[-1] is None
