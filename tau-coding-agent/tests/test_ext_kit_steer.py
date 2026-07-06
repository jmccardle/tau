"""Tests for ``examples/ext_kit/steer.py`` — the S58 *in-loop steering* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S58.

Three atoms, three proofs:

* **ReminderBank** — the generalized ``21_reminders`` bank: threshold trips,
  cooldown windows, the stable drain order, the ``patch_result`` durable edit, and
  a RELOAD-INVARIANCE proof that a drained ``<system-reminder>`` — appended to a
  ``tool_result`` node through the real loop — is present on the persisted tree and
  survives a fresh ``ConversationTree`` fold (the durable-hook invariant, E5 §3.3).
* **TurnDebouncer** — the turn-cadence gate: first fire passes, then suppressed for
  ``interval`` turns, per-key independence, and the ``interval < 1`` Fail-Early.
* **wrap_tool** — the pi *tool-override* pattern: ``before`` mutation, ``before``
  short-circuit (veto), ``after`` post-process, delegation to the real built-in,
  the unknown-name raise, and an end-to-end proof that the wrapped tool SHADOWS the
  built-in of the same name through ``_build_turn_tools`` with ``ctx`` bound.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools import ReadTool

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import steer  # noqa: E402  (path insertion must precede the import)


# ── ReminderBank: registration + Fail-Early ──────────────────────────────────


def test_reminderbank_rejects_duplicate_rule() -> None:
    bank = steer.ReminderBank()
    bank.add("scope", "keep it in scope")
    with pytest.raises(ValueError, match="already registered"):
        bank.add("scope", "again")


def test_reminderbank_rejects_bad_threshold_and_cooldown() -> None:
    bank = steer.ReminderBank()
    with pytest.raises(ValueError, match="threshold must be >= 1"):
        bank.add("r", "t", threshold=0)
    with pytest.raises(ValueError, match="cooldown must be >= 0"):
        bank.add("r2", "t", cooldown=-1)


def test_reminderbank_unknown_rule_raises() -> None:
    bank = steer.ReminderBank()
    with pytest.raises(ValueError, match="unknown rule 'nope'"):
        bank.trigger("nope")
    with pytest.raises(ValueError, match="unknown rule 'nope'"):
        bank.bump("nope")


# ── ReminderBank: threshold + cooldown state machine ─────────────────────────


def test_bump_trips_only_at_threshold() -> None:
    """A rule with threshold 2 needs two bumps to go pending (then resets)."""
    bank = steer.ReminderBank()
    bank.add("twice", "two failures", threshold=2)

    assert bank.bump("twice") is False  # 1 of 2
    assert bank.is_pending("twice") is False
    assert bank.bump("twice") is True  # 2 of 2 → trips
    assert bank.is_pending("twice") is True
    assert bank.drain() == ["twice"]


def test_reset_clears_the_counter() -> None:
    """A reset (e.g. on a success) rewinds progress toward the threshold."""
    bank = steer.ReminderBank()
    bank.add("twice", "streak", threshold=2)
    bank.bump("twice")
    bank.reset("twice")
    assert bank.bump("twice") is False  # back to 1 of 2, not tripped


def test_trigger_bypasses_threshold() -> None:
    bank = steer.ReminderBank()
    bank.add("hi", "immediate", threshold=5)
    bank.trigger("hi")
    assert bank.drain() == ["hi"]


def test_fires_once_then_cools_down() -> None:
    """A fired rule stays silent for exactly ``cooldown`` drains, then re-fires."""
    bank = steer.ReminderBank()
    bank.add("nag", "stop that", cooldown=3)

    bank.trigger("nag")
    assert bank.drain() == ["nag"]  # first fire arms the cooldown

    for _ in range(3):
        bank.trigger("nag")
        assert bank.drain() == []  # silent while cooling, even re-triggered

    bank.trigger("nag")
    assert bank.drain() == ["nag"]  # off cooldown → fires again


def test_drain_uses_registration_order() -> None:
    """Fired names come back in the order the rules were registered (stable)."""
    bank = steer.ReminderBank()
    bank.add("a", "A")
    bank.add("b", "B")
    bank.add("c", "C")
    bank.trigger("c")
    bank.trigger("a")
    assert bank.drain() == ["a", "c"]


def test_drain_empty_when_nothing_pending() -> None:
    bank = steer.ReminderBank()
    bank.add("a", "A")
    assert bank.drain() == []


# ── ReminderBank: render + patch_result ──────────────────────────────────────


def test_render_wraps_each_rule_in_a_system_reminder() -> None:
    bank = steer.ReminderBank()
    bank.add("a", "first body")
    bank.add("b", "second body")
    rendered = bank.render(["a", "b"])
    assert rendered == (
        "<system-reminder>first body</system-reminder>\n"
        "<system-reminder>second body</system-reminder>"
    )


def test_patch_result_appends_reminder_and_keeps_original_content() -> None:
    bank = steer.ReminderBank()
    bank.add("scope", "keep it in scope")
    bank.trigger("scope")

    event = {"content": [{"type": "text", "text": "tool output"}]}
    patch_ = bank.patch_result(event)
    assert patch_ is not None
    content = patch_["content"]
    # Original output survives beneath the appended nag.
    assert content[0] == {"type": "text", "text": "tool output"}
    assert content[1]["text"] == "<system-reminder>keep it in scope</system-reminder>"
    # The event dict is not mutated in place (a copy is returned).
    assert len(event["content"]) == 1


def test_patch_result_returns_none_when_nothing_fires() -> None:
    bank = steer.ReminderBank()
    bank.add("scope", "keep it in scope")
    # Nothing triggered → drain fires nothing → the result passes through untouched.
    assert bank.patch_result({"content": []}) is None


# ── TurnDebouncer ────────────────────────────────────────────────────────────


def test_debouncer_rejects_bad_interval() -> None:
    with pytest.raises(ValueError, match="interval must be >= 1"):
        steer.TurnDebouncer(0)


def test_debouncer_first_fire_passes_then_suppresses() -> None:
    deb = steer.TurnDebouncer(interval=3)
    assert deb.fire() is True  # first fire (turn 0) always passes
    assert deb.fire() is False  # same turn — suppressed

    deb.tick()  # turn 1
    assert deb.fire() is False  # only 1 turn since last fire (< 3)
    deb.tick()  # turn 2
    assert deb.fire() is False  # 2 turns (< 3)
    deb.tick()  # turn 3
    assert deb.fire() is True  # 3 turns since fire → fires again
    assert deb.fire() is False


def test_debouncer_keys_are_independent() -> None:
    deb = steer.TurnDebouncer(interval=2)
    assert deb.fire("budget") is True
    assert deb.fire("replan") is True  # different key, its own cadence
    assert deb.fire("budget") is False
    assert deb.fire("replan") is False


def test_debouncer_ready_matches_fire() -> None:
    deb = steer.TurnDebouncer(interval=2)
    assert deb.ready("x") is True
    deb.fire("x")
    assert deb.ready("x") is False
    deb.tick()
    deb.tick()
    assert deb.ready("x") is True


def test_debouncer_reset() -> None:
    deb = steer.TurnDebouncer(interval=5)
    deb.fire("x")
    assert deb.fire("x") is False
    deb.reset("x")
    assert deb.fire("x") is True  # forgotten → fires immediately again


def test_debouncer_tick_advances_turn_and_rejects_negative() -> None:
    deb = steer.TurnDebouncer(interval=1)
    assert deb.turn == 0
    assert deb.tick() == 1
    assert deb.tick(2) == 3
    assert deb.turn == 3
    with pytest.raises(ValueError, match="n must be >= 0"):
        deb.tick(-1)


# ── wrap_tool: pure/unit over the returned definition ────────────────────────


class _FakeTool:
    """A minimal built-in stand-in: records its calls, returns a canned result."""

    name = "read"
    label = "Read File"
    description = "read a file"
    parameters: dict[str, Any] = {"type": "object", "properties": {"path": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self, tool_call_id: str, args: dict, signal: Any = None, on_update: Any = None
    ) -> dict:
        self.calls.append(dict(args))
        return {
            "content": [{"type": "text", "text": f"read {args.get('path')}"}],
            "is_error": False,
        }


def test_wrap_tool_unknown_builtin_raises() -> None:
    with pytest.raises(ValueError, match="unknown built-in tool 'nope'"):
        steer.wrap_tool("nope")


def test_wrap_tool_definition_inherits_builtin_schema() -> None:
    fake = _FakeTool()
    defn = steer.wrap_tool("read", tool=fake)
    assert defn["name"] == "read"
    assert defn["label"] == "Read File"
    assert defn["description"] == "read a file"
    assert defn["parameters"] is fake.parameters
    assert callable(defn["execute"])


def test_wrap_tool_description_override() -> None:
    fake = _FakeTool()
    defn = steer.wrap_tool("read", tool=fake, description="read (audited)")
    assert defn["description"] == "read (audited)"


async def test_wrap_tool_delegates_to_original() -> None:
    fake = _FakeTool()
    defn = steer.wrap_tool("read", tool=fake)
    result = await defn["execute"]("c1", {"path": "/x"}, None, None, None)
    assert fake.calls == [{"path": "/x"}]
    assert result["content"][0]["text"] == "read /x"


async def test_wrap_tool_before_can_mutate_params() -> None:
    fake = _FakeTool()

    def before(params: dict, ctx: Any) -> None:
        params["path"] = "/rewritten"
        return None

    defn = steer.wrap_tool("read", before=before, tool=fake)
    await defn["execute"]("c1", {"path": "/orig"}, None, None, None)
    assert fake.calls == [{"path": "/rewritten"}]


async def test_wrap_tool_before_short_circuits() -> None:
    """A ``before`` returning a result vetoes: the original is never called."""
    fake = _FakeTool()

    def before(params: dict, ctx: Any) -> dict:
        return {"content": [{"type": "text", "text": "blocked"}], "is_error": True}

    defn = steer.wrap_tool("read", before=before, tool=fake)
    result = await defn["execute"]("c1", {"path": "/secret"}, None, None, None)
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "blocked"
    assert fake.calls == []  # original NOT invoked


async def test_wrap_tool_after_can_replace_result() -> None:
    fake = _FakeTool()

    def after(result: dict, params: dict, ctx: Any) -> dict:
        return {"content": [{"type": "text", "text": "redacted"}], "is_error": False}

    defn = steer.wrap_tool("read", after=after, tool=fake)
    result = await defn["execute"]("c1", {"path": "/x"}, None, None, None)
    assert result["content"][0]["text"] == "redacted"
    assert fake.calls == [{"path": "/x"}]  # original DID run


async def test_wrap_tool_after_passthrough_on_none() -> None:
    fake = _FakeTool()
    seen: list[Any] = []

    def after(result: dict, params: dict, ctx: Any) -> None:
        seen.append(result)
        return None

    defn = steer.wrap_tool("read", after=after, tool=fake)
    result = await defn["execute"]("c1", {"path": "/x"}, None, None, None)
    assert result["content"][0]["text"] == "read /x"  # original passed through
    assert len(seen) == 1


async def test_wrap_tool_resolves_real_builtin_and_reads_a_file(tmp_path) -> None:
    """No ``tool=`` → wrap_tool resolves the real ``ReadTool`` and delegates to it."""
    target = tmp_path / "hello.txt"
    target.write_text("line one\nline two\n")
    defn = steer.wrap_tool("read")
    result = await defn["execute"]("c1", {"path": str(target)}, None, None, None)
    blob = "\n".join(b.get("text", "") for b in result["content"])
    assert "line one" in blob
    assert "line two" in blob


# ── wrap_tool: end-to-end shadow through the session tool merge ───────────────


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


async def test_wrap_tool_shadows_builtin_with_ctx_bound(tmp_path) -> None:
    """The wrapped tool overrides the built-in of the same name in the turn tools.

    Register it via the real ``api.register_tool`` on a bound api, then resolve the
    turn tools: the extension ``read`` must win over the built-in ``read``, and its
    ``before`` hook must run with the live ``ExtensionContext`` bound as ``ctx``.
    """
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        tools=[ReadTool()],
        extensions=[],
    )
    api = session._bind_extension_api("examples/ext_kit/steer.py")

    seen_ctx: list[Any] = []

    def before(params: dict, ctx: Any) -> dict:
        seen_ctx.append(ctx)
        return {"content": [{"type": "text", "text": "vetoed"}], "is_error": True}

    api.register_tool(steer.wrap_tool("read", before=before, description="read (guarded)"))

    tools = session._build_turn_tools()
    reads = [t for t in tools if t.name == "read"]
    assert len(reads) == 1  # the shadow replaced the built-in (not two `read`s)
    assert reads[0].description == "read (guarded)"

    result = await reads[0].execute(tool_call_id="c1", args={"path": "/etc/passwd"}, signal=None)
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "vetoed"
    # The adapter bound the live ExtensionContext as ctx (not None).
    assert seen_ctx and seen_ctx[0] is api.context


# ── ReminderBank: RELOAD-INVARIANCE through the real loop ─────────────────────


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


class _Stream:
    """Minimal async stream matching the stream_simple contract."""

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


def _count_tool_results(messages: list[Any], tool_name: str) -> int:
    n = 0
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            n += 1
    return n


def _message_text_blob(messages: list[Any]) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    out.append(str(block.get("text", "")))
                else:
                    out.append(str(getattr(block, "text", "")))
    return "\n".join(out)


def _entry_message_text(entry: dict[str, Any]) -> str:
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


async def test_reminderbank_edit_is_durable_in_tree_and_reload() -> None:
    """S58 reload-invariance: a ``patch_result`` edit is a DURABLE tree node.

    Wire a generic ReminderBank as an extension: ``tool_call`` triggers a rule when a
    ``write`` lands, ``tool_result`` drains it via ``patch_result`` (the durable edit).
    The fake emits one ``write`` (an error, since ``write`` is unregistered) then stops,
    so there are two LLM calls; call 2 carries the ``<system-reminder>`` appended to
    call 1's ``tool_result``. The reminder must then appear on THREE surfaces the
    durable-hook invariant forces to agree: the wire transcript, the persisted tree
    entries, and a fresh ``ConversationTree`` fold (the reload).
    """
    bank = steer.ReminderBank()
    bank.add("tests-readonly", "Tests are read-only — change the implementation instead.")

    def on_tool_call(event: dict[str, Any], ctx: Any) -> None:
        if event.get("tool_name") == "write":
            bank.trigger("tests-readonly")
        return None

    def on_tool_result(event: dict[str, Any], ctx: Any) -> dict[str, Any] | None:
        return bank.patch_result(event)

    reminder_text = "Tests are read-only — change the implementation instead."

    wire_payloads: list[list[Any]] = []

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        wire_payloads.append(list(messages))
        if _count_tool_results(messages, "write") >= 1:
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", "write", {"path": "tests/test_x.py", "content": "x"})
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[])
    api = session._bind_extension_api("examples/ext_kit/steer.py")
    api.on("tool_call", on_tool_call)
    api.on("tool_result", on_tool_result)

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("edit the test until it passes")

    # ── surface 1: the wire transcript (call 2 carries the durable edit) ──
    assert len(wire_payloads) == 2
    assert reminder_text in _message_text_blob(wire_payloads[-1])

    # ── surface 2: the persisted tree (a toolResult message node) ──
    entries = session._session_log.entries()
    tool_result_entries = [
        e
        for e in entries
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "toolResult"
    ]
    assert tool_result_entries, "expected a persisted toolResult node"
    assert any(reminder_text in _entry_message_text(e) for e in tool_result_entries)

    # ── surface 3: a reload (fold a fresh tree over the persisted entries) ──
    reloaded = ConversationTree(session._session_log.entries(), session._session_log.cursor)
    assert reminder_text in _message_text_blob(reloaded.context_for())
