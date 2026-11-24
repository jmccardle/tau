"""Which images a model takes (D30 section 6): `ImageLimits`, and the one rule the driver holds a
tool's images to when the call answers and a walk's images to before a sample.

A model takes images only if its config says `images: true`, and only within what its provider's
API takes (`Provider.image_limits`). Nothing is resized and nothing is dropped: an image that does
not fit is an error that says why.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tau._config import ConfigError, ModelConfig
from tau._core import Body, Image, Node, Outcome

if TYPE_CHECKING:
    from tau._config_layers import ModelCatalog
    from tau._protocols import Provider

MIB = 1024 * 1024


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
    lines = [f"[image {i.width}x{i.height} {i.mime}]" for i in outcome.images]
    return "\n".join([outcome.value, *lines] if outcome.value else lines)


def _count(n: int) -> str:
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
    return Outcome.Err(f"{tool} returned {_count(len(outcome.images))}, not sent: {problem}.{text}")


def node_images(body: Body) -> list[Image]:
    """The images of a `Results` node, in call order."""
    if not isinstance(body, Body.Results):
        return []
    return [i for _, o, _ in body.outcomes for i in outcome_images(o)]


def walk_problem(
    walk: Sequence[Node], name: str, model: ModelConfig, provider: "Provider"
) -> str | None:
    """Why the walk cannot be sent to `model` (qualified `name`) for its images, naming the first
    node whose images do not fit; `None` if they all do."""
    for node in walk:
        problem = images_problem(node_images(node.body), name, model, provider)
        if problem is not None:
            return (
                f"node {node.id} holds {_count(len(node_images(node.body)))} the request cannot "
                f"carry: {problem}. Go back before it, or switch to a model that takes them"
            )
    return None


def call_gate(
    providers: Mapping[str, "Provider"],
    catalog: "ModelCatalog",
    model_at: Callable[[int], str],
    node: int,
) -> Callable[[Sequence[Image]], str | None]:
    """The image rule for the calls of the batch at `node`: the model in force there. Resolved
    when a call first answers with images, so a batch without any reads no config."""

    def problem(images: Sequence[Image]) -> str | None:
        if not images:
            return None
        name = model_at(node)
        try:
            model = catalog.resolve(name)
        except ConfigError as e:
            return f"the model at this batch cannot be read: {e}"
        return images_problem(images, name, model, providers.get(model.provider))

    return problem
