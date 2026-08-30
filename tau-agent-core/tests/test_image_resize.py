"""Bounding an image before it reaches a model.

Reference: tau_agent_core/tools/image_resize.py.

The cap exists because an unbounded image is not merely expensive. Measured
2026-08-28 against llama.cpp with a vision model, a 2000x2000 PNG closed the
connection with no HTTP status and left the server process gone -- twice. These
tests hold the two properties that matters rests on: an image over the cap comes
back smaller, and an image under it comes back untouched.
"""

import base64
import io

import pytest
from tau_agent_core.tools.image_resize import (
    DEFAULT_MAX_IMAGE_DIMENSION,
    ImageSupportUnavailable,
    resize_image,
)
from tau_agent_core.tools.read import ReadTool

PIL = pytest.importorskip("PIL.Image", reason="the [images] extra")


def png(width: int, height: int, colour: str = "red") -> bytes:
    """A real PNG of the given size. Pillow, so the bytes are decodable."""
    buf = io.BytesIO()
    PIL.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def dimensions(data: bytes) -> tuple[int, int]:
    with PIL.open(io.BytesIO(data)) as img:
        return img.size


class TestResizeImage:
    def test_an_image_over_the_cap_is_scaled_down(self):
        out = resize_image(png(4000, 3000), "image/png", 2000)

        assert out.resized is True
        assert out.original_size == (4000, 3000)
        assert out.size == (2000, 1500)
        assert dimensions(out.data) == (2000, 1500), "the reported size is not the real one"

    def test_an_image_under_the_cap_is_returned_byte_identical(self):
        """Not merely 'unchanged in size' -- the SAME BYTES.

        Re-encoding an image that already fits costs quality and gains nothing.
        A lossless source that came back through a JPEG round trip would be a
        silent downgrade of every screenshot τ reads.
        """
        original = png(300, 200)
        out = resize_image(original, "image/png", 2000)

        assert out.resized is False
        assert out.data is original or out.data == original
        assert out.size == out.original_size == (300, 200)

    def test_an_image_is_never_scaled_up(self):
        out = resize_image(png(100, 100), "image/png", 2000)
        assert out.size == (100, 100)

    def test_the_aspect_ratio_survives_an_extreme_shape(self):
        """A 4000x3 strip scales by 0.5. ``round(1.5)`` is 2, not 1 -- Python
        rounds halves to even -- so the expectation here is 2."""
        out = resize_image(png(4000, 3), "image/png", 2000)

        assert out.size == (2000, 2)
        assert dimensions(out.data) == (2000, 2)

    def test_a_one_pixel_side_does_not_round_away_to_nothing(self):
        """4000x1 scales the short side to 0.5. Rounded, that is 0, and Pillow
        rejects a zero dimension outright, so the floor of 1 is what keeps a
        single-row image readable instead of a tool error."""
        out = resize_image(png(4000, 1), "image/png", 2000)

        assert out.size == (2000, 1)
        assert dimensions(out.data) == (2000, 1)

    @pytest.mark.parametrize(
        "fmt,mime",
        [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
    )
    def test_a_resized_image_keeps_its_format(self, fmt, mime):
        buf = io.BytesIO()
        PIL.new("RGB", (3000, 3000), "blue").save(buf, format=fmt)

        out = resize_image(buf.getvalue(), mime, 2000)

        assert out.mime_type == mime
        with PIL.open(io.BytesIO(out.data)) as img:
            assert img.format == fmt

    def test_a_gif_becomes_a_png_and_says_so(self):
        """GIF is the one format that changes. Pillow writes a single frame, so
        a resized animated GIF would come back silently de-animated; re-encoding
        to PNG at least reports the change through ``mime_type``, which ``read``
        puts in front of the model."""
        buf = io.BytesIO()
        PIL.new("P", (3000, 3000)).save(buf, format="GIF")

        out = resize_image(buf.getvalue(), "image/gif", 2000)

        assert out.mime_type == "image/png"
        with PIL.open(io.BytesIO(out.data)) as img:
            assert img.format == "PNG"

    def test_an_rgba_source_encoding_as_jpeg_is_converted_not_crashed(self):
        """JPEG has no alpha channel and Pillow raises rather than dropping it.
        The mode conversion is the fix; without it a transparent .jpg is an
        unreadable file rather than a resized one."""
        buf = io.BytesIO()
        PIL.new("RGBA", (3000, 3000), (1, 2, 3, 4)).save(buf, format="PNG")

        out = resize_image(buf.getvalue(), "image/jpeg", 2000)

        assert out.size == (2000, 2000)

    def test_an_unreadable_file_raises_rather_than_being_passed_through(self):
        """A file ``read`` decided was an image, and Pillow cannot decode, is a
        fault. Passing the bytes through would send a model something no
        endpoint can render, under a mime type claiming it can."""
        with pytest.raises(OSError):
            resize_image(b"\x89PNG\r\n\x1a\n" + b"\xde\xad\xbe\xef" * 64, "image/png", 2000)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_cap_raises(self, bad):
        with pytest.raises(ValueError, match="must be positive"):
            resize_image(png(10, 10), "image/png", bad)

    def test_the_default_matches_pi(self):
        assert DEFAULT_MAX_IMAGE_DIMENSION == 2000


class TestPillowIsOptional:
    def test_a_missing_pillow_names_the_extra_rather_than_sending_the_image(
        self, monkeypatch
    ):
        """The Fail-Early shape. Without Pillow the cap cannot be enforced, and
        the alternative -- send it unresized -- reports a bound that is not
        there. The error has to be actionable, so it names the extra."""
        import builtins

        real_import = builtins.__import__

        def no_pil(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pil)

        with pytest.raises(ImageSupportUnavailable) as excinfo:
            resize_image(png(10, 10), "image/png", 2000)
        assert "ffwf-tau-agent-core[images]" in str(excinfo.value)

    async def test_read_turns_it_into_a_tool_error_that_names_the_extra(
        self, tmp_path, monkeypatch
    ):
        """The message reaches the model as the tool's error, unprefixed --
        ``read`` handles this exception separately so "Error reading image:"
        does not bury the instruction."""
        (tmp_path / "shot.png").write_bytes(png(10, 10))

        def unavailable(*args, **kwargs):
            raise ImageSupportUnavailable("install 'ffwf-tau-agent-core[images]'")

        monkeypatch.setattr("tau_agent_core.tools.read.resize_image", unavailable)

        result = await ReadTool(cwd=str(tmp_path)).execute("tc1", {"path": "shot.png"})

        text = result["content"][0]["text"]
        assert text == "install 'ffwf-tau-agent-core[images]'"
        assert not [b for b in result["content"] if b["type"] == "image"]


class TestReadAppliesTheCap:
    async def test_a_large_screenshot_reaches_the_model_bounded(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png(3000, 3000))

        result = await ReadTool(cwd=str(tmp_path)).execute("tc1", {"path": "shot.png"})

        block = next(b for b in result["content"] if b["type"] == "image")
        assert dimensions(base64.b64decode(block["data"])) == (2000, 2000)

    async def test_the_text_block_reports_the_resize(self, tmp_path):
        """The model is reading a downscaled image and has to know. Fine text in
        a screenshot may be gone, and "I cannot read it" is a better answer than
        a guess."""
        (tmp_path / "shot.png").write_bytes(png(3000, 1500))

        result = await ReadTool(cwd=str(tmp_path)).execute("tc1", {"path": "shot.png"})

        text = result["content"][0]["text"]
        assert "2000x1000" in text
        assert "resized from 3000x1500" in text

    async def test_an_image_within_the_cap_is_not_announced_as_resized(self, tmp_path):
        (tmp_path / "small.png").write_bytes(png(64, 48))

        result = await ReadTool(cwd=str(tmp_path)).execute("tc1", {"path": "small.png"})

        text = result["content"][0]["text"]
        assert "64x48" in text
        assert "resized" not in text

    async def test_the_cap_can_be_lowered(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png(1000, 1000))

        tool = ReadTool(cwd=str(tmp_path), max_image_dimension=256)
        result = await tool.execute("tc1", {"path": "shot.png"})

        block = next(b for b in result["content"] if b["type"] == "image")
        assert dimensions(base64.b64decode(block["data"])) == (256, 256)

    async def test_none_is_an_explicit_opt_out_and_sends_the_file_whole(self, tmp_path):
        """``None`` is a choice an operator makes, and the only way to get the
        pre-cap behaviour. It is NOT what a missing Pillow does."""
        original = png(3000, 3000)
        (tmp_path / "shot.png").write_bytes(original)

        tool = ReadTool(cwd=str(tmp_path), max_image_dimension=None)
        result = await tool.execute("tc1", {"path": "shot.png"})

        block = next(b for b in result["content"] if b["type"] == "image")
        assert base64.b64decode(block["data"]) == original
