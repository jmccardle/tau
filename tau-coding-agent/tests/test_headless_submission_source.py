"""B2-c: print mode joins the one door.

docs/SUBMISSION-LIFECYCLE.md phase 3, part 3 — the spec's problem statement names
three routes to the model ("Headless … reaches the model by a different path. The
SDK reaches it by a third") and asks for one data model that holds for all of them.
``run_print`` now builds its OWN :class:`~tau_agent_core.submission.Submission` and
hands it to ``AgentSession.submit`` (via ``stream_submission`` / ``submit_command``)
instead of letting ``stream_chat`` derive one on its behalf.

What these pin:

* the three fields the work item turns on — ``source``/``submitter``
  (a human at a frontend that does not draw), ``allow_user_input=False`` (nobody is
  here to answer a dialog), ``expand_commands=True`` (argv is the operator's own
  text) — and that the record reaches ``submit()`` verbatim;
* **both output shapes are unaffected**: the text transcript is byte-identical, and
  ``--mode json`` emits the same lifecycle events it did, each carrying the
  provenance phase 2 stamped (now attributable to print mode's own submission id);
* a blocking dialog opened under a print-mode turn obeys the headless policy and
  does NOT hang — including in a process that HAS a UI delegate, which is what makes
  ``allow_user_input`` a per-submission capability rather than a per-process mode;
* the ``expand_commands`` boundary in both directions: an extension command runs,
  and a built-in that print mode cannot perform raises rather than silently
  travelling to the model as prompt text.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import tau_coding_agent.session_store as store
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.commands import UnsupportedCommandError
from tau_agent_core.submission import Submission
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, Usage
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import build_print_submission, run_print

# ── a scripted LLM boundary (same shape as test_json_mode / test_cost) ───────


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="qwen",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1000, output_tokens=500, total_tokens=1500, cache_read_tokens=0),
    )


class _EventIterator:
    def __init__(self, events):
        self._events = events
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return _EventIterator(self._events)

    async def result(self):
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self):
        pass


async def _fake_stream_simple(model, context, options=None):
    text = "ok"
    return _Stream(
        [
            TextDeltaEvent(delta=text, partial=_assistant(text)),
            DoneEvent(final=_assistant(text), usage=_assistant(text).usage),
        ]
    )


@pytest.fixture
def fake_llm():
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream_simple):
        yield


def _config() -> dict:
    return {
        "models": {
            "local-llm": {
                "backend": "openai",
                "model": "qwen",
                "base_url": "http://localhost:8080/v1",
                "api_key": "not-needed",
                "tools": [],  # no tools → a single completion, one message_end
            },
        },
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)


@pytest.fixture
def admitted(monkeypatch) -> list[Submission]:
    """Record every submission that reaches the real ``AgentSession.submit``."""
    seen: list[Submission] = []
    real_submit = AgentSession.submit

    async def recording(self, sub, *, context=None):
        seen.append(sub)
        return await real_submit(self, sub, context=context)

    monkeypatch.setattr(AgentSession, "submit", recording)
    return seen


# ── the record itself ────────────────────────────────────────────────────────


def test_print_submission_says_who_submitted_and_what_they_may_do():
    sub = build_print_submission("summarize the readme")

    # A human at a frontend that happens not to draw. NOT "rpc": print mode cannot
    # tell a person at a shell from a script, and τ has a real RPC transport that
    # can. What it CAN state truthfully is who the text came from.
    assert sub.source == "interactive"
    assert sub.submitter == "human"

    # Jupyter's allow_stdin, and the reason the source axis does not have to carry
    # non-interactivity: nobody is at this process to answer a modal.
    assert sub.allow_user_input is False

    # argv is the operator's own text — the same trust level as a keystroke in the
    # TUI, and the boundary B2-b draws is against text that ARRIVES while τ runs.
    assert sub.expand_commands is True

    # A fresh process has nothing in flight except a turn an extension's
    # session_start may have started; refusing the user's own prompt over that race
    # is the wrong answer, so wait for it.
    assert sub.multitask_strategy == "enqueue"

    # Left alone: False would ALSO skip the end-of-prompt drain and user_turn_end,
    # extension hooks a headless run fires today.
    assert sub.store_history is True
    assert sub.silent is False

    assert sub.text == "summarize the readme"
    assert sub.submission_id, "an unattributable submission cannot be a parent_header"
    assert build_print_submission("x").submission_id != build_print_submission("x").submission_id


async def test_run_print_admits_its_own_record_through_the_one_door(fake_llm, admitted):
    """The record print mode built is the record ``submit()`` admits — once."""
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0

    assert len(admitted) == 1, "the turn must be admitted exactly once"
    sub = admitted[0]
    assert sub.text == "hi"
    assert (sub.source, sub.submitter) == ("interactive", "human")
    assert sub.allow_user_input is False
    assert sub.expand_commands is True
    assert sub.multitask_strategy == "enqueue"


async def test_run_print_does_not_route_through_prompt(fake_llm, monkeypatch):
    """``prompt()`` is not the headless route any more (it would admit a second time)."""

    async def exploding_prompt(self, *a, **kw):
        raise AssertionError("run_print must reach the model through submit(), not prompt()")

    monkeypatch.setattr(AgentSession, "prompt", exploding_prompt)
    assert await run_print(CLIArgs(messages=["hi"], print_mode=True), _config()) == 0


# ── both output modes are unaffected ────────────────────────────────────────


async def test_text_mode_transcript_is_byte_identical(fake_llm, capsys):
    """The plain transcript: raw deltas, then one newline. Nothing else, ever."""
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0
    assert capsys.readouterr().out == "ok\n"


async def test_json_mode_emits_the_same_lifecycle_events(fake_llm, capsys):
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config())
    assert rc == 0

    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["type"] == "session"  # header FIRST (pi print-mode.ts:113-116)
    assert [e["type"] for e in lines[1:]] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert all("kind" not in e for e in lines)
    assert all(e.get("type") != "done" for e in lines)


async def test_json_mode_events_carry_the_print_submissions_provenance(fake_llm, admitted, capsys):
    """Phase 2's stated value — *"lets --mode json attribute events"* — on this path.

    The provenance fields are NOT a new schema: ``tau_event_to_pi_event`` serializes
    each event with ``model_dump(exclude_none=True)``, so ``submission_id`` /
    ``source`` / ``submitter`` / ``correlation`` have ridden the JSON stream since
    phase 2 stamped them onto ``AgentEvent``. What B2-c changes is WHOSE submission
    they name: print mode's own record rather than one the adapter derived for it.
    Nothing is added here, because there is nothing honest to add — a print run has
    no correlation detail (no bus subject, no cron id, no request id), and inventing
    one would be fabricated data on every event.
    """
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config())
    assert rc == 0

    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    events = lines[1:]  # line 0 is the session header, not an AgentEvent
    assert events, "the run emitted no events"
    for event in events:
        assert event["source"] == "interactive"
        assert event["submitter"] == "human"
        assert event["submission_id"] == admitted[0].submission_id


# ── a blocking dialog under a print-mode turn ───────────────────────────────

# An extension whose ``input`` hook — which fires INSIDE submit(), under the
# submission's published capability — opens a blocking confirm and records what
# happened. It also records the provenance the hook was handed, which is the same
# stamp the events carry.
_DIALOG_EXT = """
import json
from pathlib import Path

MARKER = Path(__file__).with_name("dialog.json")


def register(api):
    async def _on_input(event, ctx):
        record = {"source": event["source"], "submitter": event["submitter"]}
        try:
            record["answer"] = await ctx.ui.confirm("Proceed?", "may I")
        except Exception as exc:
            record["error"] = type(exc).__name__
        MARKER.write_text(json.dumps(record))
        return None

    api.on("input", _on_input)
"""


class _RecordingDelegate:
    """A UI delegate that must never be reached from a print-mode turn."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def confirm(self, title, message):
        self.calls.append(title)
        return True

    def notify(self, message, level="info"):
        pass


def _install_dialog_ext(tmp_path) -> tuple[str, Path]:
    ext = tmp_path / "dialog_ext.py"
    ext.write_text(_DIALOG_EXT)
    return str(ext), tmp_path / "dialog.json"


def _dialog_args(ext: str, **extra) -> CLIArgs:
    return CLIArgs(
        messages=["hi"],
        print_mode=True,
        mode="text",
        extensions=[ext],
        no_extensions=True,
        **extra,
    )


async def test_dialog_under_print_mode_raises_with_no_policy(fake_llm, tmp_path):
    """No ``--ui-defaults`` → HeadlessDialogError, promptly. Nothing is auto-answered."""
    ext, marker = _install_dialog_ext(tmp_path)

    rc = await asyncio.wait_for(run_print(_dialog_args(ext), _config()), timeout=10)
    assert rc == 0

    record = json.loads(marker.read_text())
    assert record["error"] == "HeadlessDialogError"
    assert "answer" not in record
    # The same provenance the events carry reached the hook chain.
    assert (record["source"], record["submitter"]) == ("interactive", "human")


async def test_dialog_under_print_mode_honours_ui_defaults(fake_llm, tmp_path):
    """``--ui-defaults confirm=yes`` is the explicit opt-in, and it still applies."""
    ext, marker = _install_dialog_ext(tmp_path)

    rc = await asyncio.wait_for(
        run_print(_dialog_args(ext, ui_defaults="confirm=yes"), _config()), timeout=10
    )
    assert rc == 0

    record = json.loads(marker.read_text())
    assert record["answer"] is True
    assert "error" not in record


async def test_dialog_cannot_reach_a_delegate_even_when_one_exists(fake_llm, tmp_path, monkeypatch):
    """The point of the capability being per-SUBMISSION rather than per-process.

    Print mode has no delegate today, so ``allow_user_input=False`` would be
    unobservable in a plain run — ``ExtensionUI._human_delegate`` already refuses on
    "no delegate" alone. Give the process one (an embedded τ, or a print run inside
    a host that has a screen) and the flag is the only thing standing between a
    nobody-is-watching turn and a modal nobody will answer: the dialog must take the
    headless route regardless, and the delegate must not be called.
    """
    ext, marker = _install_dialog_ext(tmp_path)
    delegate = _RecordingDelegate()

    from tau_coding_agent import backends as backends_module

    real_create = backends_module.create_backend

    def factory(config):
        backend = real_create(config)
        backend.set_ui_delegate(delegate)  # a live human is attached to this process
        return backend

    monkeypatch.setattr(backends_module, "create_backend", factory)

    rc = await asyncio.wait_for(run_print(_dialog_args(ext), _config()), timeout=10)
    assert rc == 0

    assert delegate.calls == [], "a print-mode submission must not open a modal"
    assert json.loads(marker.read_text())["error"] == "HeadlessDialogError"


# ── the expand_commands boundary ────────────────────────────────────────────

_TODOS_EXT = """
def register(api):
    def _todos(args, ctx):
        return "TODOS:" + args

    api.register_command("todos", {"description": "list todos", "handler": _todos})
"""


async def test_extension_command_still_dispatches_through_submit(tmp_path, capsys, admitted):
    """S46's channel, now resolved by the core's vocabulary instead of a local split.

    The observable behaviour is unchanged (the handler's text is printed and no turn
    runs); what changed is that the decision came from ``resolve_command`` and the
    dispatch happened inside ``AgentSession.submit`` — one answer to "what is a
    command", shared with the TUI.
    """
    ext = tmp_path / "todos_ext.py"
    ext.write_text(_TODOS_EXT)

    args = CLIArgs(
        messages=["/todos alpha"],
        print_mode=True,
        mode="text",
        extensions=[str(ext)],
        no_extensions=True,
    )
    assert await run_print(args, _config()) == 0
    assert capsys.readouterr().out == "TODOS:alpha\n"

    assert len(admitted) == 1
    assert admitted[0].expand_commands is True, "dispatch happened because we asked for it"


async def test_frontend_command_print_mode_cannot_perform_raises(tmp_path):
    """``tau -p "/compact"`` says it cannot, rather than sending eight characters to a model.

    The Fail-Early half of B2-b's seam: the core is allowed to resolve commands a
    given frontend cannot perform, and such a frontend must say so out loud. All four
    built-ins are frontend-shaped, and print mode has no frontend — ``/compact``
    specifically would summarize a working list this process discards on exit,
    because ``run_print`` does not bind its session as the AgentSession's log.
    """
    with pytest.raises(UnsupportedCommandError, match="print mode"):
        await run_print(
            CLIArgs(messages=["/compact"], print_mode=True, mode="text", no_extensions=True),
            _config(),
        )


# ── a command that arrives AFTER print mode's peek (the `input` hook seam) ───

# The 37_inline_bash shape: an `input` hook rewrites the typed text before the core
# resolves it. Print mode's peek saw "cc" — ordinary prompt text — so it took the
# turn path; submit() resolved the POST-hook text and dispatched a command instead.
# The alias expands to an extension command (performer="core", the handler already
# RAN inside submit and its output is on the result) or to a built-in
# (performer="frontend", which print mode cannot perform).
_ALIAS_EXT = """
def register(api):
    def _todos(args, ctx):
        return "TODOS:" + args

    api.register_command("todos", {"description": "list todos", "handler": _todos})

    async def _alias(event, ctx):
        if event["prompt"] == "cc":
            return {"prompt": "/todos alpha"}
        if event["prompt"] == "kk":
            return {"prompt": "/compact"}
        return None

    api.on("input", _alias)
"""


def _alias_args(ext: Path, text: str, mode: str = "text") -> CLIArgs:
    return CLIArgs(
        messages=[text],
        print_mode=True,
        mode=mode,
        extensions=[str(ext)],
        no_extensions=True,
    )


async def test_late_arriving_core_command_is_performed_not_swallowed(tmp_path, capsys, admitted):
    """A hook-rewritten prompt that resolves to a command must not print nothing.

    The regression: ``run_print``'s prompt path read only ``result.accepted`` and
    iterated ``result.messages``, which a dispatched command leaves empty — so the
    run exited 0 having written a bare newline, discarding output from a handler
    that had ALREADY run inside ``submit()``. Every sibling call site in the block
    acts on ``result.command``; this one absorbed it.
    """
    ext = tmp_path / "alias_ext.py"
    ext.write_text(_ALIAS_EXT)

    assert await run_print(_alias_args(ext, "cc"), _config()) == 0

    out = capsys.readouterr().out
    assert "TODOS:alpha" in out, "the command the hook expanded to must be reported"

    # One admission, and the text submit() saw is still the pre-hook text: the
    # rewrite happens inside submit(), which is exactly why print mode's own peek
    # could not see it.
    assert [s.text for s in admitted] == ["cc"]


async def test_late_arriving_core_command_emits_a_json_record(tmp_path, capsys, admitted):
    """``--mode json``: the dispatched command is a ``command_output`` record, not silence."""
    ext = tmp_path / "alias_ext.py"
    ext.write_text(_ALIAS_EXT)

    assert await run_print(_alias_args(ext, "cc", mode="json"), _config()) == 0

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    outputs = [r for r in records if r.get("type") == "command_output"]
    assert outputs == [{"type": "command_output", "command": "todos", "output": "TODOS:alpha"}]


async def test_late_arriving_frontend_command_raises_like_the_argv_form(tmp_path):
    """A hook that expands to ``/compact`` says print mode cannot, same as ``tau -p "/compact"``.

    Which side of the ``input`` hook the slash came from must not decide whether the
    command is reported or vanishes.
    """
    ext = tmp_path / "alias_ext.py"
    ext.write_text(_ALIAS_EXT)

    with pytest.raises(UnsupportedCommandError, match="print mode"):
        await run_print(_alias_args(ext, "kk"), _config())


async def test_unregistered_slash_is_still_prompt_text(fake_llm, admitted, capsys):
    """Unchanged, and deliberately: pasting a path must not become a refusal."""
    rc = await run_print(
        CLIArgs(messages=["/usr/bin/env is a path"], print_mode=True, no_extensions=True),
        _config(),
    )
    assert rc == 0
    assert capsys.readouterr().out == "ok\n"
    assert [s.text for s in admitted] == ["/usr/bin/env is a path"]
