"""The mock provider: a reply streamed word by word, no network, no key (D15).

Settings: `base_url` is refused (there is nothing to call). `options`, anything else being a
problem in `check`: `reply` (the text to stream; default: the walk's last user input after
`mock: `) and `delay_ms` (a non-negative integer, default 30, before each chunk). `max_tokens`
caps the words streamed and ends the reply with the length stop.

It takes images (`image_limits`, as an OpenAI-compatible server: 20 MiB, no side limit) for a
model whose config says `images: true`, and echoes a user message's as one more line,
`[N images: image/png 640x480, ...]` (D31): enough to see which reached the request.
"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Sequence
from typing import cast

from tau._config import ModelConfig
from tau._core import Body, Image, Node, Part, Stop
from tau._events import Delta, TextDelta
from tau._images import MIB, ImageLimits, count
from tau._protocols import Finish, SampleError, Tool
from tau._providers._wire import is_count, option_problems

NAME = "mock"
DEFAULT_DELAY_MS = 30
_OPTIONS = ("reply", "delay_ms")
_CHUNK = re.compile(r"\S+\s*|\s+")


def _echoed(images: Sequence[Image]) -> str:
    """A user message's images as the mock's reply names them; nothing for none."""
    if not images:
        return ""
    shown = ", ".join(f"{i.mime} {i.width}x{i.height}" for i in images)
    return f"\n[{count(len(images))}: {shown}]"


class MockProvider:
    """Streams a canned or echoed reply. Holds nothing open."""

    image_limits = ImageLimits(20 * MIB)
    """What one image may be (D30): as an OpenAI-compatible server takes it."""

    @property
    def name(self) -> str:
        """`"mock"`: what a model's `provider` setting calls it."""
        return NAME

    def check(self, model: ModelConfig) -> list[str]:
        """Problems with the model's settings: a `base_url` it has no use for, an unknown option,
        or a bad `reply` or `delay_ms`. Empty if the model is fine."""
        problems = option_problems(model, _OPTIONS)
        if model.base_url is not None:
            problems.append("has no use for base_url; remove it")
        if "reply" in model.options and (
            not isinstance(model.options["reply"], str) or not model.options["reply"]
        ):
            problems.append("option 'reply' must be a non-empty string")
        if "delay_ms" in model.options and not is_count(model.options["delay_ms"]):
            problems.append("option 'delay_ms' must be a non-negative integer")
        return problems

    async def aclose(self) -> None:
        """Nothing is held open."""

    def _reply(self, walk: Sequence[Node], model: ModelConfig) -> str:
        if "reply" in model.options:  # `check` held it to a non-empty string
            return cast("str", model.options["reply"])
        for node in reversed(walk):
            if isinstance(node.body, Body.User):
                return f"mock: {node.body.text}{_echoed(node.body.images)}"
        raise SampleError("the walk has no user input to echo and the model has no `reply` option")

    async def sample(
        self, walk: Sequence[Node], model: ModelConfig, tools: Sequence[Tool]
    ) -> AsyncGenerator[Part | Finish | Delta, None]:
        """Stream the reply's chunks (a word and the space after it) as `TextDelta`s, then the
        whole `Text`, then the `Finish` with normalized `usage` in `meta`."""
        chunks = _CHUNK.findall(self._reply(walk, model))
        stop = Stop.End
        if model.max_tokens is not None and len(chunks) > model.max_tokens:
            chunks = chunks[: model.max_tokens]
            stop = Stop.Length
        delay = cast("int", model.options.get("delay_ms", DEFAULT_DELAY_MS))  # held by `check`
        for chunk in chunks:
            await asyncio.sleep(delay / 1000)
            yield TextDelta(chunk)
        text = "".join(chunks)
        yield Part.Text(text)
        said = " ".join(n.body.text for n in walk if isinstance(n.body, Body.User))
        usage = {"input": len(said.split()), "output": len(text.split()), "cached": 0}
        yield Finish(
            stop, "length" if stop == Stop.Length else "stop", json.dumps({"usage": usage})
        )
