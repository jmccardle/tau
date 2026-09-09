"""Commands at the REPL prompt — docs/REPL-HEAD.md §6.

The four arms of a dispatched command, this head's own four words, and the two
outcomes that are not "nothing happened": a refusal, and a command the core ran a
TURN for. Nothing here is ever sent to the model as prose.
"""

from __future__ import annotations

from typing import Any

import pytest

from tau_agent_core.commands import FRONTEND_COMMANDS
from tau_agent_core.flows import Performed, Ready, View
from tau_agent_core.sdk import ExtensionLoadError, LoadExtensionsResult
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.repl import REPL_COMMANDS

from repl_fakes import FakeBackend, ReplEnv, env, performed_result  # noqa: F401


def _answers(result: SubmissionResult, *registered: str) -> Any:
    """A preparer whose ``submit_command`` returns *result*.

    ``registered`` names extension commands, so a line naming one RESOLVES as a
    command — ``resolve_command`` sends an unknown slash to the model as prose.
    """

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = result
        backend.extension_commands = [(name, "") for name in registered]

    return prepare


def test_this_heads_commands_cannot_shadow_a_builtin() -> None:
    """``resolve_command`` gives a collision to the built-in, so a head word that
    collided would be unreachable — and offered by the completer anyway."""
    assert REPL_COMMANDS.isdisjoint(FRONTEND_COMMANDS)


async def test_a_performed_prints_what_the_capability_returned(env: ReplEnv) -> None:
    """The one arm every text head can report: the session already ran it."""
    env.install(
        _answers(
            performed_result(
                Performed(flow="greet", mutation="greet", data={"output": "hello there"})
            ),
            "greet",
        )
    )
    await env.run(["/greet"])
    assert "hello there" in env.text
    assert env.submissions == []


async def test_a_performed_with_no_output_prints_its_one_line_record(env: ReplEnv) -> None:
    """``Performed.summary()`` is written once in the core so three heads say the
    same sentence rather than each stringifying whatever came back."""
    env.install(
        _answers(
            performed_result(
                Performed(flow="name", mutation="set_session_name", data={"name": "docs"})
            )
        )
    )
    await env.run(["/name docs"])
    assert "set_session_name: name='docs'" in env.text


async def test_a_view_this_head_does_not_have_is_refused_not_sent_to_the_model(
    env: ReplEnv,
) -> None:
    """docs/TUI-STYLE-GUIDE.md §2.5: a frontend that cannot perform says so. The
    tree browser is a non-goal (§10), so ``/tree`` is exactly that case."""
    env.install(_answers(performed_result(View(name="tree", unavailable_because="no state yet"))))
    await env.run(["/tree"])
    assert "/tree resolved to a command this caller must perform" in env.text
    assert "REPL (tau --mode repl) cannot perform it" in env.text
    assert env.submissions == []


async def test_the_extensions_view_prints_the_listing_instead_of_opening_one(env: ReplEnv) -> None:
    """This head opens no view; the state a view would be drawn from is a read."""

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(
            View(name="extensions", unavailable_because="no state yet")
        )
        backend.agent_session.extension_state = LoadExtensionsResult(
            errors=[ExtensionLoadError(path="/x/broken.py", error="SyntaxError")]
        )

    env.install(prepare)
    await env.run(["/extensions"])
    assert "failed to load /x/broken.py: SyntaxError" in env.text
    assert env.submissions == []


async def test_a_ready_is_performed_through_the_backend_method_it_names(env: ReplEnv) -> None:
    """The generic arm: the capability names the method, the head calls it and
    reports the ``Performed`` it must answer with."""
    env.install(
        _answers(
            performed_result(
                Ready(flow="name", mutation="set_session_name", arguments={"name": "docs"})
            )
        )
    )
    await env.run(["/name docs"])
    assert env.backend is not None
    assert "set_session_name:docs" in env.backend.events
    assert "set_session_name: name='docs'" in env.text


async def test_a_ready_the_backend_cannot_perform_says_so(env: ReplEnv) -> None:
    """Fail-Early: the core may resolve a mutation this backend does not offer."""
    env.install(_answers(performed_result(Ready(flow="x", mutation="teleport", arguments={})), "x"))
    await env.run(["/x"])
    assert "performs 'teleport', which this backend does not offer" in env.text


async def test_compact_runs_the_cores_own_compaction_not_the_tuis(env: ReplEnv) -> None:
    """This head BINDS its log, so a shortened list handed back would be discarded;
    ``AgentSession.compact`` appends the entry the bound log reads through."""
    env.install(
        _answers(
            performed_result(
                Ready(
                    flow="compact",
                    mutation="compact",
                    arguments={"custom_instructions": "the auth bug"},
                )
            )
        )
    )
    await env.run(["/compact the auth bug"])
    assert env.backend is not None
    assert env.backend.agent_session.compactions == ["the auth bug"]
    assert "nothing to compact yet" in env.text


async def test_a_command_the_core_ran_a_turn_for_is_reported(env: ReplEnv) -> None:
    """An ``input`` hook rewrote the text after this head resolved it: the turn ran
    unrendered, and pretending otherwise is the divergence this must not hide."""
    env.install(_answers(SubmissionResult(accepted=True, submission_id="s")))
    await env.run(["/name docs"])
    assert "ran a TURN for it" in env.text


async def test_a_refused_command_shows_the_reason_the_core_gave(env: ReplEnv) -> None:
    """A lock refusal carries its own sentence; the head prints it verbatim."""
    env.install(
        _answers(
            SubmissionResult(
                accepted=False, submission_id="s", rejection_reason="an extension holds the session"
            )
        )
    )
    await env.run(["/name docs"])
    assert "an extension holds the session" in env.text


async def test_the_heads_own_commands_are_never_submitted_anywhere(env: ReplEnv) -> None:
    """``/reasoning`` is not in ``FRONTEND_COMMANDS``, so ``resolve_command`` would
    send it to the model as prose — it is peeked for by name first."""
    await env.run(["/reasoning"])
    assert env.backend is not None
    assert env.submissions == []
    assert env.backend.commands == []
    assert "reasoning is now printed in full" in env.text


async def test_tools_takes_one_of_two_words_and_refuses_the_rest(env: ReplEnv) -> None:
    """Stray text is refused rather than discarded (docs/SLASH-COMMANDS.md §4)."""
    await env.run(["/tools verbose", "/tools sideways"])
    assert "tool calls and results are now printed verbose" in env.text
    assert "/tools takes compact or verbose; got 'sideways'" in env.text


async def test_quit_ends_the_loop_without_reading_the_next_line(env: ReplEnv) -> None:
    """The line after ``/quit`` is never read — the loop returns instead."""
    assert await env.run(["/quit", "and then this"]) == 0
    assert env.submissions == []


async def test_a_turn_that_raises_is_reported_and_the_prompt_survives_it(env: ReplEnv) -> None:
    """A provider error on turn 30 ends the turn, never the session (§5)."""

    def prepare(backend: FakeBackend) -> None:
        async def explode(submission: Any, context: Any) -> SubmissionResult:
            backend.submissions.append(submission)
            raise RuntimeError("the provider hung up")

        backend.submit_turn = explode  # type: ignore[method-assign]

    env.install(prepare)
    assert await env.run(["first", "second"]) == 0
    assert "turn failed: RuntimeError: the provider hung up" in env.text
    assert len(env.submissions) == 2


@pytest.mark.parametrize("line", ["/quit now", "/reasoning please"])
async def test_a_head_command_with_stray_text_changes_nothing(env: ReplEnv, line: str) -> None:
    """Both of the argument-less words refuse rather than ignore the extra."""
    await env.run([line])
    assert "takes no argument" in env.text
