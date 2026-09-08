"""docs/EXTENSION-LOCKS.md §9 — the TUI half of an extension lock.

Driven through the real ``TauApp`` (``App.run_test()`` / Pilot), matching
``test_extension_panel``. Under test: the typed line bouncing off a lock instead
of clearing the editor, the transcript row that draws the request at the cursor,
the click that re-opens a dismissed ask, and the modal answering through
``answer_request``.

The core half — the refusal itself, the four states, the escapes — is
``tau-agent-core/tests/test_extension_locks.py``.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import ExtensionCommandResult
from tau_agent_core.extension_locks import REQUEST_ENTRY_TYPE, build_request_data, read_request
from tau_agent_core.extension_types import validate_ask_spec
from tau_coding_agent import extension_ui, transcript
from tau_coding_agent.chat_widgets import ChatInput

_ASK = validate_ask_spec(
    {
        "title": "Release gate",
        "text": "Say who approved it.",
        "fields": [{"name": "approver", "kind": "text", "default": "nobody"}],
        "actions": [{"label": "Approve", "command": "gate-approve"}],
    }
)


def _request(*, lock: bool = True, ask: dict | None = _ASK, release: str | None = "gate-clear"):
    return read_request(
        {
            "type": "customEntry",
            "customType": REQUEST_ENTRY_TYPE,
            "id": "req-1",
            "data": build_request_data(
                "/x/release_gate.py",
                "A deploy-shaped command ran.",
                lock=lock,
                ask=ask,
                release=release,
            ),
        }
    )


class _FakeBackend:
    """The two seams a head uses: what is pending, and how to answer it."""

    def __init__(self, request=None) -> None:
        self.pending_request = request
        self.answers: list[tuple[str, str, dict]] = []
        self.commands: list[tuple[str, str]] = []

    async def answer_request(self, request_id, action, values):
        self.answers.append((request_id, action, values))
        self.pending_request = None
        return ExtensionCommandResult(handled=True, output=f"approved by {values['approver']}")

    async def run_extension_command(self, name, args=""):
        self.commands.append((name, args))
        return ExtensionCommandResult(handled=True)


@pytest.fixture
def app(make_app):
    return make_app()


async def _with_request(app, request):
    app.current_backend = _FakeBackend(request)
    app.refresh_extension_request()


# ── the transcript row (§9) ──────────────────────────────────────────────────


async def test_no_row_without_a_request(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _FakeBackend(None)
        app.refresh_extension_request()
        await pilot.pause()
        assert not app.query(extension_ui.ExtensionRequestBox)


async def test_the_row_shows_the_label_and_the_sentence(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request())
        await pilot.pause()
        row = app.query_one(extension_ui.ExtensionRequestBox)
        assert "release_gate requires a response" in row.body_text
        assert "A deploy-shaped command ran." in row.body_text
        assert row.has_class("ext-request-locked")


async def test_an_unlocked_ask_reads_as_a_request(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request(lock=False))
        await pilot.pause()
        row = app.query_one(extension_ui.ExtensionRequestBox)
        assert "requests a response" in row.body_text
        assert row.has_class("ext-request-open")


async def test_a_second_request_replaces_the_first_row(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request())
        await pilot.pause()
        await _with_request(app, _request(ask=None))
        await pilot.pause()
        rows = list(app.query(extension_ui.ExtensionRequestBox))
        assert len(rows) == 1
        assert "requires intervention" in rows[0].body_text


async def test_the_row_is_cleared_when_the_request_is(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request())
        await pilot.pause()
        app.current_backend.pending_request = None
        app.refresh_extension_request()
        await pilot.pause()
        assert not app.query(extension_ui.ExtensionRequestBox)


# ── the bounce (§9) ──────────────────────────────────────────────────────────


async def test_a_locked_prompt_stays_in_the_editor(app) -> None:
    """The editor clears on ADMISSION, so a refused line is still there to edit."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request())
        editor = app.query_one("#chat-input", ChatInput)
        editor.text = "carry on then"

        assert app._bounce_if_locked() is True
        await pilot.pause()
        assert editor.text == "carry on then"


async def test_an_unlocked_request_does_not_bounce(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request(lock=False))
        assert app._bounce_if_locked() is False


async def test_nothing_pending_does_not_bounce(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _FakeBackend(None)
        assert app._bounce_if_locked() is False


# ── the modal (§8, §9) ───────────────────────────────────────────────────────


async def test_the_ask_opens_by_itself_when_its_command_is_registered(app, monkeypatch) -> None:
    monkeypatch.setattr(app, "_extension_command_names", lambda: ["gate-approve"])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _FakeBackend(_request())
        app.refresh_extension_request(open_ask=True)
        await pilot.pause()
        assert isinstance(app.screen, extension_ui.ExtensionAskScreen)


async def test_the_ask_does_not_open_when_its_command_is_unknown(app, monkeypatch) -> None:
    """Render always, auto-open only when the buttons would do something (§9)."""
    monkeypatch.setattr(app, "_extension_command_names", lambda: [])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _FakeBackend(_request())
        app.refresh_extension_request(open_ask=True)
        await pilot.pause()
        assert not isinstance(app.screen, extension_ui.ExtensionAskScreen)
        assert app.query(extension_ui.ExtensionRequestBox)


async def test_pressing_an_action_answers_through_the_core(app, monkeypatch) -> None:
    monkeypatch.setattr(app, "_extension_command_names", lambda: ["gate-approve"])
    async with app.run_test() as pilot:
        await pilot.pause()
        backend = _FakeBackend(_request())
        app.current_backend = backend
        app.refresh_extension_request(open_ask=True)
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, extension_ui.ExtensionAskScreen)
        screen.query_one("#ext-ask-action-0").press()
        await pilot.pause()
        await pilot.pause()

        assert backend.answers == [("req-1", "Approve", {"approver": "nobody"})]
        assert not app.query(extension_ui.ExtensionRequestBox)


async def test_dismissing_the_ask_answers_nothing(app, monkeypatch) -> None:
    monkeypatch.setattr(app, "_extension_command_names", lambda: ["gate-approve"])
    async with app.run_test() as pilot:
        await pilot.pause()
        backend = _FakeBackend(_request())
        app.current_backend = backend
        app.refresh_extension_request(open_ask=True)
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert backend.answers == []
        assert backend.pending_request is not None
        assert app.query(extension_ui.ExtensionRequestBox)


async def test_clicking_the_row_re_opens_the_ask(app) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request())
        await pilot.pause()
        row = app.query_one(extension_ui.ExtensionRequestBox)

        app.post_message(extension_ui.ExtensionRequestBox.Reopen(row._request))
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, extension_ui.ExtensionAskScreen)


async def test_a_lock_with_no_ask_has_nothing_to_click(app) -> None:
    """§3, row 2: the extension's own slash command is the way out, not a form."""
    async with app.run_test() as pilot:
        await pilot.pause()
        await _with_request(app, _request(ask=None))
        await pilot.pause()
        row = app.query_one(extension_ui.ExtensionRequestBox)
        assert "gate-clear clears it" in row.body_text
        assert row._request.ask is None


async def test_the_ask_screen_refuses_a_request_with_no_ask(app) -> None:
    with pytest.raises(ValueError, match="no ask to render"):
        extension_ui.ExtensionAskScreen(_request(ask=None))


async def test_the_row_survives_a_transcript_reload(app) -> None:
    """§9: every reload site redraws the row, because a reload means the cursor moved."""
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_backend = _FakeBackend(_request())
        app.messages = [{"role": "user", "content": "hello"}]
        await app._reload_transcript()
        await pilot.pause()
        display = app.query_one(transcript.ChatDisplay)
        assert list(display.query(extension_ui.ExtensionRequestBox))
