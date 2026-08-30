"""``@file`` attachments in the chat editor: the bar, the Tab cycle, the submission.

Reference: docs/FILE-ATTACHMENTS.md.

Every test chdirs into its own ``tmp_path``, because the app resolves references
against the process working directory — the same directory the agent's own tools
work in, which is the whole point of a relative ``@src/app.py``.

Keys are pressed through a mounted app wherever the question is which layer gets
the keystroke, exactly as ``test_slash_command_popup`` does it: Tab now has two
vocabularies to choose between and the choice is made in a key handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from tau_agent_core.submission import SubmissionResult
from tau_coding_agent.app import (
    AttachmentBar,
    AttachmentRow,
    ChatDisplay,
    ChatInput,
    CommandPopup,
)

PIL = pytest.importorskip("PIL.Image", reason="the [images] extra")


class _Submit:
    """Duck-typed Input.Submitted — ``on_input_submitted`` only reads ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


class _RecordingBackend:
    """Records every submission and answers it with nothing.

    ``submit_turn`` returns at once, so a test that submits does not have to
    settle a turn to read what was submitted.
    """

    def __init__(self) -> None:
        self.submissions: list[Any] = []
        self._log: Any = None

    def bind_session_log(self, session_log) -> None:
        self._log = session_log

    def abort(self) -> None:  # pragma: no cover - no test aborts
        pass

    async def submit_turn(self, submission, context=None) -> SubmissionResult:
        self.submissions.append(submission)
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)

    async def submit_command(self, submission) -> SubmissionResult:
        """Record a command submission and refuse it.

        Refusing keeps the app on its one reported path (a warning toast) instead
        of performing an outcome no test here is about; the submission is recorded
        either way, which is what the attachment tests read.
        """
        self.submissions.append(submission)
        return SubmissionResult(
            accepted=False,
            submission_id=submission.submission_id,
            rejection_reason="no command runs in this test",
        )


class _BlockingBackend(_RecordingBackend):
    """Holds an ordinary turn open so a steering message has something to steer."""

    def __init__(self) -> None:
        super().__init__()
        self._released = asyncio.Event()

    def abort(self) -> None:
        self._released.set()

    def release(self) -> None:
        self._released.set()

    async def submit_turn(self, submission, context=None) -> SubmissionResult:
        self.submissions.append(submission)
        if submission.multitask_strategy == "steer":
            return SubmissionResult(accepted=True, submission_id=submission.submission_id)
        await self._released.wait()
        self._released.clear()
        return SubmissionResult(accepted=True, submission_id=submission.submission_id)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory holding one file of each kind, and chdir into it."""
    (tmp_path / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("second\n", encoding="utf-8")
    (tmp_path / "big.log").write_text("x" * 20_000, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text("print()\n", encoding="utf-8")
    PIL.new("RGB", (10, 10), "red").save(tmp_path / "shot.png")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def app(make_app, workspace: Path):
    """A sandboxed app whose backend records submissions."""
    return make_app(create_backend=lambda cfg: _RecordingBackend())


async def _type(pilot, editor: ChatInput, text: str) -> None:
    """Put ``text`` in the editor with the cursor at the end."""
    editor.text = text
    editor.move_cursor(editor.document.end)
    await pilot.pause()


# ---------------------------------------------------------------------------
# §4 — the bar
# ---------------------------------------------------------------------------


class TestTheBarShowsWhatTheDraftWillAttach:
    async def test_prose_shows_nothing(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "just a question")
            bar = app.query_one(AttachmentBar)
            assert bar.display is False
            assert bar.attachments == ()

    async def test_a_reference_becomes_a_row(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @notes.txt please")
            bar = app.query_one(AttachmentBar)
            assert bar.display is True
            assert [a.token for a in bar.attachments] == ["notes.txt"]
            (row,) = app.query(AttachmentRow).results()
            assert "notes.txt" in row.text

    async def test_a_word_that_names_no_file_gets_no_row(self, app):
        """It is prose on its way to the model. The popup says so, not the bar."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "ask @alice about it")
            assert app.query_one(AttachmentBar).attachments == ()

    async def test_the_row_says_which_files_are_sent_by_path_only(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @big.log")
            (row,) = app.query(AttachmentRow).results()
            assert "path only" in row.text

    async def test_deleting_the_word_empties_the_bar(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @notes.txt")
            assert app.query_one(AttachmentBar).display is True
            await _type(pilot, editor, "read")
            bar = app.query_one(AttachmentBar)
            assert bar.display is False
            assert list(app.query(AttachmentRow).results()) == []


class TestRemovingAnAttachment:
    async def test_clicking_a_row_deletes_the_word_from_the_editor(self, app):
        """The ✕ has no separate state to clear: the word IS the attachment."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @notes.txt and stop")
            (row,) = app.query(AttachmentRow).results()

            app.post_message(AttachmentBar.Remove(row.attachment))
            await pilot.pause()

            assert editor.text == "read and stop"
            assert app.query_one(AttachmentBar).display is False

    async def test_one_of_two_can_be_removed(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "@notes.txt and @other.txt")
            rows = app.query(AttachmentRow).results()
            assert len(list(rows)) == 2

            first = app.query_one(AttachmentBar).attachments[0]
            app.post_message(AttachmentBar.Remove(first))
            await pilot.pause()

            assert editor.text == "and @other.txt"
            assert [a.token for a in app.query_one(AttachmentBar).attachments] == ["other.txt"]

    async def test_a_stale_span_warns_instead_of_cutting(self, app):
        """The human typed between the redraw and the click."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @notes.txt")
            stale = app.query_one(AttachmentBar).attachments[0]
            await _type(pilot, editor, "completely different")

            app.post_message(AttachmentBar.Remove(stale))
            await pilot.pause()

            assert editor.text == "completely different"


# ---------------------------------------------------------------------------
# §3 — the Tab vocabulary
# ---------------------------------------------------------------------------


class TestTabCompletesAPath:
    async def test_it_completes_a_reference_mid_sentence(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "read @no")
            await pilot.press("tab")
            assert editor.text == "read @notes.txt "

    async def test_the_cursor_lands_after_what_was_inserted(self, app):
        """A reference can sit anywhere in the line, so the cursor must not jump
        to the end of the document the way a completed command's does."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            editor.text = "read @no and stop"
            editor.move_cursor(editor._location_of_offset(8))
            await pilot.pause()

            await pilot.press("tab")
            assert editor.text == "read @notes.txt  and stop"
            assert editor.cursor_offset == len("read @notes.txt ")

    async def test_a_directory_is_inserted_without_a_space(self, app):
        """So the next Tab continues into it."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "read @su")
            await pilot.press("tab")
            assert editor.text == "read @sub/"
            await pilot.press("tab")
            assert editor.text == "read @sub/inner.py "

    async def test_repeated_tab_cycles_the_candidates(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "read @")
            await pilot.press("tab")
            first = editor.text
            await pilot.press("tab")
            second = editor.text
            assert first != second
            assert first.startswith("read @") and second.startswith("read @")

    async def test_tab_outside_a_reference_still_completes_a_command(self, app):
        """The two vocabularies are asked in the order the cursor decides."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "/comp")
            await pilot.press("tab")
            assert editor.text == "/compact "

    async def test_a_reference_wins_over_the_command_on_the_same_line(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "/fork @no")
            await pilot.press("tab")
            assert editor.text == "/fork @notes.txt "

    async def test_tab_with_nothing_to_insert_is_left_alone(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            editor.focus()
            await _type(pilot, editor, "read @zzz")
            await pilot.press("tab")
            assert editor.text == "read @zzz"


class TestThePopupUnderTheEditor:
    async def test_it_lists_paths_while_the_cursor_is_in_a_reference(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @no")
            popup = app.query_one("#command-popup", CommandPopup)
            assert popup.display is True
            assert "@notes.txt" in popup.text

    async def test_it_warns_that_a_reference_names_no_file(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "ask @zzz")
            popup = app.query_one("#command-popup", CommandPopup)
            assert popup.display is True
            assert "matches no file" in popup.text

    async def test_moving_the_cursor_out_of_the_reference_hides_it(self, app):
        """``SelectionChanged``, not ``Changed``: no character changed."""
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "read @no ok")
            editor.move_cursor(editor._location_of_offset(0))
            await pilot.pause()
            assert app.query_one("#command-popup", CommandPopup).display is False

    async def test_a_slash_line_still_gets_the_command_list(self, app):
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await _type(pilot, editor, "/comp")
            popup = app.query_one("#command-popup", CommandPopup)
            assert "/compact" in popup.text


# ---------------------------------------------------------------------------
# §2 / §5 — what reaches the model
# ---------------------------------------------------------------------------


class TestTheSubmission:
    async def test_an_inlined_file_is_prefixed_to_the_prompt(self, app):
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("summarise @notes.txt"))
            await pilot.pause()
            (submission,) = app.current_backend.submissions
            assert submission.text == (
                '<attachment filename="notes.txt">\nhello\nworld\n</attachment>\n'
                "summarise @notes.txt"
            )
            assert submission.images is None

    async def test_an_image_rides_as_a_content_block(self, app):
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("what is @shot.png"))
            await pilot.pause()
            (submission,) = app.current_backend.submissions
            assert submission.images is not None
            (image,) = submission.images
            assert image["mime_type"] == "image/png"
            assert '<attachment filename="shot.png" type="image/png" />' in submission.text

    async def test_a_large_file_sends_its_path_not_its_content(self, app):
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("check @big.log"))
            await pilot.pause()
            (submission,) = app.current_backend.submissions
            assert submission.text.startswith('<reference filename="big.log" ')
            assert "xxxx" not in submission.text

    async def test_prose_is_submitted_unchanged(self, app):
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("ask @alice about it"))
            await pilot.pause()
            (submission,) = app.current_backend.submissions
            assert submission.text == "ask @alice about it"
            assert submission.images is None

    async def test_the_inline_limit_is_configurable(self, make_app, workspace):
        app = make_app(
            create_backend=lambda cfg: _RecordingBackend(),
            config={"attachment_inline_limit": 4},
        )
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("read @notes.txt"))
            await pilot.pause()
            (submission,) = app.current_backend.submissions
            assert submission.text.startswith("<reference ")

    async def test_a_command_attaches_nothing(self, app):
        """``/fork`` is not a prompt; no file is read for it."""
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("/compact @notes.txt"))
            await pilot.pause()
            assert all(
                "<attachment" not in s.text for s in app.current_backend.submissions
            )

    async def test_the_transcript_folds_the_body_it_sent(self, app):
        """The bubble is the record of what was sent, minus 10 KB of file."""
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("summarise @notes.txt"))
            await pilot.pause()
            await app._on_render_event(
                {
                    "kind": "lane_start",
                    "lane": "L",
                    "source": "interactive",
                    "submitter": "human",
                    "text": '<attachment filename="notes.txt">\nhello\nworld\n</attachment>\nsummarise @notes.txt',
                }
            )
            await pilot.pause()
            bodies = [
                str(box._content) for box in app.query_one(ChatDisplay).query("MessageBox")
            ]
            assert any("not shown" in body for body in bodies)
            assert not any("hello\nworld" in body for body in bodies)


class TestSteeringAttachesToo:
    async def test_a_file_named_mid_turn_is_read_at_delivery(self, make_app, workspace):
        backend = _BlockingBackend()
        app = make_app(
            create_backend=lambda cfg: backend, config={"steering_strategy": "enqueue"}
        )
        async with app.run_test() as pilot:
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("also read @notes.txt"))
            await pilot.pause()
            assert app._pending_steer == ["also read @notes.txt"]

            backend.release()
            for _ in range(200):
                if len(backend.submissions) == 2:
                    break
                await pilot.pause()

            assert backend.submissions[1].text.startswith('<attachment filename="notes.txt">')

            backend.release()
            for _ in range(200):
                if not app.is_generating:
                    break
                await pilot.pause()

    async def test_reclaiming_a_pending_line_brings_the_attachment_back(
        self, make_app, workspace
    ):
        """The buffer holds what the human typed, so the bar can be rebuilt from
        it. Nothing has to remember the file separately."""
        backend = _BlockingBackend()
        app = make_app(create_backend=lambda cfg: backend)
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", ChatInput)
            await app.on_input_submitted(_Submit("go"))
            await pilot.pause()
            await app.on_input_submitted(_Submit("also read @notes.txt"))
            await pilot.pause()
            assert app.query_one(AttachmentBar).attachments == ()

            app._return_pending_to_the_editor()
            await pilot.pause()

            assert editor.text == "also read @notes.txt"
            assert [a.token for a in app.query_one(AttachmentBar).attachments] == ["notes.txt"]
            assert app._pending_steer == []

            backend.release()
            for _ in range(200):
                if not app.is_generating:
                    break
                await pilot.pause()
