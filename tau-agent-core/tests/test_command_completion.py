"""What ``complete_command`` offers for a half-typed slash command.

Reference: docs/SLASH-COMMANDS.md.

The function is pure and decides nothing — ``resolve_command`` remains the only
thing that says whether a finished line IS a command. What these tests hold is
that the two agree: a token completion offers must be a token resolution accepts,
and a token completion warns about must be one resolution sends to the model.
"""

from __future__ import annotations

from tau_agent_core.commands import FRONTEND_COMMANDS, complete_command, resolve_command

EXTENSIONS = {"todo": "manage the todo list", "bookmark": "save a place in the conversation"}


def names(text: str, extensions: dict[str, str] | None = None) -> list[str]:
    completions = complete_command(text, extensions if extensions is not None else EXTENSIONS)
    assert completions is not None
    return [match.name for match in completions.matches]


class TestWhatItOffers:
    def test_a_bare_slash_offers_the_whole_vocabulary(self):
        """The discovery affordance, and it costs nothing: an empty prefix
        matches every name, so ``/`` alone is the command list."""
        assert names("/") == [*FRONTEND_COMMANDS, "todo", "bookmark"]

    def test_a_prefix_narrows_it(self):
        assert names("/t") == ["tree", "todo"]

    def test_an_exact_name_still_offers_itself(self):
        """So the description stays on screen while the arguments are typed,
        rather than vanishing at the moment the command becomes real."""
        assert names("/extensions") == ["extensions"]

    def test_a_typed_argument_keeps_the_command_on_screen(self):
        assert names("/extensions enable foo") == ["extensions"]

    def test_matching_is_case_sensitive(self):
        """Because ``resolve_command`` is. Offering ``/TREE`` → ``tree`` would
        advertise a completion of a line that resolves to prose."""
        assert names("/TREE") == []
        assert resolve_command("/TREE", EXTENSIONS) is None

    def test_leading_whitespace_is_accepted(self):
        """``parse_command`` strips, so completion must strip too or the two
        would disagree about a line that dispatches perfectly well."""
        assert names("   /tr") == ["tree"]

    def test_built_ins_come_first(self):
        """Resolution order, mirrored. ``resolve_command`` checks the built-ins
        before the extension registry."""
        offered = names("/", {"aaa": "sorts first alphabetically"})
        assert offered[: len(FRONTEND_COMMANDS)] == list(FRONTEND_COMMANDS)

    def test_an_extension_cannot_shadow_a_built_in(self):
        """It is dropped, not listed twice and not listed instead.

        ``resolve_command`` gives the built-in to an extension that registered
        ``tree``, so the extension's command is unreachable by name. Offering it
        would advertise a command the user cannot run.
        """
        offered = names("/tree", {"tree": "an extension that lost"})
        assert offered == ["tree"]
        assert resolve_command("/tree", {"tree": "…"}).performer == "frontend"

    def test_the_description_comes_with_the_name(self):
        completions = complete_command("/todo", EXTENSIONS)
        assert completions is not None
        assert completions.matches[0].description == "manage the todo list"
        assert completions.matches[0].performer == "core"

    def test_no_backend_still_offers_the_built_ins(self):
        """τ's own vocabulary needs no extensions loaded, which is what lets the
        popup work on the first frame."""
        assert names("/comp", {}) == ["compact"]


class TestTheUnknownSlashWarning:
    """The case the whole function exists for: an unknown ``/…`` is sent to the
    model as ordinary text, which is right and is silent."""

    def test_an_unknown_name_reports_itself_with_no_matches(self):
        completions = complete_command("/exntesions", EXTENSIONS)
        assert completions is not None
        assert completions.matches == ()
        assert completions.token == "exntesions"

    def test_and_that_line_really_does_go_to_the_model(self):
        assert resolve_command("/exntesions", EXTENSIONS) is None

    def test_a_space_after_an_unknown_name_silences_it(self):
        """Pasting a path is the reason unrecognised slashes fall through at all.
        Once a space follows a word that names nothing, the line is committed to
        being prose and a warning about it is noise."""
        assert complete_command("/usr/bin/env is on my PATH", EXTENSIONS) is None
        assert complete_command("/xyzzy foo", EXTENSIONS) is None

    def test_a_second_line_makes_the_first_word_unknown(self):
        """Not a quirk of completion — of ``parse_command``, which splits on the
        first SPACE and so reads ``tree\\nmore`` as one word. The warning is the
        only place a user sees that ``/tree`` plus a newline is prose."""
        completions = complete_command("/tree\nmore", EXTENSIONS)
        assert completions is not None
        assert completions.matches == ()
        assert resolve_command("/tree\nmore", EXTENSIONS) is None


class TestWhenItShowsNothingAtAll:
    def test_ordinary_prose(self):
        assert complete_command("summarise the readme", EXTENSIONS) is None

    def test_a_slash_that_is_not_first(self):
        assert complete_command("look in src/ for it", EXTENSIONS) is None

    def test_empty(self):
        assert complete_command("", EXTENSIONS) is None
