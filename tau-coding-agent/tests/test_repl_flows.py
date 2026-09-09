"""The flow loop at a prompt line — docs/REPL-HEAD.md §6, TUI-STYLE-GUIDE §2.2.

``next_step`` → render → perform, with ``rich`` printing the choices and the
reader taking the answer. The head reads the STEP and the registry and never the
flow's name, which is what makes a flow an extension declared askable here with no
head code naming it.
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.console import Console

from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.capabilities import BUILTIN, Argument, Domain, Flow
from tau_agent_core.flows import next_step
from tau_coding_agent.repl import ReplRenderer, ask_form
from tau_coding_agent.repl_input import INTERRUPT, MemoryReader

from repl_fakes import FakeBackend, ReplEnv, env, performed_result  # noqa: F401

#: A flow an extension declared: one argument, a domain whose labels are not its values.
PAINT = Flow(
    name="paint",
    description="paint the shed",
    mutation="paint",
    arguments=(Argument("colour", "colour", "Which colour."),),
)

COLOUR = Domain(
    name="colour", description="A colour.", enumerator="list_colours", field_kind="select"
)


def _colours(_query: str, _limit: int) -> list[tuple[str, str]]:
    """Two values whose labels a person reads and whose values a handler takes."""
    return [("#ff0000", "red"), ("#00ff00", "green")]


def _one_label_twice(_query: str, _limit: int) -> list[tuple[str, str]]:
    """Two values a form could not tell apart once they were displayed."""
    return [("#ff0000", "red"), ("#ee0000", "red")]


def _vocabulary(enumerator: Any = _colours) -> Any:
    """``BUILTIN`` plus the ``paint`` flow, the way ``api.register_flow`` extends it."""
    return BUILTIN.extended_with(
        [PAINT], domains={"colour": COLOUR}, enumerators={"colour": enumerator}
    )


def _paints(enumerator: Any = _colours) -> Any:
    """A preparer whose ``/paint`` dispatches to the extension flow's first step."""
    vocabulary = _vocabulary(enumerator)

    def prepare(backend: FakeBackend) -> None:
        backend.agent_session.vocabulary = vocabulary
        backend.extension_commands = [("paint", "paint the shed")]
        backend.command_result = performed_result(
            next_step("paint", {}, vocabulary=vocabulary)  # type: ignore[arg-type]
        )

    return prepare


def _form(reader: MemoryReader) -> ReplRenderer:
    """A renderer over a recording console, for the ``ask_form`` unit tests."""
    return ReplRenderer(
        Console(record=True, width=100, force_terminal=False), reader, model_name="local-llm"
    )


async def test_a_select_hands_back_the_label_and_the_head_maps_it_to_the_value(
    env: ReplEnv,
) -> None:
    """docs/TUI-STYLE-GUIDE.md §3: a form returns the string it DISPLAYED, so the
    head owns the translation — and the handler is called with the value."""
    env.install(_paints())
    await env.run(["/paint"], answers=["2"])
    assert env.backend is not None
    assert env.backend.extension_runs == [("paint", "#00ff00")]
    assert "the handler ran" in env.text
    assert "  1. red" in env.text and "  2. green" in env.text


async def test_a_select_refuses_an_answer_that_is_not_one_of_its_options(env: ReplEnv) -> None:
    """The validator re-asks rather than binding something the domain never offered."""
    env.install(_paints())
    await env.run(["/paint"], answers=["9", "burgundy", "1"])
    assert env.backend is not None
    assert env.backend.extension_runs == [("paint", "#ff0000")]


async def test_two_values_with_one_label_are_refused_before_anything_is_asked(
    env: ReplEnv,
) -> None:
    """Ambiguous is worse than wrong: the answer could not say which was chosen."""
    env.install(_paints(_one_label_twice))
    await env.run(["/paint"])
    assert env.backend is not None
    assert env.backend.extension_runs == []
    assert "two values with the same label" in env.text


async def test_cancelling_a_field_performs_nothing(env: ReplEnv) -> None:
    """Ctrl+C inside an ask cancels the flow — X:590's "when a TUI user cancels"."""
    env.install(_paints())
    await env.run(["/paint"], answers=[INTERRUPT])
    assert env.backend is not None
    assert env.backend.extension_runs == []
    assert "cancelled" in env.text


async def test_session_id_is_asked_as_a_numbered_pick(
    env: ReplEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substitution §2.4 licenses, in CLI form: ``session_id`` renders as
    ``text`` and this head offers the enumerated list instead of a bare box."""
    switched: list[str] = []

    async def fake_switch(self: Any, session_id: str) -> dict[str, Any]:
        switched.append(session_id)
        return {"cancelled": False, "session_id": session_id, "store": "json"}

    monkeypatch.setattr(AgentSessionRuntime, "switch_session", fake_switch)

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(next_step("resume", {}))  # type: ignore[arg-type]

    env.install(prepare)
    await env.run(["/resume"], answers=["1"])
    assert len(switched) == 1
    assert f"now on session {switched[0]}" in env.text


async def test_a_fork_is_performed_by_the_runtime_and_reported(
    env: ReplEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving this head onto another session is the runtime's, not a backend method's."""

    async def fake_fork(self: Any) -> dict[str, Any]:
        return {"cancelled": False, "session_id": "forked-1", "store": "json"}

    monkeypatch.setattr(AgentSessionRuntime, "fork", fake_fork)

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(next_step("fork", {}))  # type: ignore[arg-type]

    env.install(prepare)
    await env.run(["/fork"])
    assert "fork: now on session forked-1" in env.text


async def test_a_vetoed_swap_says_so_rather_than_reporting_a_move(
    env: ReplEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``session_before_switch`` hook said no; nothing was touched."""

    async def fake_fork(self: Any) -> dict[str, Any]:
        return {"cancelled": True}

    monkeypatch.setattr(AgentSessionRuntime, "fork", fake_fork)

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(next_step("fork", {}))  # type: ignore[arg-type]

    env.install(prepare)
    await env.run(["/fork"])
    assert "/fork was vetoed by an extension" in env.text


async def test_every_field_kind_is_asked_with_its_own_control() -> None:
    """The one control table, which the delegate's ``ui.form`` will reuse (§7)."""
    reader = MemoryReader(answers=["a note", "42", "1.5", "y", "2", "1, 2, 1"])
    spec = {
        "title": "Everything",
        "fields": [
            {"name": "note", "kind": "text"},
            {"name": "count", "kind": "number"},
            {"name": "ratio", "kind": "number"},
            {"name": "sure", "kind": "confirm"},
            {"name": "one", "kind": "select", "options": ["a", "b"]},
            {"name": "many", "kind": "multiselect", "options": ["a", "b"]},
        ],
    }
    answers = await ask_form(reader, _form(reader), spec)
    assert answers == {
        "note": "a note",
        "count": 42,
        "ratio": 1.5,
        "sure": True,
        "one": "b",
        "many": ["a", "b"],
    }


async def test_a_number_field_re_asks_until_the_answer_is_one() -> None:
    """``validate_form_values`` checks the type without coercing, so the coercion
    is the head's — and a word that is not a number never becomes one."""
    reader = MemoryReader(answers=["seven", "7"])
    answers = await ask_form(
        reader, _form(reader), {"fields": [{"name": "count", "kind": "number"}]}
    )
    assert answers == {"count": 7}


async def test_an_empty_answer_is_a_value_and_the_form_still_submits() -> None:
    """No field can be declared required (``validate_form_spec`` has no such key),
    so a blank box is an empty answer — not a cancellation that would also throw
    away every field already typed. The TUI submits the same spec the same way."""
    reader = MemoryReader(answers=["", "the path"])
    answers = await ask_form(
        reader,
        _form(reader),
        {"fields": [{"name": "note", "kind": "text"}, {"name": "path", "kind": "text"}]},
    )
    assert answers == {"note": "", "path": "the path"}


async def test_a_cancelled_form_is_the_press_that_cancels_it() -> None:
    """Ctrl+C, and nothing else, is what abandons a form."""
    reader = MemoryReader(answers=[INTERRUPT])
    assert (
        await ask_form(reader, _form(reader), {"fields": [{"name": "note", "kind": "text"}]})
        is None
    )


async def test_a_select_default_pre_fills_the_number_its_own_validator_accepts() -> None:
    """The prompt asks for an index, so the default must be one: the label is text
    the validator rejects, and ``ask`` re-fills the same default after every
    complaint — pressing Enter on it would loop until the line was cleared."""
    reader = MemoryReader(answers=["2"])
    spec = {
        "fields": [
            {"name": "env", "kind": "select", "options": ["dev", "prod"], "default": "prod"},
        ]
    }
    assert await ask_form(reader, _form(reader), spec) == {"env": "prod"}
    assert reader.defaults == ["2"]


async def test_an_emptied_choice_falls_back_the_way_the_tuis_widget_does() -> None:
    """A radio cannot be cleared, so a select answers its default; a selection list
    can be, so a multiselect answers ``[]`` (``_FieldForm._collect``)."""
    reader = MemoryReader(answers=["", ""])
    spec = {
        "fields": [
            {"name": "env", "kind": "select", "options": ["dev", "prod"], "default": "prod"},
            {"name": "tags", "kind": "multiselect", "options": ["a", "b"], "default": ["b"]},
        ]
    }
    assert await ask_form(reader, _form(reader), spec) == {"env": "prod", "tags": []}
    assert reader.defaults == ["2", "2"]


async def test_a_swap_draws_the_transcript_it_swapped_onto(
    env: ReplEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3 step 7's third duty: a ``/resume`` that printed one id and no content
    left the reader unable to say which conversation they were now in."""

    class _Swapped:
        """What the runtime hands the rebind callback: the session, re-logged."""

        messages = [
            {"role": "user", "content": "port the parser", "timestamp": 1},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Ported it."}],
                "timestamp": 2,
            },
        ]

    async def fake_switch(self: Any, session_id: str) -> dict[str, Any]:
        # RT:468 — the runtime calls the installed callback after every real swap.
        self._rebind(_Swapped())
        return {"cancelled": False, "session_id": session_id, "store": "json"}

    monkeypatch.setattr(AgentSessionRuntime, "switch_session", fake_switch)

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(next_step("resume", {}))  # type: ignore[arg-type]

    env.install(prepare)
    await env.run(["/resume"], answers=["1"])
    assert "› port the parser" in env.text
    assert "Ported it." in env.text
