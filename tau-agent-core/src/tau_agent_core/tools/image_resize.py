"""τ-agent-core tools: bound an image before it goes to a model.

``read`` used to send an image file whole, at whatever resolution it was on
disk. MEASURED 2026-08-28 against llama.cpp (Qwen3.8-27B, vision on): a
2000x2000 PNG — 31 KB on disk, 4307 prompt tokens — closed the connection with
no HTTP status and left the server process gone. Twice. The same image
succeeded once the server restarted with a smaller model context, so the limit
was the vision encoder's spare VRAM: a number the client cannot read and the
server does not advertise.

That is why the cap here is a BUDGET THE OPERATOR PICKS, not a property
discovered from the model. 2000x2000 is pi's default (``image-resize-core.ts``,
``maxWidth``/``maxHeight``) and τ's.

Pillow is an optional dependency. It is imported inside the function, and a
missing one raises :class:`ImageSupportUnavailable` naming the extra to install
— the same shape as ``--store jmfts`` without ``ffwf-tau-jmfts``. Reading an
image does NOT silently fall back to sending it unresized: a cap that cannot be
enforced must not be reported as enforced.
"""

from __future__ import annotations

import io
from typing import NamedTuple

#: pi's default, and τ's. See the module docstring for why this is a chosen
#: budget rather than a measured model property.
DEFAULT_MAX_IMAGE_DIMENSION = 2000

#: Output format per input mime type. GIF is absent on purpose: Pillow writes a
#: single frame, so an animated GIF would come back silently de-animated. It is
#: re-encoded to PNG instead, and :func:`resize_image` reports the change.
_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}


class ImageSupportUnavailable(RuntimeError):
    """Pillow is not installed, so an image cannot be bounded.

    Raised by :func:`resize_image` rather than returning the image untouched.
    The caller decides how to report it; ``read`` turns it into a tool error
    that names the extra.
    """


class ResizedImage(NamedTuple):
    """The result of bounding one image.

    Attributes:
        data: The image bytes to send. Byte-identical to the input when
            ``resized`` is False — an image already inside the cap is never
            re-encoded, so a lossless source stays lossless.
        mime_type: The mime type of ``data``. Differs from the input only when
            the format had to change (GIF becomes PNG).
        original_size: ``(width, height)`` as found on disk.
        size: ``(width, height)`` of ``data``.
        resized: Whether anything changed.
    """

    data: bytes
    mime_type: str
    original_size: tuple[int, int]
    size: tuple[int, int]
    resized: bool


def resize_image(
    data: bytes,
    mime_type: str,
    max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
) -> ResizedImage:
    """Scale an image down so neither side exceeds ``max_dimension``.

    The aspect ratio is preserved and the image is never scaled UP: a 300x200
    source with a 2000 cap comes back untouched, and byte-identical, because
    re-encoding it would cost quality for nothing.

    This is CPU-bound and decodes the whole image. Call it in a worker thread —
    ``read`` uses :func:`asyncio.to_thread`.

    Args:
        data: The raw image file bytes.
        mime_type: The image's mime type, as ``read`` derives it from the file
            extension. An unrecognised type is re-encoded to PNG.
        max_dimension: The largest allowed width or height, in pixels. Must be
            positive.

    Returns:
        A :class:`ResizedImage`. ``resized`` is False when the image already fit.

    Raises:
        ImageSupportUnavailable: Pillow is not installed.
        ValueError: ``max_dimension`` is not positive.
        OSError: Pillow could not decode ``data`` as an image. Raised rather
            than caught: a file this tool decided was an image, and cannot
            read, is a fault worth surfacing.
    """
    if max_dimension <= 0:
        raise ValueError(f"max_dimension must be positive, got {max_dimension}")

    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - exercised by a stubbed import
        raise ImageSupportUnavailable(
            "reading an image needs Pillow, which is an optional dependency: "
            "pip install 'ffwf-tau-agent-core[images]' "
            "(or 'ffwf-tau', which includes it)"
        ) from e

    with Image.open(io.BytesIO(data)) as img:
        width, height = img.size
        if width <= max_dimension and height <= max_dimension:
            return ResizedImage(data, mime_type, (width, height), (width, height), False)

        scale = max_dimension / max(width, height)
        # At least 1px per side: a 4000x3 source scaled by 0.5 rounds the short
        # side to 1, and int() would make it 0, which Pillow rejects.
        target = (max(1, round(width * scale)), max(1, round(height * scale)))

        fmt = _FORMATS.get(mime_type, "PNG")
        out_mime = mime_type if mime_type in _FORMATS else "image/png"
        # JPEG has no alpha channel. A source that is RGBA and encodes as JPEG
        # raises in Pillow, so the mode is converted rather than the failure
        # being reported as a resize fault.
        # `Image.Resampling.LANCZOS` rather than the `Image.LANCZOS` alias: the
        # alias is untyped, so mypy reports it as a missing attribute.
        shrunk = img.resize(target, Image.Resampling.LANCZOS)
        if fmt == "JPEG" and shrunk.mode not in ("RGB", "L"):
            shrunk = shrunk.convert("RGB")

        buf = io.BytesIO()
        if fmt == "JPEG":
            shrunk.save(buf, format=fmt, quality=80)
        else:
            shrunk.save(buf, format=fmt)
        return ResizedImage(buf.getvalue(), out_mime, (width, height), target, True)
