"""Which images a model takes (D30 section 6): `ImageLimits`, and the one rule the driver holds a
tool's images to when the call answers, a user's images to when they are sent and after the
`pre_input` hooks (D31), and a walk's images to before a sample.

A model takes images only if its config says `images: true`, and only within what its provider's
API takes (`Provider.image_limits`). Nothing is resized and nothing is dropped: an image that does
not fit is an error that says why.

`ImageInput` is one image a client sends with an input (D31): base64 bytes, checked where the
call's params are decoded, or the `sha256` of an image the session already keeps.
"""

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

from tau._config import ModelConfig
from tau._core import BadImage, Body, Image, Node, Outcome

if TYPE_CHECKING:
    from tau._protocols import Provider

MIB = 1024 * 1024
MAX_INPUT_IMAGES: Final = 20
"""The most images one input may carry (D31; fixed for 0.18)."""
MAX_INPUT_BYTES: Final = 20 * MIB
"""The most bytes one image of an input may be, before base64 (D31; fixed for 0.18)."""
_SHA256_HEX: Final = frozenset("0123456789abcdef")
_SHA256_LEN: Final = 64


@dataclass(frozen=True)
class ImageInput:
    """One image a client sends with an input (D31): exactly one of `data`, the image file's bytes
    in standard base64 (with padding, nothing else), or `sha256`, the name of an image this
    session already keeps (a node's or an input's; nothing is read from a path). The format and
    size are read from the bytes, never taken from the client. Built from the wire or by hand,
    it is checked at once: `BadImage` (a `ValueError`) for bytes that are not strict base64 of
    a PNG, JPEG, GIF or WebP image of at most `MAX_INPUT_BYTES`, or a `sha256` that is not 64
    lowercase hex digits. Its `repr` never shows the bytes."""

    data: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.sha256 is None):
            raise BadImage("an image input has exactly one of `data` (base64) and `sha256`")
        if self.sha256 is not None:
            if len(self.sha256) != _SHA256_LEN or not set(self.sha256) <= _SHA256_HEX:
                raise BadImage("an image's sha256 is 64 lowercase hex digits")
            return
        self.image()

    @classmethod
    def of(cls, data: bytes) -> Self:
        """The input of the image file `data`: its bytes as base64."""
        return cls(data=base64.b64encode(data).decode("ascii"))

    def image(self) -> Image | None:
        """The image `data` holds, with its bytes; `None` for a `sha256`, which the session
        resolves. `BadImage` as at construction."""
        if self.data is None:
            return None
        # A base64 text this long cannot decode to bytes within the limit: refused unread.
        if len(self.data) > (MAX_INPUT_BYTES + 2) // 3 * 4:
            raise BadImage(f"an image input is over {MAX_INPUT_BYTES} bytes")
        try:
            raw = binascii.a2b_base64(self.data.encode("ascii"), strict_mode=True)
        except (binascii.Error, UnicodeEncodeError):
            raise BadImage("an image input's data is not strict base64") from None
        return Image(raw)

    def __repr__(self) -> str:
        if self.data is not None:
            return f"ImageInput(<{len(self.data) * 3 // 4} bytes>)"
        return f"ImageInput(sha256={self.sha256!r})"


@dataclass(frozen=True)
class ImageLimits:
    """What one image may be for a provider's API: at most `max_bytes` bytes (the file, not its
    base64) and `max_side` pixels on its longer side (`None`: no limit on the side). A provider
    declares its own as the optional attribute `image_limits`; a provider without one sends no
    images. `ValueError` for a limit that is not a positive integer."""

    max_bytes: int
    max_side: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_side"):
            value: object = getattr(self, name)
            if value is None and name == "max_side":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ImageLimits.{name} must be a positive integer, not {value!r}")

    def describe(self) -> str:
        """The limits as a phrase: `at most N bytes and S px a side`."""
        side = "" if self.max_side is None else f" and {self.max_side} px a side"
        return f"at most {self.max_bytes} bytes{side}"


def describe(image: Image) -> str:
    """One image as the errors name it: `image/png 640x480, 1234 bytes`."""
    size = "" if image.data is None else f", {len(image.data)} bytes"
    return f"{image.mime} {image.width}x{image.height}{size}"


def shown(outcome: Outcome.Ok) -> str:
    """An `Ok` as a frontend shows it (the TUI, `tau export`): its text, then one line per image,
    `[image WxH mime]`. The bytes are never shown."""
    lines = labels(outcome.images)
    return "\n".join([outcome.value, *lines] if outcome.value else lines)


def count(n: int) -> str:
    return "1 image" if n == 1 else f"{n} images"


def images_problem(
    images: Sequence[Image], name: str, model: ModelConfig, provider: "Provider | None"
) -> str | None:
    """Why `model` (qualified `name`) on `provider` cannot be sent `images`, or `None` if it can:
    it does not take images, its provider declares no `image_limits`, or an image is over them.
    An image without its bytes has no size to check, and is a problem too."""
    if not images:
        return None
    if not model.images:
        return (
            f"the model {name} does not take images (its config has no `images: true`; "
            "set it if the model can see)"
        )
    limits: object = getattr(provider, "image_limits", None)
    if not isinstance(limits, ImageLimits):
        who = "its provider" if provider is None else f"its provider {provider.name!r}"
        return f"the model {name} takes images, but {who} sends none (it has no `image_limits`)"
    for i, image in enumerate(images, 1):
        if image.data is None:
            return f"image {i} ({describe(image)}) has no bytes to send"
        too_big = len(image.data) > limits.max_bytes
        too_wide = limits.max_side is not None and max(image.width, image.height) > limits.max_side
        if too_big or too_wide:
            return (
                f"image {i} ({describe(image)}) is over what the model {name} takes "
                f"({limits.describe()}); τ does not resize images: make a smaller copy and send "
                "that"
            )
    return None


def outcome_images(outcome: Outcome) -> list[Image]:
    """The images of an `Ok`; none for anything else."""
    return list(outcome.images) if isinstance(outcome, Outcome.Ok) else []


def refused(outcome: Outcome.Ok, tool: str, problem: str) -> Outcome:
    """The `Err` a call answers when its images do not fit: what was refused and why, and the
    tool's text, so nothing it said is lost."""
    text = f" The tool's text: {outcome.value}" if outcome.value else ""
    return Outcome.Err(f"{tool} returned {count(len(outcome.images))}, not sent: {problem}.{text}")


def labels(images: Sequence[Image]) -> list[str]:
    """One `[image WxH mime]` line per image: how an image no longer sent is named (prune, the
    TUI, `tau export`). The bytes are never shown."""
    return [f"[image {i.width}x{i.height} {i.mime}]" for i in images]


def with_text(body: Body, text: str) -> Body:
    """`body` with `text` in place of its own: a `User` keeps its images (D31)."""
    if isinstance(body, Body.User):
        return Body.User(text, body.images)
    return body


def walk_problem(
    walk: Sequence[Node], name: str, model: ModelConfig, provider: "Provider"
) -> str | None:
    """Why the walk cannot be sent to `model` (qualified `name`) for its images, naming the first
    node whose images do not fit; `None` if they all do."""
    for node in walk:
        problem = images_problem(node.body.images, name, model, provider)
        if problem is not None:
            return (
                f"node {node.id} holds {count(len(node.body.images))} the request cannot "
                f"carry: {problem}. Go back before it, or switch to a model that takes them"
            )
    return None
