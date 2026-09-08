"""`Performed` and `View` — the two arms a dispatched gesture had no record for.

`FlowStep` and `Ready` have described the INPUT side since `abe6a7a`. What running
a gesture produced was reported three ways and only the wire said whether the
conversation had changed.

Reference: docs/REMOTE-CONTROL.md §6, "the result half is generated too".
"""

from __future__ import annotations

import pytest
from tau_agent_core.flows import Performed, View
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.schema import result_schema_for
from tau_agent_core.sdk import AgentSession, InMemorySessionLog
from tau_llm.types import Model


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


def test_two_cursors_that_disagree_are_refused() -> None:
    with pytest.raises(ValueError, match="Two answers to 'where is the tip'"):
        Performed(flow=None, mutation="set_model", data={"cursor": "a"}, cursor="b")


def test_a_capability_that_carries_no_cursor_is_fine() -> None:
    """`data` without a cursor and `cursor=None` agree; nothing is invented."""
    performed = Performed(flow=None, mutation="abort", data={"status": "aborted"})
    assert performed.cursor is None


def test_summary_prefers_the_line_the_capability_already_wrote() -> None:
    performed = Performed(
        flow="disable_extension",
        mutation="disable_extension",
        data={"ok": True, "message": "disabled my_ext.py", "cursor": None},
    )
    assert performed.summary() == "disabled my_ext.py"


def test_summary_names_the_fields_when_there_is_no_message() -> None:
    """What a reader used to get here was `set_auto_compaction: True`."""
    performed = Performed(
        flow="autocompact", mutation="set_auto_compaction", data={"enabled": False, "cursor": None}
    )
    assert performed.summary() == "set_auto_compaction: enabled=False"


def test_summary_of_a_mutation_that_returned_only_a_cursor_still_names_itself() -> None:
    assert Performed(flow=None, mutation="fork", data={"cursor": None}).summary() == "fork"


def test_a_view_must_say_one_of_the_two_things() -> None:
    for state, reason in ((None, None), ({"nodes": []}, "not here")):
        with pytest.raises(ValueError, match="exactly one of"):
            View(name="tree", state=state, unavailable_because=reason)


def test_a_view_carrying_a_reason_is_a_statement_not_a_no_op() -> None:
    view = View(name="tree", unavailable_because="this head has no tree browser")
    assert view.state is None
    assert view.unavailable_because


def test_performed_refuses_a_read(session: AgentSession) -> None:
    """E5 rule 2, in process: a read has no completion to stamp."""
    with pytest.raises(ValueError, match="names a read"):
        session.performed("get_state", {})


def test_performed_refuses_a_cursor_the_caller_supplied(session: AgentSession) -> None:
    with pytest.raises(ValueError, match="two writers of one field"):
        session.performed("set_model", {"model": {}, "cursor": "x"})


def test_performed_adds_the_cursor_exactly_where_returns_declares_one(
    session: AgentSession,
) -> None:
    """Whether a completion carries a cursor is read off the registry, not decided here."""
    carried = session.performed("set_model", {"model": {"id": "m"}})
    assert carried.data["cursor"] == session.session_log.cursor
    assert carried.cursor == carried.data["cursor"]

    uncarried = session.performed("abort", {"status": "aborted", "compaction_id": None})
    assert "cursor" not in uncarried.data
    assert uncarried.cursor is None


def test_performed_records_the_flow_that_named_the_mutation(session: AgentSession) -> None:
    assert session.performed("set_model", {"model": {}}, flow="model").flow == "model"
    assert session.performed("set_model", {"model": {}}).flow is None


@pytest.mark.parametrize(
    "mutation, data",
    [
        ("set_model", {"model": {"id": "m", "provider": "openai", "context_window": 8192}}),
        ("set_session_name", {"name": "the refactor"}),
        ("set_auto_compaction", {"enabled": False}),
        (
            "enable_extension",
            {"action": "enable", "path": "/x/a.py", "ok": True, "message": "enabled /x/a.py"},
        ),
    ],
)
def test_each_generic_mutations_data_matches_its_declared_returns(
    session: AgentSession, mutation: str, data: dict
) -> None:
    """The four that used to report a dict, a str, a bool and an ExtensionActionResult.

    `Performed.data` is not re-validated on every call — nothing re-validates a
    params dict in process either — so this is where the claim is checked.
    """
    performed = session.performed(mutation, data)
    violation = commands.validate_params(result_schema_for(mutation), performed.data)
    assert violation is None, f"{mutation}: {violation}\ndata={performed.data}"
