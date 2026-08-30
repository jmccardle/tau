"""``@file`` attachment scanning, rendering and completion.

Reference: docs/FILE-ATTACHMENTS.md.

Every test builds its own directory and passes it as ``cwd``, so nothing here
depends on where pytest was started from — the same isolation the TUI suite gets
from its ``tau_home`` fixture.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tau_agent_core.attachments import (
    DEFAULT_INLINE_LIMIT,
    Attachment,
    complete_attachment,
    elide_attachment_bodies,
    human_size,
    remove_attachment,
    render_attachments,
    scan_attachments,
)

PIL = pytest.importorskip("PIL.Image", reason="the [images] extra")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A directory with one file of each kind the scanner distinguishes."""
    (tmp_path / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "big.log").write_text("x" * (DEFAULT_INLINE_LIMIT + 1), encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(bytes(range(256)))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text("print()\n", encoding="utf-8")
    PIL.new("RGB", (10, 10), "red").save(tmp_path / "shot.png")
    return tmp_path


class TestWhatAWordInAPromptIs:
    """The scan decides which words name files, and what each one costs."""

    def test_a_text_file_within_the_limit_is_inlined(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @notes.txt", cwd=workspace)
        assert (found.kind, found.token, found.size) == ("inline", "notes.txt", 12)
        assert found.path == workspace / "notes.txt"

    def test_a_large_text_file_becomes_a_reference(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @big.log", cwd=workspace)
        assert found.kind == "reference"
        assert "over the" in found.note

    def test_a_binary_file_becomes_a_reference(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @blob.bin", cwd=workspace)
        assert (found.kind, found.note) == ("reference", "not UTF-8 text")

    def test_an_image_is_its_own_kind(self, workspace: Path) -> None:
        (found,) = scan_attachments("look at @shot.png", cwd=workspace)
        assert (found.kind, found.mime_type) == ("image", "image/png")

    def test_the_limit_is_a_parameter(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @notes.txt", cwd=workspace, inline_limit=4)
        assert found.kind == "reference"

    def test_a_word_that_names_nothing_is_prose(self, workspace: Path) -> None:
        (found,) = scan_attachments("ask @alice about it", cwd=workspace)
        assert (found.kind, found.note) == ("unresolved", "no such file")

    def test_a_directory_is_prose(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @sub", cwd=workspace)
        assert (found.kind, found.note) == ("unresolved", "is a directory")

    def test_an_email_address_is_not_a_reference(self, workspace: Path) -> None:
        """The ``@`` must open a word. ``bob@example.com`` does not."""
        assert scan_attachments("mail bob@example.com", cwd=workspace) == ()

    def test_trailing_punctuation_is_dropped_only_when_it_has_to_be(
        self, workspace: Path
    ) -> None:
        (found,) = scan_attachments("read @notes.txt, then stop", cwd=workspace)
        assert (found.kind, found.token) == ("inline", "notes.txt")

    def test_a_file_named_with_punctuation_still_wins(self, tmp_path: Path) -> None:
        """The literal spelling is tried first, so a real ``odd,`` is not trimmed."""
        (tmp_path / "odd,").write_text("x\n", encoding="utf-8")
        (found,) = scan_attachments("read @odd, please", cwd=tmp_path)
        assert (found.kind, found.token) == ("inline", "odd,")

    def test_the_span_covers_exactly_the_reference(self, workspace: Path) -> None:
        text = "read @notes.txt now"
        (found,) = scan_attachments(text, cwd=workspace)
        assert text[found.start : found.end] == "@notes.txt"

    def test_several_references_keep_their_order(self, workspace: Path) -> None:
        found = scan_attachments("@notes.txt then @shot.png", cwd=workspace)
        assert [a.token for a in found] == ["notes.txt", "shot.png"]


class TestTheBlocksThatReachTheModel:
    """What ``render_attachments`` puts in front of the human's own words."""

    def test_an_inlined_file_carries_its_content(self, workspace: Path) -> None:
        rendered = render_attachments(scan_attachments("@notes.txt", cwd=workspace))
        assert rendered.prefix == (
            '<attachment filename="notes.txt">\nhello\nworld\n</attachment>\n'
        )
        assert rendered.images == ()

    def test_a_file_without_a_trailing_newline_gets_one(self, tmp_path: Path) -> None:
        """So the closing tag is on its own line, whatever the file ends with."""
        (tmp_path / "a.txt").write_text("no newline", encoding="utf-8")
        rendered = render_attachments(scan_attachments("@a.txt", cwd=tmp_path))
        assert rendered.prefix.endswith("no newline\n</attachment>\n")

    def test_a_reference_names_the_path_and_the_size(self, workspace: Path) -> None:
        rendered = render_attachments(scan_attachments("@big.log", cwd=workspace))
        assert rendered.prefix.startswith('<reference filename="big.log" ')
        assert str(workspace / "big.log") in rendered.prefix
        assert 'reason="' in rendered.prefix
        assert rendered.prefix.endswith("/>\n")

    def test_an_image_becomes_a_content_block_and_an_empty_marker(
        self, workspace: Path
    ) -> None:
        rendered = render_attachments(scan_attachments("@shot.png", cwd=workspace))
        assert rendered.prefix == '<attachment filename="shot.png" type="image/png" />\n'
        (image,) = rendered.images
        assert image["type"] == "image"
        assert image["mime_type"] == "image/png"
        assert base64.b64decode(image["data"])[:4] == b"\x89PNG"

    def test_an_oversized_image_is_bounded_and_says_so(self, tmp_path: Path) -> None:
        PIL.new("RGB", (300, 120), "blue").save(tmp_path / "wide.png")
        rendered = render_attachments(
            scan_attachments("@wide.png", cwd=tmp_path), max_image_dimension=100
        )
        assert 'resized="300x120 to 100x40"' in rendered.prefix

    def test_prose_references_produce_nothing(self, workspace: Path) -> None:
        rendered = render_attachments(scan_attachments("ask @alice", cwd=workspace))
        assert (rendered.prefix, rendered.images, rendered.failures) == ("", (), ())

    def test_a_file_deleted_after_the_scan_is_reported_not_dropped(
        self, workspace: Path
    ) -> None:
        """Fail-Early: the model is told, and so is the frontend."""
        found = scan_attachments("@notes.txt", cwd=workspace)
        (workspace / "notes.txt").unlink()
        rendered = render_attachments(found)
        assert 'error="' in rendered.prefix
        assert rendered.failures and "notes.txt" in rendered.failures[0]

    def test_a_quote_in_a_filename_cannot_break_the_header(self, tmp_path: Path) -> None:
        (tmp_path / 'od"d.txt').write_text("x\n", encoding="utf-8")
        rendered = render_attachments(scan_attachments('@od"d.txt', cwd=tmp_path))
        assert rendered.prefix.startswith('<attachment filename="od&quot;d.txt">')

    def test_the_body_is_never_escaped(self, tmp_path: Path) -> None:
        """A code file full of ``<`` and ``&`` must reach the model as itself."""
        (tmp_path / "code.py").write_text("if a < b & c:\n", encoding="utf-8")
        rendered = render_attachments(scan_attachments("@code.py", cwd=tmp_path))
        assert "if a < b & c:" in rendered.prefix


class TestRemovingAReference:
    """``remove_attachment`` is what the frontend's ✕ actually does."""

    def test_it_deletes_the_word_and_collapses_the_gap(self, workspace: Path) -> None:
        text = "read @notes.txt and stop"
        (found,) = [a for a in scan_attachments(text, cwd=workspace) if a.kind == "inline"]
        assert remove_attachment(text, found) == "read and stop"

    def test_a_trailing_reference_leaves_no_trailing_space(self, workspace: Path) -> None:
        text = "read @notes.txt"
        (found,) = scan_attachments(text, cwd=workspace)
        assert remove_attachment(text, found) == "read"

    def test_a_leading_reference_leaves_no_leading_space(self, workspace: Path) -> None:
        text = "@notes.txt please"
        (found,) = scan_attachments(text, cwd=workspace)
        assert remove_attachment(text, found) == "please"

    def test_a_stale_span_raises_rather_than_cutting(self, workspace: Path) -> None:
        (found,) = scan_attachments("read @notes.txt", cwd=workspace)
        with pytest.raises(ValueError, match="edited after the scan"):
            remove_attachment("something else entirely", found)


class TestCompletingAHalfTypedReference:
    """The Tab vocabulary. ``None`` means "the cursor is not in a ``@…``"."""

    def test_no_reference_under_the_cursor(self, workspace: Path) -> None:
        assert complete_attachment("plain prose", 5, cwd=workspace) is None

    def test_a_prefix_matches_by_first_characters(self, workspace: Path) -> None:
        completions = complete_attachment("read @no", 8, cwd=workspace)
        assert completions is not None
        assert [m.name for m in completions.matches] == ["notes.txt"]

    def test_a_bare_at_lists_the_directory(self, workspace: Path) -> None:
        completions = complete_attachment("read @", 6, cwd=workspace)
        assert completions is not None
        assert "notes.txt" in [m.name for m in completions.matches]

    def test_a_directory_candidate_ends_in_a_slash(self, workspace: Path) -> None:
        completions = complete_attachment("read @su", 8, cwd=workspace)
        assert completions is not None
        assert [(m.name, m.is_dir) for m in completions.matches] == [("sub/", True)]

    def test_completion_continues_into_a_directory(self, workspace: Path) -> None:
        completions = complete_attachment("read @sub/", 10, cwd=workspace)
        assert completions is not None
        assert [m.name for m in completions.matches] == ["sub/inner.py"]

    def test_hidden_entries_need_a_dot_in_the_prefix(self, tmp_path: Path) -> None:
        (tmp_path / ".secret").write_text("x", encoding="utf-8")
        (tmp_path / "plain").write_text("x", encoding="utf-8")
        visible = complete_attachment("@", 1, cwd=tmp_path)
        assert visible is not None and [m.name for m in visible.matches] == ["plain"]
        asked = complete_attachment("@.", 2, cwd=tmp_path)
        assert asked is not None and [m.name for m in asked.matches] == [".secret"]

    def test_no_match_is_information_not_absence(self, workspace: Path) -> None:
        """Empty matches is the "names no file" warning, and is NOT ``None``."""
        completions = complete_attachment("read @zzz", 9, cwd=workspace)
        assert completions is not None and completions.matches == ()

    def test_the_span_replaces_the_whole_token(self, workspace: Path) -> None:
        text = "read @notes.txt now"
        completions = complete_attachment(text, 8, cwd=workspace)
        assert completions is not None
        assert text[completions.start : completions.end] == "@notes.txt"

    def test_the_cursor_at_the_end_of_the_token_still_counts(self, workspace: Path) -> None:
        completions = complete_attachment("read @no", 8, cwd=workspace)
        assert completions is not None and completions.token == "no"


class TestTheDisplayFold:
    """``elide_attachment_bodies`` is what the transcript shows."""

    def test_a_body_becomes_a_visible_summary(self) -> None:
        text = '<attachment filename="a.txt">\nline\nline\n</attachment>\n'
        folded = elide_attachment_bodies(text)
        assert '<attachment filename="a.txt">' in folded
        assert "2 lines" in folded and "not shown" in folded
        assert "line\nline" not in folded

    def test_an_image_marker_is_left_alone(self) -> None:
        text = '<attachment filename="a.png" type="image/png" />\n'
        assert elide_attachment_bodies(text) == text

    def test_an_image_marker_does_not_swallow_the_next_block(self) -> None:
        text = (
            '<attachment filename="a.png" type="image/png" />\n'
            '<attachment filename="b.txt">\nbody\n</attachment>\n'
        )
        folded = elide_attachment_bodies(text)
        assert folded.startswith('<attachment filename="a.png" type="image/png" />\n')
        assert "body" not in folded

    def test_two_blocks_stay_two_blocks(self) -> None:
        text = (
            '<attachment filename="a.txt">\naaa\n</attachment>\n'
            '<attachment filename="b.txt">\nbbb\n</attachment>\n'
        )
        folded = elide_attachment_bodies(text)
        assert folded.count("<attachment") == 2
        assert "aaa" not in folded and "bbb" not in folded

    def test_ordinary_prose_is_untouched(self) -> None:
        assert elide_attachment_bodies("just words") == "just words"


class TestSizes:
    def test_bytes_kilobytes_megabytes(self) -> None:
        assert human_size(12) == "12 bytes"
        assert human_size(10 * 1024) == "10.0 KB"
        assert human_size(3 * 1024 * 1024) == "3.0 MB"


def test_an_attachment_is_frozen() -> None:
    """It rides into a widget and back out as a removal instruction."""
    attachment = Attachment(token="a.txt", start=0, end=6, kind="inline")
    with pytest.raises(Exception):
        attachment.token = "b.txt"  # type: ignore[misc]
