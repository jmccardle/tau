"""``ext`` on the file store: it reloads, travels with a fork, and not with a copy.

docs/ENTRY-ANNOTATIONS.md §4.2.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.tree_surgery import copy_of
from tau_coding_agent.session_store import Session


class _Stream:
    """A one-message stream_simple result."""

    def __init__(self, final: AssistantMessage) -> None:
        self._events = [DoneEvent(final=final, usage=Usage())]

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        self._i += 1
        return self._events[self._i - 1]

    async def result(self) -> Any:
        return self._events[0].final

    def abort(self) -> None:
        pass


async def _fake(model, context, options=None):
    return _Stream(
        AssistantMessage(
            content=[TextContent(text="ok")],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",
            timestamp=1_700_000_000_000,
        )
    )


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _annotator(api):
    api.on("annotate", lambda event, ctx: {"kind": event["kind"]})


async def _annotated_store(tmp_path) -> Session:
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(session_log=store, model=_model(), extensions=[_annotator])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        await session.prompt("go")
    return store


def _annotated(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if "ext" in e]


async def test_ext_survives_reload(tmp_path) -> None:
    store = await _annotated_store(tmp_path)
    reloaded = Session.load(store.path)
    assert reloaded.entries() == store.entries()
    kinds = [next(iter(e["ext"].values()))["kind"] for e in _annotated(reloaded.entries())]
    assert kinds == ["user", "assistant"]


async def test_ext_travels_with_a_fork_at_an_entry(tmp_path) -> None:
    store = await _annotated_store(tmp_path)
    assistant = _annotated(store.entries())[-1]
    forked = Session.fork(store, "/tmp", base_dir=tmp_path, at=assistant["id"])
    on_disk = Session.load(forked.path)
    assert [e["ext"] for e in _annotated(on_disk.entries())] == [
        e["ext"] for e in _annotated(store.entries())
    ]


async def test_copy_of_drops_ext(tmp_path) -> None:
    store = await _annotated_store(tmp_path)
    entry = _annotated(store.entries())[0]
    kind, payload = copy_of(entry)
    assert kind == "message"
    assert "ext" not in payload
    assert payload["copiedFrom"] == entry["id"]
