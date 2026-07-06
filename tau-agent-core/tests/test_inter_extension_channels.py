"""S52 (E7) — inter-extension pub/sub channels (``api.emit`` / ``api.on("ext:…")``).

Blesses the latent ``EventBus.emit_channel`` facility as the transport for custom
inter-extension channels namespaced ``ext:<name>:<topic>`` (a faithful port of pi's
``pi.events`` — ``event-bus.ts``). One extension broadcasts with ``api.emit(topic,
payload)``; another receives it with ``api.on("ext:<name>:<topic>", handler)``. The
channel is namespaced under the EMITTING extension's own stem, so provenance is
unforgeable and an extension can only publish under its own name.

The load-bearing invariant proven here (roadmap §3 S52): these channels are in-RAM,
fire-and-forget, and **NEVER model-visible** — explicitly NOT a backplane (the tree
is). ``TestNotABackplane`` asserts a payload that crosses the bus leaves NO trace on
the session log / model context. Since nothing is persisted, there is nothing to
reload — the "durability" here is deliberately absent, and the test proves that.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S52; docs/EXTENSIONS-E5-WIRING.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import EventBus
from tau_agent_core.extension_types import ExtensionAPI, ext_channel
from tau_agent_core.extensions.runner import ExtensionRunner
from tau_agent_core.session_log import InMemorySessionLog


# ── unit level: two apis sharing ONE bus, each with its own bucket ────────────


def _bound_api(bus: EventBus, runner: ExtensionRunner, path: str) -> ExtensionAPI:
    """An ExtensionAPI bound to a runner bucket labelled ``path``, sharing ``bus``.

    Mirrors how a real ``AgentSession`` binds every loaded extension to the SAME
    ``self._events`` bus (so custom channels reach across extensions) while giving
    each its own :class:`ExtensionHandlers` bucket (so ``emit`` can derive an
    identity from ``Path(bucket.path).stem``).
    """
    bucket = runner.register_extension(path)
    return ExtensionAPI(event_bus=bus, hook_handlers=bucket)


class TestChannelNaming:
    def test_ext_channel_builds_namespaced_string(self) -> None:
        assert ext_channel("publisher", "ping") == "ext:publisher:ping"

    async def test_emit_uses_the_emitters_own_stem(self) -> None:
        """The channel is namespaced under the EMITTER, from a plain ``.py`` path."""
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        seen: list[Any] = []
        # A subscriber who knows the publisher's name receives it…
        bus.on("ext:publisher:ping", lambda payload: seen.append(payload))
        await pub.emit("ping", {"n": 1})
        assert seen == [{"n": 1}]


class TestPubSub:
    async def test_second_extension_receives_first_extensions_emit(self) -> None:
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        sub = _bound_api(bus, runner, "/x/subscriber.py")

        received: list[Any] = []
        sub.on(ext_channel("publisher", "ping"), lambda payload: received.append(payload))

        await pub.emit("ping", {"msg": "hi", "from": "publisher"})

        assert received == [{"msg": "hi", "from": "publisher"}]

    async def test_async_subscriber_is_awaited(self) -> None:
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        sub = _bound_api(bus, runner, "/x/subscriber.py")

        received: list[Any] = []

        async def on_ping(payload: Any) -> None:
            received.append(payload)

        sub.on("ext:publisher:ping", on_ping)
        await pub.emit("ping", 42)

        assert received == [42]

    async def test_wrong_namespace_does_not_receive(self) -> None:
        """Provenance: an extension only publishes under its OWN stem."""
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        sub = _bound_api(bus, runner, "/x/subscriber.py")

        received: list[Any] = []
        # Subscribing under a name the publisher cannot emit as → never fires.
        sub.on("ext:subscriber:ping", lambda payload: received.append(payload))
        await pub.emit("ping", {"n": 1})

        assert received == []

    async def test_bidirectional(self) -> None:
        bus = EventBus()
        runner = ExtensionRunner()
        a = _bound_api(bus, runner, "/x/alpha.py")
        b = _bound_api(bus, runner, "/x/beta.py")

        at_b: list[Any] = []
        at_a: list[Any] = []
        b.on("ext:alpha:hello", lambda p: at_b.append(p))
        a.on("ext:beta:reply", lambda p: at_a.append(p))

        await a.emit("hello", "from-alpha")
        await b.emit("reply", "from-beta")

        assert at_b == ["from-alpha"]
        assert at_a == ["from-beta"]


class TestFailEarly:
    async def test_empty_topic_raises(self) -> None:
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        with pytest.raises(ValueError, match="non-empty string"):
            await pub.emit("", {})
        with pytest.raises(ValueError, match="non-empty string"):
            await pub.emit("   ", {})

    async def test_non_string_topic_raises(self) -> None:
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        with pytest.raises(ValueError, match="non-empty string"):
            await pub.emit(123, {})  # type: ignore[arg-type]

    async def test_unbound_api_has_no_identity_and_raises(self) -> None:
        """A bare ExtensionAPI (no runner bucket) cannot namespace a channel."""
        bare = ExtensionAPI()  # lazily makes its own bus/registry, hook_handlers=None
        with pytest.raises(RuntimeError, match="not bound to an ExtensionRunner"):
            await bare.emit("ping", {})


class TestHandlerErrorSurfaced:
    async def test_raising_subscriber_is_surfaced_not_swallowed(self) -> None:
        """A throwing handler routes to on_error (S44); siblings still run."""
        bus = EventBus()
        runner = ExtensionRunner()
        pub = _bound_api(bus, runner, "/x/publisher.py")
        sub = _bound_api(bus, runner, "/x/subscriber.py")

        surfaced: list[tuple[str, str]] = []
        bus.on_error(lambda exc, channel: surfaced.append((str(exc), channel)))

        def boom(_payload: Any) -> None:
            raise ValueError("kaboom")

        good: list[Any] = []
        sub.on("ext:publisher:ping", boom)
        sub.on("ext:publisher:ping", lambda p: good.append(p))

        await pub.emit("ping", {"n": 7})

        assert good == [{"n": 7}]  # sibling ran despite the raise
        assert surfaced and "kaboom" in surfaced[0][0]
        assert surfaced[0][1] == "ext:publisher:ping"  # channel attributed


# ── integration: two LOADED file extensions, end-to-end through AgentSession ──


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model)


# The publisher registers a tool whose execute BROADCASTS on its custom channel,
# then returns a bland tool result (whose text is deliberately DISJOINT from the
# broadcast payload, so the integration test can prove the payload never entered
# the tree). ``_PAYLOAD_MARKER`` is the string that must appear on the wire-side
# channel but NOWHERE on the model-visible path.
_PAYLOAD_MARKER = "hello-from-tool-CHANNEL-ONLY"

_PUBLISHER_EXT = f"""
async def _ping_exec(tool_call_id, params, signal, on_update, ctx):
    await register._api.emit("ping", {{"msg": {_PAYLOAD_MARKER!r}}})
    return {{"content": [{{"type": "text", "text": "ping-sent"}}]}}

def register(api):
    register._api = api
    api.register_tool({{
        "name": "ping_tool",
        "description": "broadcast a ping on the inter-extension bus",
        "parameters": {{"type": "object", "properties": {{}}}},
        "execute": _ping_exec,
    }})
"""

# The subscriber listens on the publisher's channel and writes each received
# payload as a JSON line to a sink path baked into its source (the established
# capture idiom, cf. test_extension_config.py). If the cross-extension delivery
# works, the sink file gains a line; the file is the test's observation port.
_SUBSCRIBER_EXT = """
import json

def register(api):
    def on_ping(payload):
        with open({sink!r}, "a") as f:
            f.write(json.dumps(payload) + "\\n")

    api.on("ext:publisher:ping", on_ping)
"""


class TestTwoLoadedExtensions:
    async def test_pub_sub_across_two_loaded_extensions(self, tmp_path):
        """A tool in one loaded extension emits; a second loaded extension receives."""
        from unittest.mock import patch

        sink = tmp_path / "received.jsonl"
        pub = tmp_path / "publisher.py"
        sub = tmp_path / "subscriber.py"
        pub.write_text(_PUBLISHER_EXT)
        sub.write_text(_SUBSCRIBER_EXT.format(sink=str(sink)))

        session = _make_session()
        # Load BOTH extensions into the one session (shared event bus).
        result = await session.load_extensions([str(pub), str(sub)], discover=False)
        assert result.errors == []
        assert len(result.extensions) == 2

        with patch(
            "tau_agent_core.agent_loop.stream_simple",
            side_effect=_fake_stream_calling("ping_tool", {}),
        ):
            messages = await session.prompt("please ping")

        # The subscriber received the publisher's broadcast, across extensions.
        assert sink.exists(), "subscriber never received the emit"
        lines = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
        assert lines == [{"msg": _PAYLOAD_MARKER}]

        # …and the model-visible path is untouched: the tool DID run (bland result
        # persisted), but the broadcast payload appears NOWHERE on the tree.
        assert _has_tool_result(messages, "ping_tool")
        self._assert_payload_absent_from_context(session, messages)

    @staticmethod
    def _assert_payload_absent_from_context(session: AgentSession, messages: list[Any]) -> None:
        """The custom-channel payload is NOT a backplane: no trace on log or context."""
        entries_blob = json.dumps(session.session_log.entries(), default=str)
        assert _PAYLOAD_MARKER not in entries_blob, "payload leaked onto the session log"

        messages_blob = json.dumps(
            [m if isinstance(m, dict) else getattr(m, "model_dump", lambda: str(m))() for m in messages],
            default=str,
        )
        assert _PAYLOAD_MARKER not in messages_blob, "payload leaked into the model context"
