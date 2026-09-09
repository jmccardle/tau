"""The extension surfaces at the prompt line — docs/REPL-HEAD.md §7.

The four delegate methods, an extension request (lock and ask), a panel action
pressed as a typed command, and the shortcut listing. The specs are built with
the core's own validators — ``validate_form_spec``/``validate_panel_spec``/
``validate_ask_spec`` — rather than by importing ``test_extension_form.py`` or
``test_extension_locks_tui.py``, whose modules import Textual: the shapes are
theirs, the door they come through is not.

The one deliberate Textual import is :func:`test_the_panel_body_port_still_matches_the_tuis`,
which is the anti-drift check the ported ``render_panel_body`` exists under.
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.console import Console

from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.extension_locks import (
    REQUEST_ENTRY_TYPE,
    ExtensionRequest,
    build_request_data,
    read_request,
    refusal_reason,
)
from tau_agent_core.extension_types import validate_ask_spec, validate_panel_spec
from tau_agent_core.flows import View
from tau_agent_core.sdk import LoadExtensionsResult
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.repl import (
    GLYPH,
    ROLE_STYLE,
    ReplDelegate,
    ReplRenderer,
    render_panel_body,
)
from tau_coding_agent.repl_input import INTERRUPT, MemoryReader

from repl_fakes import FakeBackend, ReplEnv, env, performed_result  # noqa: F401

_FORM = {
    "title": "New task",
    "fields": [
        {"name": "desc", "kind": "text", "label": "Description", "default": "draft"},
        {"name": "prio", "kind": "select", "options": ["low", "high"], "default": "high"},
        {"name": "tags", "kind": "multiselect", "options": ["a", "b", "c"], "default": ["b"]},
        {"name": "urgent", "kind": "confirm", "label": "Urgent?", "default": True},
        {"name": "points", "kind": "number", "default": 3},
    ],
}
"""``test_extension_form.py``'s spec, reused for its five field kinds."""

_PANEL = validate_panel_spec(
    {
        "title": "Release gate",
        "table": {"columns": ["step", "state"], "rows": [["tests", "green"], ["deploy", "held"]]},
        "actions": [{"label": "Approve", "command": "gate-approve", "args": "now"}],
    }
)

_ASK = validate_ask_spec(
    {
        "title": "Release gate",
        "text": "Say who approved it.",
        "fields": [{"name": "approver", "kind": "text", "default": "nobody"}],
        "actions": [{"label": "Approve", "command": "gate-approve"}],
    }
)


def _request(*, lock: bool = True, ask: dict[str, Any] | None = _ASK) -> ExtensionRequest:
    """One request entry, read back the way a head reads it off the tree."""
    entry = {
        "type": "customEntry",
        "customType": REQUEST_ENTRY_TYPE,
        "id": "req-1",
        "data": build_request_data(
            "/x/release_gate.py",
            "A deploy-shaped command ran.",
            lock=lock,
            ask=ask,
            release="gate-clear",
        ),
    }
    request = read_request(entry)
    assert request is not None
    return request


def _delegate(answers: list[str] | None = None) -> tuple[ReplDelegate, MemoryReader, Console]:
    """A delegate over a recording console, called the way an extension calls it."""
    console = Console(record=True, width=100, force_terminal=False)
    reader = MemoryReader(answers=answers)
    renderer = ReplRenderer(console, reader, model_name="local-llm")

    async def alone(ask: Any) -> Any:
        return await ask()

    return ReplDelegate(renderer, reader, alone), reader, console


def _arm(prepare: Any) -> Any:
    """Wrap a delegate call into ``load_extensions``, where an extension's own runs."""

    def install(backend: FakeBackend) -> None:
        async def load(paths: Any = None, **kwargs: Any) -> LoadExtensionsResult:
            backend.events.append("load_extensions")
            prepare(backend)
            return LoadExtensionsResult()

        backend.load_extensions = load  # type: ignore[method-assign]

    return install


def test_every_notify_level_names_a_role_this_palette_has() -> None:
    """The level an extension passes picks a STYLE, so a level with no role would
    print in the terminal's default and say nothing about how urgent it was."""
    for role in ReplDelegate.LEVELS.values():
        assert role in ROLE_STYLE and role in GLYPH


def test_notify_prints_each_level_with_its_own_marker() -> None:
    """Three levels, three glyphs: what distinguishes them survives a pipe, where
    a colour does not."""
    delegate, _reader, console = _delegate()
    delegate.notify("indexed 12 files")
    delegate.notify("the index is stale", "warning")
    delegate.notify("the index is gone", "error")
    text = console.export_text()
    assert f"{GLYPH['extension']} indexed 12 files" in text
    assert f"{GLYPH['warning']} the index is stale" in text
    assert f"{GLYPH['error']} the index is gone" in text


def test_a_level_this_palette_does_not_know_keeps_its_name() -> None:
    """Fail-Early: the level is information about the message, so an unknown one is
    printed WITH its name rather than quietly as an info line."""
    delegate, _reader, console = _delegate()
    delegate.notify("the disk is full", "critical")
    assert "critical: the disk is full" in console.export_text()


async def test_a_form_is_asked_field_by_field_and_typed_by_its_kind() -> None:
    """§6's one control table, reached through ``api.ui.form``: rich prints the
    choices, the reader takes the answer, and each kind names the type."""
    delegate, reader, _console = _delegate(["a walkthrough", "2", "1, 3", "y", "8"])
    answers = await delegate.form(_FORM)
    assert answers == {
        "desc": "a walkthrough",
        "prio": "high",
        "tags": ["a", "c"],
        "urgent": True,
        "points": 8,
    }
    assert reader.asked == [
        "Description",
        "prio [1-2]",
        "tags [1-3, comma-separated]",
        "Urgent? [y/n]",
        "points",
    ]


async def test_a_cancelled_form_is_none_and_no_answer_is_fabricated() -> None:
    """A cancelled form is not a fabricated answer set (``ExtensionUI.form``'s own
    contract), so the Ctrl+C on field two loses field one as well."""
    delegate, _reader, console = _delegate(["a walkthrough", INTERRUPT])
    assert await delegate.form(_FORM) is None
    assert "New task: cancelled" in console.export_text()


def test_status_slots_ride_in_the_prompt_prefix_in_first_seen_order() -> None:
    """``set_status`` paints ambient state where a TUI has a strip: the prefix."""
    delegate, reader, _console = _delegate()
    delegate.set_status("budget", "3 turns left")
    delegate.set_status("index", "fresh")
    delegate.set_status("budget", "2 turns left")
    assert reader.prefixes == [
        "[budget: 3 turns left] ",
        "[budget: 3 turns left] [index: fresh] ",
        "[budget: 2 turns left] [index: fresh] ",
    ]


def test_clearing_the_last_slot_leaves_the_bare_marker() -> None:
    """``None`` removes a slot (``ExtensionUI.set_status``'s semantics); the last
    one leaving takes the whole prefix with it rather than an empty bracket."""
    delegate, reader, _console = _delegate()
    delegate.set_status("budget", "3 turns left")
    delegate.set_status("budget", None)
    assert reader.prefixes[-1] == ""


def test_a_status_change_never_writes_the_draft() -> None:
    """The prefix is a callable the reader repaints in place, so a slot ticking
    over mid-line must not go near the half-typed text (§5)."""
    delegate, reader, _console = _delegate()
    delegate.set_status("budget", "3 turns left")
    assert reader.drafts == []


def test_a_panel_is_printed_with_its_body_and_its_numbered_actions() -> None:
    """A REPL keeps nothing live, so a panel is a frame in the scrollback and its
    actions say how they are pressed — by typing, since nothing here is clickable."""
    delegate, _reader, console = _delegate()
    delegate.panel("gate", _PANEL)
    text = console.export_text()
    assert "Release gate (gate)" in text
    assert "deploy" in text and "held" in text
    assert "1) Approve → /gate-approve now" in text
    assert "press one with /panel gate <number>" in text
    assert delegate.panels["gate"] is _PANEL


def test_a_cleared_panel_says_so_and_stops_being_pressable() -> None:
    """``None`` retires the panel; the scrollback keeps what it said, and
    ``/panel gate 1`` no longer names anything."""
    delegate, _reader, console = _delegate()
    delegate.panel("gate", _PANEL)
    delegate.panel("gate", None)
    assert "panel gate removed" in console.export_text()
    assert delegate.panels == {}


def test_the_panel_body_port_still_matches_the_tuis() -> None:
    """The port in ``repl.py`` and the original in ``extension_ui.py`` answer to one
    validator, and this is what says they still answer alike (§2's note on the
    move that is not yet possible). The Textual import is the test's, not the head's.
    """
    from tau_coding_agent import extension_ui

    bodies = [
        {"kind": "text", "text": "one line"},
        {"kind": "list", "items": ["a", "b"]},
        {"kind": "table", "columns": ["step", "state"], "rows": [["tests", "green"]]},
    ]
    for body in bodies:
        ported = Console(record=True, width=60, force_terminal=False)
        original = Console(record=True, width=60, force_terminal=False)
        ported.print(render_panel_body(body))
        original.print(extension_ui.render_panel_body(body))
        assert ported.export_text() == original.export_text()


def test_the_ported_body_renderer_refuses_a_kind_it_has_no_case_for() -> None:
    """A new core body kind is a rendering decision, not something to print as a repr."""
    with pytest.raises(KeyError):
        render_panel_body({"kind": "sparkline", "points": [1, 2]})


async def test_a_notify_from_an_extensions_module_body_reaches_the_scrollback(
    env: ReplEnv,
) -> None:
    """§3 step 11: the delegate is installed BEFORE extensions load, which is what
    makes the first notify an extension makes a printed line rather than stderr."""
    env.install(_arm(lambda backend: backend.delegate.notify("index rebuilt", "warning")))
    await env.run([])
    assert f"{GLYPH['warning']} index rebuilt" in env.text


async def test_pressing_a_panel_action_goes_through_the_one_door(env: ReplEnv) -> None:
    """``/panel gate 1`` submits the command the action names — it does NOT reach
    the extension's handler, because the door is what runs the input hooks (§7)."""

    def prepare(backend: FakeBackend) -> None:
        backend.delegate.panel("gate", _PANEL)
        backend.extension_commands = [("gate-approve", "approve the release")]

    env.install(_arm(prepare))
    await env.run(["/panel gate 1"])
    assert env.backend is not None
    assert [sub.text for sub in env.backend.commands] == ["/gate-approve now"]
    assert env.backend.commands[0].expand_commands is True
    assert env.backend.extension_runs == []


async def test_a_press_the_door_refuses_prints_the_reason_it_gave(env: ReplEnv) -> None:
    """A panel press is admitted like any other command, so whatever the door says
    about it — a hook's refusal here — is what the reader is shown."""

    def prepare(backend: FakeBackend) -> None:
        backend.delegate.panel("gate", _PANEL)
        backend.extension_commands = [("gate-approve", "approve the release")]
        backend.command_result = SubmissionResult(
            accepted=False, submission_id="s", rejection_reason="the deploy window is closed"
        )

    env.install(_arm(prepare))
    await env.run(["/panel gate 1"])
    assert "the deploy window is closed" in env.text


async def test_a_press_naming_an_unregistered_command_is_refused_not_sent(env: ReplEnv) -> None:
    """``validate_panel_actions`` never checks registration and the core resolves
    nothing, so an unchecked press would reach the MODEL as the literal line
    ``/gate-approve now`` — billed, persisted, and then misreported as an `input`
    hook. The ask path makes this check (``_open_ask``); so does this one."""
    env.install(_arm(lambda backend: backend.delegate.panel("gate", _PANEL)))
    await env.run(["/panel gate 1"])
    assert env.backend is not None
    assert env.backend.commands == []
    assert env.backend.submissions == []
    assert "names /gate-approve, which no loaded extension registered" in env.text


@pytest.mark.parametrize(
    "line,expected",
    [
        ("/panel gate", "takes a panel key and an action number"),
        ("/panel ghost 1", "no panel named 'ghost'"),
        ("/panel gate 7", "names none of them"),
    ],
)
async def test_a_press_that_names_nothing_is_refused(
    env: ReplEnv, line: str, expected: str
) -> None:
    """The head owns this word, so each way of mistyping it is stated rather than
    discarded (docs/SLASH-COMMANDS.md §4's defect, not repeated here)."""
    env.install(_arm(lambda backend: backend.delegate.panel("gate", _PANEL)))
    await env.run([line])
    assert expected in env.text
    assert env.backend is not None
    assert env.backend.commands == []


async def test_a_prose_line_under_a_lock_is_bounced_and_stays_typed(env: ReplEnv) -> None:
    """Read one step early (app.py:882): the core would refuse the same submission,
    but by then the line has left the prompt and cannot be given back."""
    env.install(lambda backend: setattr(backend, "pending_request", _request(ask=None)))
    await env.run(["deploy it"])
    assert env.backend is not None
    unwrapped = " ".join(env.text.split())
    assert " ".join(refusal_reason(_request(ask=None)).split()) in unwrapped
    assert env.submissions == []
    assert env.reader.drafts == ["deploy it"]


async def test_an_ask_is_answered_through_answer_request(env: ReplEnv) -> None:
    """The ask's fields, then its numbered actions, then the core's own door: the
    append is what releases the lock, so nothing here touches the log."""

    def prepare(backend: FakeBackend) -> None:
        backend.pending_request = _request()
        backend.extension_commands = [("gate-approve", "approve the release")]
        backend.answer_result = ExtensionCommandResult(handled=True, output="approved by alice")

    env.install(prepare)
    await env.run([], answers=["alice", "1"])
    assert env.backend is not None
    assert env.backend.answers == [("req-1", "Approve", {"approver": "alice"})]
    assert "Extension release_gate requires a response" in env.text
    assert "approved by alice" in env.text


async def test_a_dismissed_ask_answers_nothing_and_is_shown_again(env: ReplEnv) -> None:
    """Dismissing is not answering: no fabricated values, no release, and the
    request is drawn again at the next cursor move."""

    def prepare(backend: FakeBackend) -> None:
        backend.pending_request = _request()
        backend.extension_commands = [("gate-approve", "approve the release")]

    env.install(prepare)
    await env.run([], answers=[INTERRUPT])
    assert env.backend is not None
    assert env.backend.answers == []
    assert "dismissed, and shown again later" in env.text


async def test_an_answer_the_extension_cannot_run_warns_and_still_unlocks(env: ReplEnv) -> None:
    """``handled=False`` means the owner is not loaded; the response entry was
    appended anyway, because a lock nobody can answer is a session nobody can use."""

    def prepare(backend: FakeBackend) -> None:
        backend.pending_request = _request()
        backend.extension_commands = [("gate-approve", "approve the release")]
        backend.answer_result = ExtensionCommandResult(handled=False)

    env.install(prepare)
    await env.run([], answers=["alice", "1"])
    assert "is not loaded, so nothing ran" in env.text


async def test_an_ask_whose_actions_name_no_command_is_explained_not_asked(
    env: ReplEnv,
) -> None:
    """Auto-opened only when every action is registered (app.py:682): an ask whose
    buttons would do nothing is a question with no answer."""
    env.install(lambda backend: setattr(backend, "pending_request", _request()))
    await env.run([])
    assert env.backend is not None
    assert env.backend.answers == []
    assert "/gate-approve" in env.text
    assert "which no loaded extension registered" in env.text


async def test_the_shortcuts_are_listed_as_the_commands_to_type(env: ReplEnv) -> None:
    """A shortcut is an accelerator over a registered command and this head binds no
    chord, so ``/extensions`` says which command each one stands for (§7)."""

    def prepare(backend: FakeBackend) -> None:
        backend.command_result = performed_result(
            View(name="extensions", unavailable_because="no state yet")
        )
        backend.shortcuts = [("t", "todo-list", "--open", "list the open todos")]

    env.install(prepare)
    await env.run(["/extensions"])
    assert "ctrl+e t → /todo-list --open — list the open todos" in env.text
