"""A fake backend for the REPL-head suite, in the shape ``run_repl`` drives.

The idiom ``test_headless_lifecycle.py`` established — a recording double
installed over ``tau_coding_agent.backends.create_backend`` — with the two extra
seams the REPL uses and print mode does not: it BINDS a session log, and it
renders from a persistent ``subscribe_render`` subscription rather than from a
per-turn stream. So the double records the bind, hands the head a router, and
replays a scripted render stream while a turn is awaited.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from rich.console import Console

import tau_coding_agent.session_store as store
from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.capabilities import BUILTIN, Vocabulary
from tau_agent_core.compaction import CompactionResult
from tau_agent_core.extension_locks import ExtensionRequest
from tau_agent_core.flows import Dispatched, Performed
from tau_agent_core.sdk import LoadExtensionsResult
from tau_agent_core.submission import Submission, SubmissionResult
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.repl import run_repl
from tau_coding_agent.repl_input import MemoryReader

CONFIG: dict[str, Any] = {
    "models": {
        "local-llm": {
            "backend": "openai",
            "model": "qwen3-32b-kv4b",
            "base_url": "http://localhost:8080/v1",
            "api_key": "not-needed",
        },
        "other": {
            "backend": "openai",
            "model": "other-id",
            "base_url": "http://localhost:8080/v1",
            "api_key": "not-needed",
        },
    },
    "default_model": "local-llm",
    "system_prompt": "You are helpful.",
}


class FakeAgentSession:
    """The half of ``AgentSession`` the REPL's startup and loop touch.

    Attributes:
        vocabulary: What the flow loop and the completer read; a test swaps in an
            overlay to drive an extension-declared flow.
        model_resolver: What ``model_name`` is enumerated from — the real resolver
            ``run_repl`` binds, kept rather than dropped.
        extension_state: What the ``/extensions`` listing is built from.
        compactions: Every ``custom_instructions`` ``/compact`` was run with.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.shutdown_requested = False
        self.routed: list[dict] = []
        self.vocabulary: Vocabulary = BUILTIN
        self.model_resolver: Any = None
        self.extension_state = LoadExtensionsResult()
        self.compactions: list[str | None] = []
        self.compaction_result: CompactionResult | None = None

    def set_model_resolver(self, resolver: Any) -> None:
        self.events.append("set_model_resolver")
        self.model_resolver = resolver

    def get_extension_state(self) -> LoadExtensionsResult:
        return self.extension_state

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult | None:
        self.events.append("compact")
        self.compactions.append(custom_instructions)
        return self.compaction_result

    def route_session_event(self, event: dict) -> None:
        self.routed.append(event)

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        self.events.append(f"shutdown:{reason}")


class FakeRouter:
    """What ``subscribe_render`` returns: two teardown calls, both recorded."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def detach(self) -> None:
        self.events.append("detach")

    async def close_all(self) -> None:
        self.events.append("close_all")


class FakeBackend:
    """Records the lifecycle calls and replays a scripted render stream.

    Attributes:
        events: Every lifecycle call, in order — the startup-order assertion.
        submissions: Every :class:`Submission` this head built, in order.
        extension_runs: Every ``run_extension_command`` call, in order.
        extension_output: What that call's handler returns.
        contexts: The ``context`` argument of each ``submit_turn``.
        script: Render events replayed into the handler during a turn.
        turn_result: What ``submit_turn`` returns; the default accepts.
        command_result: What ``submit_command`` returns.
        steer_result: What ``submit_turn`` returns for a ``"steer"`` submission.
        aborts: How many times :meth:`abort` was called.
        delegate: The UI delegate the head installed, so a test can call the four
            methods an extension would.
        pending_request: What the head reads at every cursor move; a test arms it.
        answers: Every ``answer_request`` call, in order.
        answer_result: What that call returns.
        shortcuts: What ``get_extension_shortcuts`` lists.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.system_prompt = "sys"
        self.events: list[str] = []
        self.submissions: list[Submission] = []
        self.commands: list[Submission] = []
        self.contexts: list[Any] = []
        self.bound: list[Any] = []
        self.script: list[dict[str, Any]] = []
        self.turn_result: SubmissionResult | None = None
        self.command_result: SubmissionResult | None = None
        self.steer_result: SubmissionResult | None = None
        self.aborts = 0
        self.extension_commands: list[tuple[str, str]] = []
        self.extension_runs: list[tuple[str, str]] = []
        self.extension_output: Any = "the handler ran"
        self.agent_session = FakeAgentSession(self.events)
        self.delegate: Any = None
        self.pending_request: ExtensionRequest | None = None
        self.answers: list[tuple[str, str, dict[str, Any]]] = []
        self.answer_result = ExtensionCommandResult(handled=True)
        self.shortcuts: list[tuple[str, str, str, str]] = []
        self._handler: Any = None

    def set_ui_delegate(self, delegate: Any) -> None:
        self.events.append("set_ui_delegate")
        self.delegate = delegate

    async def answer_request(
        self, request_id: str, action: str, values: dict[str, Any]
    ) -> ExtensionCommandResult:
        self.events.append(f"answer_request:{action}")
        self.answers.append((request_id, action, values))
        self.pending_request = None
        return self.answer_result

    def get_extension_shortcuts(self) -> list[tuple[str, str, str, str]]:
        return list(self.shortcuts)

    def bind_session_log(self, session_log: Any) -> None:
        self.events.append("bind_session_log")
        self.bound.append(session_log)

    def set_session_name(self, name: str) -> Performed:
        self.events.append(f"set_session_name:{name}")
        return Performed(flow=None, mutation="set_session_name", data={"name": name})

    def set_auto_compaction(self, enabled: bool) -> Performed:
        self.events.append(f"set_auto_compaction:{enabled}")
        return Performed(flow=None, mutation="set_auto_compaction", data={"enabled": enabled})

    async def run_extension_command(self, name: str, args: str = "") -> ExtensionCommandResult:
        self.extension_runs.append((name, args))
        return ExtensionCommandResult(handled=True, output=self.extension_output)

    def record_model_change(self, name: str) -> None:
        self.events.append(f"record_model_change:{name}")

    def subscribe_render(self, handler: Any, *, on_orphan: Any = None) -> FakeRouter:
        self.events.append("subscribe_render")
        self._handler = handler
        self.on_orphan = on_orphan
        return FakeRouter(self.events)

    async def load_extensions(
        self,
        explicit_paths: list[str] | None = None,
        *,
        discover: bool = True,
        user_dir: str | None = None,
        extensions_config: dict[str, dict[str, Any]] | None = None,
    ) -> LoadExtensionsResult:
        self.events.append("load_extensions")
        return LoadExtensionsResult()

    async def emit_session_start(self, reason: str = "startup") -> None:
        self.events.append(f"session_start:{reason}")

    def get_extension_commands(self) -> list[tuple[str, str]]:
        return list(self.extension_commands)

    def abort(self) -> None:
        self.events.append("abort")
        self.aborts += 1

    async def submit_turn(self, submission: Submission, context: Any) -> SubmissionResult:
        self.events.append("submit_turn")
        self.submissions.append(submission)
        self.contexts.append(context)
        if submission.multitask_strategy == "steer":
            # A queued steer opens no lane: the core returns before on_submission_start.
            if self.steer_result is not None:
                return self.steer_result
            return SubmissionResult(
                accepted=True, submission_id=submission.submission_id, messages=[]
            )
        await self.replay(submission.submission_id)
        if self.turn_result is not None:
            return self.turn_result
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)

    async def submit_command(self, submission: Submission) -> SubmissionResult:
        self.events.append("submit_command")
        self.commands.append(submission)
        if self.command_result is not None:
            return self.command_result
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)

    async def replay(self, lane: str) -> None:
        """Feed the scripted render events to the subscribed handler.

        One suspension per event, because a real router awaits the bus between
        them: without it a whole turn would run in a single step of the loop and
        no line could ever be typed during one.
        """
        for event in self.script:
            await asyncio.sleep(0)
            result = self._handler({**event, "lane": event.get("lane", lane)})
            if result is not None:
                await result


def performed_result(dispatched: Dispatched, submission_id: str = "s") -> SubmissionResult:
    """A ``submit_command`` outcome carrying one dispatched arm."""
    return SubmissionResult(accepted=True, submission_id=submission_id, command=dispatched)


class ReplEnv:
    """A hermetic REPL run: a temp session store and one recording backend.

    Attributes:
        config: The config ``run_repl`` is handed; a test mutates it before
            :meth:`run` to exercise a config-driven decision.
        console: The recording console every assertion on output reads.
        reader: The scripted terminal of the last :meth:`run`.
        backend: The double the last run built, or ``None`` when it built none.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        """
        Args:
            monkeypatch: Redirects the session store and ``create_backend``.
            tmp_path: The session store's root, so no run touches ``~/.tau``.
        """
        self._monkeypatch = monkeypatch
        monkeypatch.setattr(store, "TAU_DIR", tmp_path)
        self.config: dict[str, Any] = dict(CONFIG)
        self.console = Console(record=True, width=100, force_terminal=False)
        self.reader = MemoryReader()
        self.backend: FakeBackend | None = None
        self.install()

    def install(self, prepare: Callable[[FakeBackend], None] | None = None) -> None:
        """Install a ``create_backend`` that builds — and optionally arms — the double."""
        self.backend = None

        def factory(config: dict[str, Any]) -> FakeBackend:
            backend = FakeBackend(config)
            if prepare is not None:
                prepare(backend)
            self.backend = backend
            return backend

        self._monkeypatch.setattr("tau_coding_agent.backends.create_backend", factory)

    async def run(self, lines: list[str], **kwargs: Any) -> int:
        """Drive ``run_repl`` over a scripted reader and return its exit code."""
        answers = kwargs.pop("answers", None)
        self.reader = MemoryReader(lines=lines, answers=answers)
        return await run_repl(
            CLIArgs(mode="repl", **kwargs), self.config, reader=self.reader, console=self.console
        )

    @property
    def text(self) -> str:
        """Everything printed so far."""
        return self.console.export_text(clear=False)

    @property
    def events(self) -> list[str]:
        """The lifecycle calls the last run's backend recorded, in order."""
        assert self.backend is not None, "no backend was built"
        return self.backend.events

    @property
    def submissions(self) -> list[Submission]:
        """Every submission the head built, in order."""
        assert self.backend is not None, "no backend was built"
        return self.backend.submissions


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> ReplEnv:
    """One hermetic REPL environment per test."""
    return ReplEnv(monkeypatch, tmp_path)
