"""`dispatch_builtin` — which arm a built-in slash command is, decided once.

The head's whole rule is which arm came back. It replaced a two-valued ``performer``
flag that answered a different question on each record it sat on, and could say
nothing at all about the two arms that are neither "the core ran it" nor "a head must".

Reference: docs/REMOTE-CONTROL.md §6, "the result half is generated too".
"""

from __future__ import annotations

import pytest
from tau_agent_core.commands import (
    EXTENSION_VIEW_VERBS,
    UnsupportedCommandError,
    dispatch_builtin,
)
from tau_agent_core.flows import FlowStep, Ready, View


def test_a_view_command_is_a_view() -> None:
    view = dispatch_builtin("tree", "")
    assert isinstance(view, View)
    assert view.name == "tree"
    assert view.state is None
    assert view.unavailable_because and "projects no state" in view.unavailable_because


def test_a_flow_with_nothing_typed_is_its_first_step() -> None:
    step = dispatch_builtin("resume", "")
    assert isinstance(step, FlowStep)
    assert step.argument.name == "session_id"
    assert step.domain.name == "session_id"


def test_a_flow_whose_only_argument_is_optional_is_ready_at_once() -> None:
    ready = dispatch_builtin("compact", "")
    assert isinstance(ready, Ready)
    assert (ready.flow, ready.mutation, ready.arguments) == ("compact", "compact", {})


def test_the_typed_line_is_the_argument_verbatim() -> None:
    ready = dispatch_builtin("name", "the refactor")
    assert isinstance(ready, Ready)
    assert ready.arguments == {"name": "the refactor"}


def test_a_fixed_value_domain_binds_a_real_python_value() -> None:
    ready = dispatch_builtin("autocompact", "false")
    assert isinstance(ready, Ready)
    assert ready.arguments["enabled"] is False


def test_a_word_the_domain_does_not_declare_raises_rather_than_coercing() -> None:
    """Fail-Early: `/autocompact yes` must not quietly mean off."""
    with pytest.raises(ValueError, match="takes one of true, false"):
        dispatch_builtin("autocompact", "yes")


def test_the_extensions_verb_sugar_becomes_the_flow_it_names() -> None:
    """A `View` carries no argument string, so the sugar has to live here.

    A head left holding it would have had the verb resolved out from under it and
    the target silently dropped.
    """
    ready = dispatch_builtin("extensions", "disable my_ext.py")
    assert isinstance(ready, Ready)
    assert (ready.flow, ready.arguments) == ("disable_extension", {"path": "my_ext.py"})


def test_the_verb_table_is_derived_from_the_flows() -> None:
    assert EXTENSION_VIEW_VERBS == {
        "enable": "enable_extension",
        "disable": "disable_extension",
        "reload": "reload_extension",
    }


def test_an_extensions_verb_that_names_no_flow_is_refused_not_discarded() -> None:
    with pytest.raises(UnsupportedCommandError, match="names no action"):
        dispatch_builtin("extensions", "frobnicate my_ext.py")


def test_bare_extensions_is_still_the_view() -> None:
    assert isinstance(dispatch_builtin("extensions", ""), View)


def test_a_view_given_an_argument_is_refused_not_discarded() -> None:
    """``/tree extra words`` raises rather than opening the browser (§4).

    A view carries no argument string, so anything typed after it has nowhere to
    go. It ran and discarded them silently until docs/SLASH-COMMANDS.md §4's
    "still unfixed" case was fixed — the same refusal ``/extensions frobnicate``
    already made, on the other half of the view table.
    """
    with pytest.raises(UnsupportedCommandError, match="takes no arguments"):
        dispatch_builtin("tree", "extra words")


def test_whitespace_after_a_view_is_not_an_argument() -> None:
    """``parse_command`` strips, so trailing spaces must not trip the refusal."""
    assert isinstance(dispatch_builtin("tree", "   "), View)


def test_a_scoped_cursor_rides_the_step_to_the_head() -> None:
    step = dispatch_builtin("resume", "", cursor="entry-7")
    assert isinstance(step, FlowStep)
    assert step.cursor == "entry-7"
