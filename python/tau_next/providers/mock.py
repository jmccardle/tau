"""The mock provider: a reply streamed word by word, no network, no key (D15).

Settings: `base_url` is refused (there is nothing to call). `options`, anything else being a
problem in `check`: `reply` (the text to stream; default: the walk's last user input after
`mock: `) and `delay_ms` (a non-negative integer, default 30, before each chunk). `max_tokens`
caps the words streamed and ends the reply with the length stop.
"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Sequence
from typing import cast

from tau_next._config import ModelConfig
from tau_next._core import Body, Node, Part, Stop
from tau_next.events import Delta, TextDelta
from tau_next.protocols import Finish, SampleError, Tool

NAME = "mock"
DEFAULT_DELAY_MS = 30
_OPTIONS = ("reply", "delay_ms")
_CHUNK = re.compile(r"\S+\s*|\s+")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class MockProvider:
    """Streams a canned or echoed reply. Holds nothing open."""

    @property
    def name(self) -> str:
        return NAME

    def check(self, model: ModelConfig) -> list[str]:
        problems: list[str] = []
        if model.provider != NAME:
            problems.append(f"provider is {model.provider!r}, not {NAME!r}")
        if model.base_url is not None:
            problems.append("has no use for base_url; remove it")
        problems += [
            f"unknown option {key!r}; known options are {list(_OPTIONS)}"
            for key in model.options
            if key not in _OPTIONS
        ]
        if "reply" in model.options and (
            not isinstance(model.options["reply"], str) or not model.options["reply"]
        ):
            problems.append("option 'reply' must be a non-empty string")
        if "delay_ms" in model.options and not _is_count(model.options["delay_ms"]):
            problems.append("option 'delay_ms' must be a non-negative integer")
        return problems

    async def aclose(self) -> None:
        """Nothing is held open."""

    def _reply(self, walk: Sequence[Node], model: ModelConfig) -> str:
        if "reply" in model.options:  # `check` held it to a non-empty string
            return cast("str", model.options["reply"])
        for node in reversed(walk):
            if isinstance(node.body, Body.User):
                return f"mock: {node.body.text}"
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
