"""Command dispatch inside ``submit()`` — the decision, and the flag that gates it.

Reference: docs/SUBMISSION-LIFECYCLE.md ``submit()`` step 3 ("**Command dispatch**, if
``expand_commands``. This is the logic that must move out of ``on_input_submitted``"), B2-b.

Two things are being pinned here, and they are equally load-bearing.

**The move.** ``/compact``, ``/tree``, ``/fork``, ``/extensions`` and every
extension-registered ``/name args`` used to be intercepted inside a Textual event handler, so
no other input source had a command vocabulary at all. The decision now lives in
:mod:`tau_agent_core.commands`, is taken in ``submit()``, and is reported as a typed
:class:`~tau_agent_core.commands.CommandOutcome` — with the split that makes it possible for a
core to dispatch a command it cannot itself perform: the core RUNS an extension command (any
frontend can render the string it returns) and hands a built-in BACK (the core cannot push a
Textual screen).

**The flag.** ``expand_commands`` defaults to ``False`` and the spec is emphatic that this is a
security property: "pi's sendUserMessage sets expandPromptTemplates: false, so injected text can
never smuggle a '/compact' through a bus payload." So the same eight characters must compact when
a human types them and reach the model as literal prompt text when a NATS payload carries them.
Both directions are asserted below against the SAME session, because a test that only checks one
proves nothing about the boundary.

The dataclasses' own construction rules are test_submission.py's; admission/strategy semantics
are test_submit_admission.py's; the TUI half (performing a ``performer="frontend"`` outcome, and
raising when it cannot) is tau-coding-agent/tests/test_app_command_dispatch.py's.
"""

from __future__ import annotations

import pytest

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.commands import (
    CommandInvocation,
    UnsupportedCommandError,
    parse_command,
    resolve_command,
)
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


def _bound(session: AgentSession, path: str) -> ExtensionAPI:
    """The api a loaded extension at ``path`` is handed — the real binding path."""
    return session._bind_extension_api(path)


def _human(text: str, submission_id: str, **overrides) -> Submission:
    """What a human at an interactive frontend submits: dispatch declared ON."""
    fields = {
        "text": text,
        "source": "interactive",
        "submitter": "human",
        "submission_id": submission_id,
        "multitask_strategy": "enqueue",
        "expand_commands": True,
        "allow_user_input": True,
    }
    fields.update(overrides)
    return Submission(**fields)  # type: ignore[arg-type]


def _bus(text: str, submission_id: str, **overrides) -> Submission:
    """What a NATS payload submits: the defaults, i.e. dispatch declared OFF."""
    fields = {
        "text": text,
        "source": "bus",
        "submitter": "nats:jobs.inbound",
        "submission_id": submission_id,
        "multitask_strategy": "enqueue",
    }
    fields.update(overrides)
    return Submission(**fields)  # type: ignore[arg-type]


def _record_input(session: AgentSession, seen: list[str]) -> None:
    """An ``input`` hook that records the text and consumes it.

    ``handled`` means no model call, which lets a test assert exactly what text WOULD have
    been sent without needing a provider. It fires for every admitted submission, so it is
    also how "the bus payload was treated as prompt text" is observed rather than inferred.

    Note that ``handled`` short-circuits BEFORE dispatch (spec step order 2 then 3), so a
    test that needs a command to actually dispatch must not install this on that path.
    """

    async def handler(event, ctx):
        seen.append(event["prompt"])
        return {"handled": True}

    _bound(session, "/x/probe.py").on("input", handler)


def _record_bus_input(session: AgentSession, seen: list[str]) -> None:
    """:func:`_record_input`, but consuming only ``source="bus"`` submissions.

    Lets one session assert both directions of the ``expand_commands`` boundary: the bus
    payload is observed as prompt text (consumed, so no provider is needed) while the human
    submission is left alone to reach dispatch.
    """

    async def handler(event, ctx):
        if event["source"] != "bus":
            return None
        seen.append(event["prompt"])
        return {"handled": True}

    _bound(session, "/x/probe.py").on("input", handler)


def _register(session: AgentSession, name: str, calls: list[tuple[str, str]], output=None):
    """Register an extension command whose handler records its args and returns ``output``."""

    def _handler(args, ctx):
        calls.append((name, args))
        return output

    _bound(session, f"/x/{name}.py").register_command(
        name, {"description": f"the {name} command", "handler": _handler}
    )


# ── the pure decision ──────────────────────────────────────────────────────────


class TestResolveCommand:
    def test_plain_text_is_not_a_command(self):
        assert resolve_command("compact the log please") is None

    def test_built_ins_resolve_to_the_frontend(self):
        assert resolve_command("/compact") == CommandInvocation("compact", "", "frontend")
        assert resolve_command("/tree") == CommandInvocation("tree", "", "frontend")
        assert resolve_command("/fork") == CommandInvocation("fork", "", "frontend")

    def test_arguments_split_on_the_first_space_and_keep_their_spacing(self):
        assert resolve_command("/extensions disable  my ext") == CommandInvocation(
            "extensions", "disable  my ext", "frontend"
        )

    def test_a_registered_extension_command_resolves_to_the_core(self):
        assert resolve_command("/greet world", ["greet"]) == CommandInvocation(
            "greet", "world", "core"
        )

    def test_an_unknown_slash_falls_through_to_the_model(self):
        """Unchanged behaviour, and deliberately so: refusing every unrecognised
        slash would break pasting an absolute path as a prompt."""
        assert resolve_command("/usr/local/bin/tau --help", ["greet"]) is None

    def test_a_built_in_cannot_be_shadowed_by_an_extension(self):
        """Resolution order mirrors what on_input_submitted did: built-ins first."""
        assert resolve_command("/compact", ["compact"]).performer == "frontend"

    def test_parse_command_is_purely_syntactic(self):
        assert parse_command("/x") == ("x", "")
        assert parse_command("x") is None
        assert parse_command("  /x y  ") == ("x", "y")

    async def test_the_session_reads_its_live_registry(self):
        """The frontend peek and submit() must see the same set, including a command
        registered after the session was built."""
        session = _session()
        assert session.resolve_command("/greet hi") is None

        _register(session, "greet", [])

        assert session.resolve_command("/greet hi") == CommandInvocation("greet", "hi", "core")


# ── the flag IS the boundary: same text, two sources, two meanings ─────────────


class TestExpandCommandsIsTheSecurityBoundary:
    async def test_a_human_slash_compact_dispatches(self):
        session = _session()
        before = list(session._session_log.entries())

        result = await session.submit(_human("/compact", "h-1"))

        assert result.accepted is True
        assert result.command is not None
        assert (result.command.name, result.command.performer) == ("compact", "frontend")
        # No turn: nothing was sent, and no user node joined the log.
        assert result.messages == []
        assert session._session_log.entries() == before

    async def test_a_bus_slash_compact_is_literal_prompt_text(self):
        """The whole point of the flag. An injected payload cannot smuggle a command."""
        session = _session()
        seen: list[str] = []
        _record_input(session, seen)

        result = await session.submit(_bus("/compact", "b-1"))

        assert result.accepted is True
        assert result.command is None, "a bus payload must not dispatch a command"
        assert seen == ["/compact"], "it reached the turn pipeline as ordinary text"

    async def test_a_bus_payload_cannot_smuggle_an_extension_command_either(self):
        """Not just the built-ins: a registered handler must not be reachable from a
        payload, which is the reason the spec says an extension calls the typed API."""
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "deploy", calls)
        seen: list[str] = []
        _record_input(session, seen)

        result = await session.submit(_bus("/deploy prod", "b-2"))

        assert calls == [], "the handler must not have run"
        assert result.command is None
        assert seen == ["/deploy prod"]

    async def test_both_directions_hold_on_one_session(self):
        """Asserted together so the boundary is the FLAG and not some per-session state."""
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "deploy", calls)
        seen: list[str] = []
        _record_bus_input(session, seen)

        assert (await session.submit(_bus("/deploy prod", "x-1"))).command is None
        assert calls == []
        assert seen == ["/deploy prod"]

        human = await session.submit(_human("/deploy prod", "x-2"))
        assert human.command is not None
        assert calls == [("deploy", "prod")]


# ── the core/frontend split ────────────────────────────────────────────────────


class TestDispatchOutcomes:
    async def test_an_extension_command_is_RUN_by_the_core_and_reports_its_output(self):
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "todos", calls, output="# Todos\n- one")

        result = await session.submit(_human("/todos mine", "c-1"))

        assert calls == [("todos", "mine")]
        assert result.command is not None
        assert result.command.performer == "core"
        assert result.command.output == "# Todos\n- one"

    async def test_a_command_that_returns_nothing_has_no_output(self):
        """A command that ran and had nothing to say — distinct from one that did not
        run, which is an exception rather than a None."""
        session = _session()
        _register(session, "ping", [])

        result = await session.submit(_human("/ping", "c-2"))

        assert result.command is not None
        assert result.command.performer == "core"
        assert result.command.output is None

    async def test_a_built_in_is_handed_back_unperformed(self):
        """The core cannot push a Textual screen, so it says what the command IS and
        stops. Performing it — and raising when it cannot — is the frontend's."""
        session = _session()

        result = await session.submit(_human("/extensions disable foo", "c-3"))

        assert result.command is not None
        assert result.command.name == "extensions"
        assert result.command.args == "disable foo"
        assert result.command.performer == "frontend"
        assert result.command.output is None

    async def test_an_unknown_slash_still_runs_a_turn(self):
        session = _session()
        seen: list[str] = []
        _record_input(session, seen)

        result = await session.submit(_human("/usr/bin/env python", "c-4"))

        assert result.command is None
        assert seen == ["/usr/bin/env python"]

    async def test_a_command_unregistered_mid_flight_raises_rather_than_prompting(self):
        """resolve_command found it; run_extension_command no longer knows it.

        The window is narrow (an extension reload landing between the decision and the
        invocation), so it is forced here rather than raced. What is being pinned is the
        REACTION: falling through to "send it to the model" would ship the user's
        ``/note buy milk`` to an LLM as a prompt, which is worse than an exception.
        """
        session = _session()
        _register(session, "note", [])

        async def _gone(name, args=""):
            from tau_agent_core.agent_session import ExtensionCommandResult

            return ExtensionCommandResult(handled=False)

        session.run_extension_command = _gone  # type: ignore[method-assign]

        with pytest.raises(UnsupportedCommandError, match="unregistered"):
            await session.submit(_human("/note buy milk", "c-5"))

    async def test_dispatch_releases_the_turn_slot(self):
        """A command is not a turn, so the session must be idle straight afterwards —
        otherwise the next prompt would queue behind a command that already finished."""
        session = _session()

        await session.submit(_human("/compact", "c-6"))

        assert session.is_streaming is False
        assert session._turn_lock.locked() is False


# ── the input hook chain still runs first (spec step order 2 then 3) ──────────


class TestInputHooksPrecedeDispatch:
    async def test_a_hook_sees_the_command_text_and_may_rewrite_its_arguments(self):
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "deploy", calls)

        async def _rewrite(event, ctx):
            return {"prompt": event["prompt"].replace("prod", "staging"), "images": None}

        _bound(session, "/x/guard.py").on("input", _rewrite)

        await session.submit(_human("/deploy prod", "i-1"))

        assert calls == [("deploy", "staging")], "dispatch resolves the POST-hook text"

    async def test_a_hook_that_consumes_the_input_pre_empts_dispatch(self):
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "deploy", calls)

        async def _consume(event, ctx):
            return {"handled": True}

        _bound(session, "/x/veto.py").on("input", _consume)

        result = await session.submit(_human("/deploy prod", "i-2"))

        assert result.accepted is True
        assert result.command is None
        assert calls == [], "a consumed input never reaches dispatch"


# ── prompt(): no channel for an outcome, so it refuses rather than swallowing ──


class TestPromptRefusesCommands:
    async def test_prompt_raises_on_a_command_before_anything_runs(self):
        session = _session()
        calls: list[tuple[str, str]] = []
        _register(session, "deploy", calls)
        before = list(session._session_log.entries())

        with pytest.raises(UnsupportedCommandError, match="/deploy"):
            await session.prompt("/deploy prod")

        # Checked BEFORE submit(): the handler did not run and then get reported as
        # an error, which would be a side effect wearing a refusal's clothes.
        assert calls == []
        assert session._session_log.entries() == before
        assert session.is_streaming is False

    async def test_prompt_raises_on_a_built_in_too(self):
        session = _session()

        with pytest.raises(UnsupportedCommandError, match="/compact"):
            await session.prompt("/compact")

    async def test_prompt_still_sends_an_unknown_slash_to_the_model(self):
        """The compatibility half: a pasted path is a prompt, not a refusal."""
        session = _session()
        seen: list[str] = []
        _record_input(session, seen)

        await session.prompt("/etc/hosts is missing")

        assert seen == ["/etc/hosts is missing"]
