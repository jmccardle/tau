"""Styled output survives an outstanding prompt — docs/REPL-HEAD.md §7.

``patch_stdout``'s default proxy calls ``Output.write``, which replaces every
``\\x1b`` with ``?``. The REPL prints ``rich`` styling from a render subscription
while a read is open, so without ``raw=True`` a turn's footer reached the
terminal as literal ``?[2mctx 15,103 · out 261 · 1.7s?[0m``.
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.console import Console

from tau_coding_agent.repl_input import PromptToolkitReader


class _Recorder:
    """Stands in for ``patch_stdout``, recording the keywords it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> "_Recorder":
        self.calls.append(kwargs)
        return self

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def reader() -> PromptToolkitReader:
    return PromptToolkitReader(Console())


async def test_read_patches_stdout_raw(reader: PromptToolkitReader) -> None:
    recorder = _Recorder()
    reader._patch_stdout = recorder  # type: ignore[assignment]

    async def prompt_async(**kwargs: Any) -> str:
        return "hello"

    reader._session.prompt_async = prompt_async  # type: ignore[assignment]

    assert await reader.read() == "hello"
    assert recorder.calls == [{"raw": True}]


async def test_ask_patches_stdout_raw(reader: PromptToolkitReader) -> None:
    recorder = _Recorder()
    reader._patch_stdout = recorder  # type: ignore[assignment]

    async def prompt_async(**kwargs: Any) -> str:
        return "yes"

    reader._session.prompt_async = prompt_async  # type: ignore[assignment]

    assert await reader.ask("continue?") == "yes"
    assert recorder.calls == [{"raw": True}]


def test_escape_is_what_the_default_proxy_destroys() -> None:
    """The upstream behaviour this guards against, pinned so a version bump reports it."""
    import inspect

    from prompt_toolkit.output.vt100 import Vt100_Output

    assert 'replace("\\x1b", "?")' in inspect.getsource(Vt100_Output.write)
