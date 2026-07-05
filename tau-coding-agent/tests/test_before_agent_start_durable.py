"""S29 — ``before_agent_start`` injected messages become DURABLE tree nodes.

E5 §3.1 / §1.1. Before S29 a ``before_agent_start`` hook's ``message`` reached the
model but was never persisted (``agent_session.py:419-421``), so a reload forked
history (two histories: the one the model saw vs. the one on disk). S29 persists
each injected message as a ``customMessage`` tree node carrying ``role: "custom"``
(so the TUI / tree browser render it as extension-injected, NOT a literal user
turn) while the WIRE serializes it to an LLM-acceptable ``user`` role (pi
messages.ts custom→user).

This is the proving test for all four Verify criteria, end-to-end through the real
on-disk ``Session`` (which IS the ``AgentSession`` SessionLog on the live path);
only the network boundary (``agent_loop.stream_simple``) is faked, and the fake
captures the exact wire messages the provider would receive:

  (a) the injected message appears as a NODE in the session tree;
  (b) it appears in the emitted transcript (``prompt()``'s return);
  (c) it survives a session reload byte-identically (no second history);
  (d) it serializes to a wire role the LLM accepts (``user``, not ``custom``) —
      proven by pushing the reloaded node through the real provider conversion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.providers.openai import OpenAICompletionsProvider
from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree, TreeNode
from tau_agent_core.messages import convert_to_llm
from tau_coding_agent.session_store import Session


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


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
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


def _capturing_stream(captured: dict[str, Any]):
    """Fake stream_simple that records the wire messages then answers with text."""

    async def fake(model, context, options=None):
        captured["messages"] = context["messages"]
        final = _assistant("ok")
        return _Stream(
            [
                TextDeltaEvent(delta="ok", partial=final),
                DoneEvent(final=final, usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2)),
            ]
        )

    return fake


def _flatten(roots: list[TreeNode]) -> list[TreeNode]:
    out: list[TreeNode] = []
    stack = list(roots)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else getattr(message, "role", None)


def _texts_for_role(messages: list[Any], role: str) -> list[str]:
    out: list[str] = []
    for m in messages:
        if _role(m) != role:
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if btype == "text" and text is not None:
                out.append(text)
    return out


def _injecting_extension(api: Any) -> None:
    """A file-extension-shaped factory: inject one custom message per turn."""

    def hook(event: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"message": {"customType": "reminder", "content": "INJECTED"}}

    api.on("before_agent_start", hook)


async def test_before_agent_start_message_is_a_durable_node(tmp_path) -> None:
    # A real ON-DISK session (so we can reload it from bytes below) is the
    # AgentSession's SessionLog on the live path.
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(
        session_log=store,
        model=_model(),
        system_prompt="SYS",
        extensions=[_injecting_extension],
    )

    captured: dict[str, Any] = {}
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream(captured),
    ):
        returned = await session.prompt("hello")

    # ── (d) WIRE — the message the provider actually received is custom→user ──
    # The captured wire messages are post-serialization (agent_loop convert_to_llm),
    # so the injected text rides on a `user` message and NO `custom` role leaks to
    # the provider (which would reject it).
    wire = captured["messages"]
    assert "custom" not in [_role(m) for m in wire]
    assert "INJECTED" in _texts_for_role(wire, "user")
    # It rode AFTER the real user turn, in injection order (pi [user, ...custom]).
    assert _texts_for_role(wire, "user") == ["hello", "INJECTED"]

    # ── (a) NODE — the injection is a customMessage node with role "custom" ──
    entries = store.entries()
    custom_entries = [e for e in entries if e.get("type") == "customMessage"]
    assert len(custom_entries) == 1
    node_entry = custom_entries[0]
    assert node_entry["customType"] == "reminder"
    assert node_entry["message"]["role"] == "custom"
    assert node_entry["message"]["content"] == [{"type": "text", "text": "INJECTED"}]
    # It sits on the active path (parentId chain), between the user turn and the
    # assistant reply — a real node, not an out-of-band channel.
    tree = ConversationTree(entries, store.cursor)
    custom_nodes = [n for n in _flatten(tree.tree()) if n.kind == "customMessage"]
    assert len(custom_nodes) == 1
    assert custom_nodes[0].role == "custom"  # tree browser tags it distinctly
    assert custom_nodes[0].preview == "INJECTED"

    # ── (b) TRANSCRIPT — the durable node is in prompt()'s returned messages ──
    transcript_customs = [m for m in returned if _role(m) == "custom"]
    assert len(transcript_customs) == 1
    assert transcript_customs[0]["customType"] == "reminder"
    assert _texts_for_role(returned, "custom") == ["INJECTED"]

    # ── (c) RELOAD — byte-identical, no second history ───────────────────────
    before_entries = store.entries()
    before_ctx = ConversationTree(before_entries, store.cursor).context_for()
    reloaded = Session.load(store.path)
    # The raw entries round-trip through the JSONL bytes unchanged.
    assert reloaded.entries() == before_entries
    # Exactly ONE custom node after reload — the fork this step closes would show
    # up as a duplicate (model-saw copy + disk copy) or a missing node.
    assert sum(1 for e in reloaded.entries() if e.get("type") == "customMessage") == 1
    after_ctx = ConversationTree(reloaded.entries(), reloaded.cursor).context_for()
    assert after_ctx == before_ctx
    # The reconstructed context keeps the extension-origin role (render view)…
    assert "custom" in [_role(m) for m in after_ctx]

    # ── (d, reload) the RELOADED node still serializes to an accepted role ────
    # Push the reloaded active path through the same custom→user wire mapping and
    # then the REAL provider conversion: every role is OpenAI-acceptable (no
    # "custom"), and the injected text survives as a user message.
    wire_after_reload = convert_to_llm(after_ctx)
    assert "custom" not in [_role(m) for m in wire_after_reload]
    provider = OpenAICompletionsProvider(api_key="test-key")
    openai_messages = provider._convert_messages_to_openai(wire_after_reload)
    assert all(m["role"] in ("system", "user", "assistant", "tool") for m in openai_messages)
    injected = [
        m
        for m in openai_messages
        if m["role"] == "user"
        and "INJECTED" in "".join(
            b.get("text", "") for b in (m["content"] if isinstance(m["content"], list) else [])
        )
    ]
    assert len(injected) == 1


async def test_missing_custom_type_raises(tmp_path) -> None:
    """Fail-Early: a before_agent_start message without ``customType`` raises —
    the extension-origin identity is required, never fabricated."""
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)

    def bad_ext(api: Any) -> None:
        api.on("before_agent_start", lambda event, ctx: {"message": {"content": "no type"}})

    session = AgentSession(session_log=store, model=_model(), extensions=[bad_ext])

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_capturing_stream({}),
    ):
        with pytest.raises(ValueError, match="customType"):
            await session.prompt("hello")
