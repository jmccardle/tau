"""A branch's model is called with that model's key, never the spawner's.

docs/CURSORS.md §6. ``AgentSession.resolve_model`` is the one way a
credential enters the session; a branch, a ``ctx.complete`` and a plain resolver
all go through it, so a parent on provider A never hands its key to a child on
provider B (the measured defect: HTTP 401 from Mistral with an Anthropic key).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.seed import seed_tool_call
from tau_agent_core.session_log import InMemorySessionLog, config_at


def _model(name: str, provider: str) -> Model:
    return Model(
        id=name,
        name=name,
        provider=provider,
        api="openai-completions",
        base_url=f"http://127.0.0.1:1/{provider}",
        context_window=8192,
        max_tokens=256,
    )


PARENT = _model("parent-model", "anthropic")
CHILD = _model("child-model", "mistral")
KEYLESS = _model("keyless-model", "mistral")


class _Resolver:
    """A ``ConfigModelResolver`` stand-in: names to models, and each name's config key."""

    MODELS = {"parent-model": PARENT, "child-model": CHILD, "keyless-model": KEYLESS}
    KEYS = {"parent-model": "key-A", "child-model": "key-B", "keyless-model": None}

    def __call__(self, name: str) -> Model:
        return self.MODELS[name]

    def api_key(self, name: str) -> str | None:
        return self.KEYS[name]


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="done")],
        api="openai-completions",
        provider="x",
        model="x",
        stop_reason="stop",
        timestamp=1_700_000_000_000,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, message: AssistantMessage) -> None:
        self._message = message

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta="done", partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


class _Provider:
    """Records the model and options of every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[Model, dict[str, Any]]] = []

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.calls.append((model, dict(options or {})))
        return _Stream(_reply())

    def key(self, index: int = 0) -> str | None:
        return self.calls[index][1].get("api_key")


async def _session(resolver: Any = None) -> AgentSession:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=PARENT,
        system_prompt="",
        tools=[],
        api_key="key-A",
        model_resolver=resolver if resolver is not None else _Resolver(),
    )
    await session.cursor.append_message(
        {"role": "user", "content": [{"type": "text", "text": "shared prefix"}]}
    )
    return session


async def _spawn(
    session: AgentSession, model: Any, provider: _Provider, *, seeded: bool = False
) -> Any:
    shape: dict[str, Any] = (
        {"seed": [*seed_tool_call("start_subagent", {}, "go")]} if seeded else {"prompt": "go"}
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        result = await session._extension_api.context.spawn_branch(
            session.cursor.leaf, tools=[], model=model, **shape
        )
    assert result.ok, result.error
    return result


@pytest.mark.parametrize("seeded", [False, True])
async def test_a_child_named_on_another_provider_gets_that_providers_key(seeded: bool):
    session = await _session()
    provider = _Provider()

    result = await _spawn(session, "child-model", provider, seeded=seeded)

    assert provider.calls[0][0].id == "child-model"
    assert provider.key() == "key-B", "the parent's key-A must never reach provider B"
    assert config_at(session.session_log.entries(), result.leaf)["model"] == "child-model"


@pytest.mark.parametrize("seeded", [False, True])
async def test_a_child_whose_config_has_no_key_gets_none_not_the_parents(seeded: bool):
    session = await _session()
    provider = _Provider()

    await _spawn(session, "keyless-model", provider, seeded=seeded)

    assert provider.key() is None, "None means the provider's environment variable"


async def test_a_foreign_model_object_gets_no_key():
    session = await _session()
    provider = _Provider()

    await _spawn(session, CHILD, provider)

    assert provider.key() is None


async def test_the_sessions_own_model_object_keeps_the_sessions_key():
    session = await _session()
    provider = _Provider()

    await _spawn(session, PARENT, provider)

    assert provider.key() == "key-A"


async def test_no_model_runs_the_sessions_model_and_key():
    session = await _session()
    provider = _Provider()

    await _spawn(session, None, provider)

    assert (provider.calls[0][0].id, provider.key()) == ("parent-model", "key-A")


async def test_a_plain_resolver_knows_no_keys_so_another_model_gets_none():
    """A ``name -> Model`` resolver used to hand every model the session's key."""
    session = await _session(resolver=lambda name: _Resolver.MODELS[name])

    assert session.resolve_model("child-model") == (CHILD, None)
    assert session.resolve_model("parent-model") == (PARENT, "key-A")


async def test_ctx_complete_calls_a_named_model_with_its_own_key(monkeypatch):
    session = await _session()
    calls: list[tuple[Model, dict[str, Any]]] = []

    async def fake_complete_simple(model, context, options=None):
        calls.append((model, dict(options or {})))
        return _reply()

    monkeypatch.setattr("tau_llm.client.complete_simple", fake_complete_simple)
    ctx = session._extension_api.context

    await ctx.complete([{"role": "user", "content": "hi"}], model="child-model")
    await ctx.complete([{"role": "user", "content": "hi"}], model=CHILD)
    await ctx.complete([{"role": "user", "content": "hi"}])

    assert [c[1].get("api_key") for c in calls] == ["key-B", None, "key-A"]
