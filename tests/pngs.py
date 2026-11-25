"""PNG files for the image tests (D30), written with the standard library: no image package.

`png(width, height, pixel)` encodes an RGB image; `picture()` is the one the live tests show a
model: a red disc on white with a black word in it, so a model that sees it can say all three.
"""

import struct
import zlib
from collections.abc import Callable

Rgb = tuple[int, int, int]

WHITE: Rgb = (255, 255, 255)
RED: Rgb = (220, 30, 30)
BLACK: Rgb = (0, 0, 0)

# A 5x7 bitmap of the letters the word uses, one string per row ("#" is ink).
_GLYPHS = {
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
}
WORD = "HELLO"


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def png(width: int, height: int, pixel: Callable[[int, int], Rgb] = lambda x, y: WHITE) -> bytes:
    """An 8-bit RGB PNG of `width` by `height`, each pixel `pixel(x, y)`."""
    rows = b"".join(
        b"\0" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows, 9))
        + _chunk(b"IEND", b"")
    )


def blank(width: int, height: int) -> bytes:
    """A white PNG of `width` by `height`, made fast enough for a 4K screenshot."""
    rows = (b"\0" + b"\xff" * (3 * width)) * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows, 6))
        + _chunk(b"IEND", b"")
    )


def picture(width: int = 320, height: int = 240, scale: int = 6) -> bytes:
    """A red disc on white with `WORD` in black across its middle."""
    cx, cy, r = width // 2, height // 2, min(width, height) * 2 // 5
    glyph_w = (5 + 1) * scale
    left = cx - (len(WORD) * glyph_w - scale) // 2
    top = cy - 7 * scale // 2

    def ink(x: int, y: int) -> bool:
        gx, gy = x - left, y - top
        if gx < 0 or gy < 0 or gy >= 7 * scale:
            return False
        letter, col = divmod(gx, glyph_w)
        if letter >= len(WORD) or col >= 5 * scale:
            return False
        return _GLYPHS[WORD[letter]][gy // scale][col // scale] == "#"

    def pixel(x: int, y: int) -> Rgb:
        if ink(x, y):
            return BLACK
        return RED if (x - cx) ** 2 + (y - cy) ** 2 <= r * r else WHITE

    return png(width, height, pixel)
