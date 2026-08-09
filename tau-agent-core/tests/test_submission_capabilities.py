"""The three ``Submission`` capability fields that used to do nothing (B1-c).

docs/SUBMISSION-LIFECYCLE.md "The dataclasses". ``allow_user_input``,
``expand_commands`` and ``silent`` were set by callers, threaded through
``submit()``, and documented — and not one of them changed any behaviour. Under
this repo's Fail-Early rule that is worse than an absent field: it reads as a
working feature, so a cron-triggered submission "declaring" it may not prompt a
human happily opened a modal on whoever was at the terminal.

Each is settled differently here, according to what the code actually supports:

- ``allow_user_input`` is WIRED. Its enforcement mechanism already existed —
  :class:`HeadlessDialogError` and the ``--ui-defaults`` headless-answer policy —
  so ``submit()`` now publishes the capability for the turn it admits and
  ``ExtensionUI``'s blocking dialogs consult it. The spec's own words:
  "Enforcement stays HeadlessDialogError."
- ``expand_commands`` is LIVE as of B2-b — its consumer (command dispatch, moved
  out of the TUI's ``on_input_submitted``) is ``submit()`` step 3. What is pinned
  here is that the field GATES it; the dispatch behaviour itself, and the security
  property that a bus payload's "/compact" stays literal, are
  test_submit_commands.py's.
- ``silent`` RAISES. Suppressing renderer-visible output needs the multi-stream
  renderer contract of Block 3; see ``submit()``'s docstring for why the core
  cannot honour it at this seam. (Its admission case also lives in
  test_submit_admission.py, where the old "silent behaves as store_history=False"
  test used to be.)

The strategy/admission semantics are test_submit_admission.py's; the dataclass's
own normalisation and validation are test_submission.py's.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import ExtensionAPI, HeadlessDialogError
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import SUBMISSION_ALLOWS_USER_INPUT, Submission


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


def _sub(text: str, submission_id: str, **overrides) -> Submission:
    fields = {
        "text": text,
        "source": "timer",
        "submitter": "cron",
        "submission_id": submission_id,
        "multitask_strategy": "enqueue",
    }
    fields.update(overrides)
    return Submission(**fields)


class _Delegate:
    """A live TUI delegate — i.e. a real human is sitting there, reachable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.notifications: list[tuple[str, str]] = []

    async def confirm(self, title: str, message: str) -> bool:
        self.calls.append(("confirm", title))
        return True

    async def select(self, title: str, items: list[str]) -> str | None:
        self.calls.append(("select", title))
        return "chosen-by-a-human"

    async def input(self, title: str, default: str = "") -> str:
        self.calls.append(("input", title))
        return "typed-by-a-human"

    async def form(self, spec: dict) -> dict:
        self.calls.append(("form", spec.get("title", "")))
        return {"filled": "by-a-human"}

    def notify(self, message: str, level: str = "info") -> None:
        # Non-blocking, and deliberately recorded in its OWN list: a notification
        # is not a question, so it must never count as "the human was asked".
        self.notifications.append((level, message))


def _tui_session() -> tuple[AgentSession, _Delegate]:
    """A session whose ExtensionUI has a live delegate — the case the flag is FOR.

    A headless session proves nothing about ``allow_user_input``: it would raise
    anyway. The interesting configuration is a process that CAN reach a human and
    must decline to, because the submission driving the code said so.
    """
    session = _session()
    delegate = _Delegate()
    session._extension_api.context.set_ui_delegate(delegate)
    return session, delegate


def _dialog_hook(outcome: list, dialog: str = "confirm"):
    """An ``input`` hook that opens ``dialog`` and records what happened.

    Returns ``{"handled": True}`` so the submission is consumed without a model
    call — the hook runs INSIDE the admitted turn, which is exactly the code
    ``allow_user_input`` governs.
    """

    async def handler(event, ctx):
        try:
            if dialog == "confirm":
                outcome.append(await ctx.ui.confirm("Deploy?", "to production"))
            elif dialog == "select":
                outcome.append(await ctx.ui.select("Which?", ["a", "b"]))
            elif dialog == "input":
                outcome.append(await ctx.ui.input("Name?", "default-name"))
            elif dialog == "form":
                outcome.append(
                    await ctx.ui.form(
                        {"title": "Details", "fields": [{"name": "who", "kind": "text"}]}
                    )
                )
            else:  # pragma: no cover — a typo in a test, not a branch
                raise AssertionError(f"unknown dialog {dialog!r}")
        except HeadlessDialogError as err:
            outcome.append(err)
        return {"handled": True}

    return handler


# ── allow_user_input: wired, enforced by HeadlessDialogError ──────────────


class TestAllowUserInputGatesBlockingDialogs:
    async def test_false_does_not_reach_the_live_delegate_and_raises(self):
        """The defect, as an assertion: a timer-driven turn used to be able to
        put a modal in front of a human who did not originate it."""
        session, delegate = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        result = await session.submit(_sub("nightly run", "t-1", allow_user_input=False))

        assert result.accepted is True, "the submission is admitted; only the DIALOG is barred"
        assert delegate.calls == [], "the human was never asked"
        assert isinstance(outcome[0], HeadlessDialogError)

    async def test_true_reaches_the_delegate(self):
        """False and True must differ OBSERVABLY — otherwise the flag is still inert."""
        session, delegate = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        await session.submit(_sub("deploy please", "t-2", allow_user_input=True))

        assert delegate.calls == [("confirm", "Deploy?")]
        assert outcome == [True]

    async def test_false_with_a_ui_defaults_policy_answers_from_the_policy(self):
        """Enforcement is the HEADLESS-ANSWER path, not a hard block (the spec:
        "Enforcement stays HeadlessDialogError"). With an explicit policy the
        dialog resolves to the configured answer — still without asking a human."""
        session, delegate = _tui_session()
        session._extension_api.context.set_headless_ui_defaults({"confirm": "no"})
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        await session.submit(_sub("nightly run", "t-3", allow_user_input=False))

        assert outcome == [False], "the explicitly configured answer, not a fabricated True"
        assert delegate.calls == []

    async def test_the_error_names_allow_user_input_not_headless_mode(self):
        """A TUI user told to "run in the TUI" would hunt entirely the wrong thing."""
        session, _ = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        await session.submit(_sub("nightly run", "t-4", allow_user_input=False))

        message = str(outcome[0])
        assert "allow_user_input=False" in message
        assert "--ui-defaults confirm=" in message
        assert "headless mode" not in message

    @pytest.mark.parametrize("dialog", ["confirm", "select", "input", "form"])
    async def test_every_blocking_dialog_is_gated_not_just_confirm(self, dialog: str):
        """A half-gated surface is the same defect one level down: an extension
        that cannot ``confirm`` under a cron turn but can ``form`` has not been
        stopped from interrupting a human."""
        session, delegate = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome, dialog))

        await session.submit(_sub("nightly run", f"t-5-{dialog}", allow_user_input=False))

        assert isinstance(outcome[0], HeadlessDialogError)
        assert delegate.calls == []

    async def test_notify_is_not_gated(self):
        """``allow_user_input`` is about ASKING a human, not telling one; a
        non-blocking notification has no answer to fabricate."""
        session, delegate = _tui_session()
        seen: list = []

        async def handler(event, ctx):
            ctx.ui.notify("turn started", source="gate")
            seen.append(True)
            return {"handled": True}

        _bound(session, "/x/gate.py").on("input", handler)

        await session.submit(_sub("nightly run", "t-6", allow_user_input=False))

        assert seen == [True], "notify must not raise under allow_user_input=False"
        assert delegate.notifications == [("info", "turn started")], "it still paints"
        assert delegate.calls == [], "but not through the blocking-dialog path"


class TestAllowUserInputScope:
    async def test_outside_any_submission_a_dialog_still_reaches_the_delegate(self):
        """Nothing is published outside a submission-driven turn, so every
        pre-existing dialog path (``session_start``, a slash-command handler,
        ``continue_conversation()``) behaves exactly as it did."""
        session, delegate = _tui_session()

        answer = await session._extension_api.context.ui.confirm("Quit?", "unsaved work")

        assert answer is True
        assert delegate.calls == [("confirm", "Quit?")]

    async def test_the_restriction_is_lifted_when_the_turn_ends(self):
        session, delegate = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        await session.submit(_sub("nightly run", "t-7", allow_user_input=False))
        assert isinstance(outcome[0], HeadlessDialogError)

        # Same session, same UI, no turn in flight: the human is reachable again.
        assert await session._extension_api.context.ui.confirm("Quit?", "now") is True
        assert SUBMISSION_ALLOWS_USER_INPUT.get() is None

    async def test_a_task_the_turn_spawns_inherits_the_restriction(self):
        """Causal, not temporal — the reason this is a ContextVar. A task a
        cron-driven turn's hook spawned must not be able to open a dialog just
        because it got round to trying after that turn ended."""
        session, delegate = _tui_session()
        outcome: list = []
        spawned: list[asyncio.Task] = []
        released = asyncio.Event()

        async def late_dialog():
            await released.wait()
            try:
                return await session._extension_api.context.ui.confirm("Deploy?", "late")
            except HeadlessDialogError as err:
                return err

        async def handler(event, ctx):
            spawned.append(asyncio.get_running_loop().create_task(late_dialog()))
            return {"handled": True}

        _bound(session, "/x/gate.py").on("input", handler)

        await session.submit(_sub("nightly run", "t-8", allow_user_input=False))
        # The turn is over — and its restriction still binds the task it created.
        released.set()
        outcome.append(await asyncio.wait_for(spawned[0], timeout=1.0))

        assert isinstance(outcome[0], HeadlessDialogError)
        assert delegate.calls == []

    async def test_a_task_that_predates_the_turn_is_unaffected(self):
        """The other direction: an extension's long-lived subscription loop
        belongs to no submission, so a cron turn happening to be in flight must
        not silently disarm its dialogs. A session attribute gets this backwards."""
        session, delegate = _tui_session()
        started = asyncio.Event()

        async def pre_existing_loop():
            await started.wait()
            return await session._extension_api.context.ui.confirm("Deploy?", "from the loop")

        watcher = asyncio.get_running_loop().create_task(pre_existing_loop())
        await asyncio.sleep(0)  # let it start, so it captured the ambient context

        async def handler(event, ctx):
            started.set()
            await asyncio.sleep(0)
            return {"handled": True}

        _bound(session, "/x/gate.py").on("input", handler)

        await session.submit(_sub("nightly run", "t-9", allow_user_input=False))

        assert await asyncio.wait_for(watcher, timeout=1.0) is True
        assert delegate.calls == [("confirm", "Deploy?")]


# ── expand_commands: live since B2-b, and False is still the security default ──


class TestExpandCommandsIsLive:
    async def test_true_dispatches_the_built_in_instead_of_running_a_turn(self):
        session = _session()

        result = await session.submit(_sub("/compact", "e-1", expand_commands=True))

        assert result.accepted is True
        assert result.messages == []
        assert result.command is not None
        assert (result.command.name, result.command.performer) == ("compact", "frontend")

    async def test_false_is_admitted_normally(self):
        """The default, and what every non-interactive call site passes."""
        session = _session()
        outcome: list = []

        async def handler(event, ctx):
            outcome.append(event["prompt"])
            return {"handled": True}

        _bound(session, "/x/probe.py").on("input", handler)

        result = await session.submit(_sub("/compact", "e-3"))

        assert result.accepted is True
        assert result.command is None
        assert outcome == ["/compact"], "unexpanded — the text reaches the pipeline verbatim"


# ── silent: raises until Block 3 gives renderers a mark to honour ─────────


class TestSilentRaises:
    async def test_true_raises_naming_the_block_and_the_half_that_exists(self):
        session = _session()

        with pytest.raises(NotImplementedError) as excinfo:
            await session.submit(_sub("quietly", "s-1", silent=True))

        message = str(excinfo.value)
        assert "silent=True" in message
        assert "Block 3" in message
        assert "store_history=False" in message, "name the capability that DOES exist"

    async def test_store_history_false_is_still_available_and_still_works(self):
        """The half ``silent`` folded into is unchanged — it is simply asked for
        by its own name now."""
        session = _session()
        outcome: list = []

        async def handler(event, ctx):
            outcome.append(event["prompt"])
            return {"handled": True}

        _bound(session, "/x/probe.py").on("input", handler)

        result = await session.submit(_sub("quietly", "s-2", store_history=False))

        assert result.accepted is True
        assert outcome == ["quietly"]


# ── prompt(): the wrapper claims a capability that now exists ─────────────


@pytest.mark.usefixtures("fake_llm")
class TestPromptDeclaresExpandCommands:
    async def test_prompt_still_runs_a_turn_end_to_end(self):
        """The regression risk of touching ``expand_commands``: ``prompt()`` is the
        live path for the TUI backend, headless print mode, the RPC server and every
        SDK script. If the new dispatch could reach ordinary text, this fails."""
        session = _session()

        messages = await session.prompt("hello")

        assert messages, "the turn ran and produced messages"

    async def test_prompt_submits_expand_commands_true_and_allow_user_input_true(self):
        session = _session()
        captured: list[Submission] = []
        real_submit = session.submit

        async def recording_submit(sub, **kwargs):
            captured.append(sub)
            return await real_submit(sub, **kwargs)

        session.submit = recording_submit  # type: ignore[method-assign]
        await session.prompt("hello")

        (sub,) = captured
        assert sub.source == "interactive"
        assert sub.submitter == "human"
        assert sub.multitask_strategy == "enqueue"
        # True since B2-b: the spec's "Interactive frontends pass True" is a
        # behaviour now, not an intent. prompt() itself refuses a command up front
        # (test_submit_commands.py) because its list[dict] return has nowhere to
        # put the outcome.
        assert sub.expand_commands is True
        # True: a human typed this, so a hook under this turn MAY ask them.
        assert sub.allow_user_input is True

    async def test_a_dialog_under_prompt_reaches_the_human(self):
        """The positive half of the ``allow_user_input`` wiring on the live path:
        an interactive turn is exactly the case that must still be able to ask."""
        session, delegate = _tui_session()
        outcome: list = []
        _bound(session, "/x/gate.py").on("input", _dialog_hook(outcome))

        await session.prompt("deploy it")

        assert delegate.calls == [("confirm", "Deploy?")]
        assert outcome == [True]
