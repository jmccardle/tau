"""Two cursors from one leaf, two models, one file (docs/TAU-SERVE.md §8).

The shape the compare demo drives: both cursors start at the same leaf, each runs
under its own model, their turns overlap, and the file store holds both branches
with each branch's config entry ahead of its turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import TurnFrame
from tau_agent_core.session_log import config_at
from tau_agent_core.submission import Submission

from tau_coding_agent.session_store import Session

_TS = 1_700_000_000_000


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name=model_id,
        context_window=8192,
        max_tokens=256,
    )


class _Stream:
    def __init__(self, model_id: str) -> None:
        self._message = AssistantMessage(
            content=[TextContent(text=f"from {model_id}")],
            api="openai-completions",
            provider="openai",
            model=model_id,
            stop_reason="stop",
            timestamp=_TS,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


class _Gated:
    """Holds every call until both are in flight, then answers as the called model."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.models: list[str] = []

    async def stream(self, model: Model, context: Any, options: Any = None) -> _Stream:
        self.models.append(model.id)
        await self.gate.wait()
        return _Stream(model.id)


def _sub(text: str) -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=f"s-{text}-{id(text)}",
        multitask_strategy="enqueue",
    )


async def test_two_models_from_one_leaf_run_at_once_into_one_file(tmp_path: Path) -> None:
    log = Session.create(str(tmp_path), "base", "openai", base_dir=tmp_path / "s")
    session = AgentSession(session_log=log, model=_model("base"), tools=[], cwd=str(tmp_path))
    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream("base")):
        await session.submit(_sub("first"))
    fork_point = session.cursor.leaf
    assert fork_point is not None

    cursors = []
    for model_id in ("model-a", "model-b"):
        cursor = await session.open_cursor(fork_point, owner=session.cursor, label=model_id)
        cursor.frame = TurnFrame(tools=(), model=_model(model_id), hooks=True)
        cursors.append(cursor)

    provider = _Gated()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        turns = [
            asyncio.create_task(session.submit(_sub("same prompt"), cursor=c)) for c in cursors
        ]
        while len(provider.models) < 2:
            await asyncio.sleep(0)
        assert all(c.busy for c in cursors), "both turns are in flight at once"
        provider.gate.set()
        await asyncio.gather(*turns)

    assert sorted(provider.models) == ["model-a", "model-b"]
    reopened = Session.load(log.path)
    entries = reopened.entries()
    for cursor, model_id in zip(cursors, ("model-a", "model-b")):
        path = ConversationTree(entries, cursor.leaf).path()
        assert fork_point in [e["id"] for e in path], "each branch grows from the shared leaf"
        assert config_at(entries, cursor.leaf)["model_spec"]["id"] == model_id
        assert path[-1]["message"]["content"][0]["text"] == f"from {model_id}"
    a, b = (ConversationTree(entries, c.leaf).path() for c in cursors)
    assert {e["id"] for e in a} & {e["id"] for e in b} == {
        e["id"] for e in ConversationTree(entries, fork_point).path()
    }, "the branches share exactly the path to the fork point"
