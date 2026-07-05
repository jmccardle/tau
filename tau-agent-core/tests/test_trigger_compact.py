"""Smoke test for ``examples/39_trigger_compact.py`` — self-managing context (S63).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S63 row 1. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/trigger-compact.ts``.

Two layers, mirroring ``test_budget.py`` / ``test_reminders.py``:

* **pure-unit** — the ``turn_end`` watcher's crossing-edge logic is exercised
  directly against a stub ``ctx`` (no full loop needed to prove "fires once on
  the crossing turn, not every turn while over, not while climbing toward it").
* **full-loop / reload-invariance** — ``/trigger-compact`` runs through a real
  ``AgentSession`` + the genuine compaction pipeline (only the LLM summarizer
  boundary, ``tau_agent_core.compaction.complete_simple``, is faked); the
  resulting ``compaction`` entry is asserted to survive a fresh fold over the
  raw log entries (à la S29's reload check).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from tau_ai.types import AssistantMessage, Model, TextContent

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog

# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRIGGER_COMPACT_PATH = _REPO_ROOT / "examples" / "39_trigger_compact.py"
_spec = importlib.util.spec_from_file_location("trigger_compact_example", _TRIGGER_COMPACT_PATH)
assert _spec is not None and _spec.loader is not None
trigger_compact = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = trigger_compact
_spec.loader.exec_module(trigger_compact)


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


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _summary_response(text: str):
    async def _impl(model, context, options=None):
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",  # type: ignore[arg-type]
            timestamp=0,
        )

    return _impl


# ── pure-unit: crossing-edge logic against a stub api/ctx ────────────────────


class _FakeAPI:
    """Captures ``api.on``/``api.register_command`` calls without a real session."""

    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.commands: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.hooks[event] = handler

    def register_command(self, name: str, command: dict) -> None:
        self.commands[name] = command


def _stub_ctx(usage_tokens: int | None) -> Any:
    notified: list[tuple[str, str]] = []
    ctx = SimpleNamespace(
        get_context_usage=Mock(
            return_value=(
                None
                if usage_tokens is None
                else {"tokens": usage_tokens, "context_window": 128000, "percent": 0.0}
            )
        ),
        compact=AsyncMock(return_value=None),
        ui=SimpleNamespace(notify=lambda msg, level="info": notified.append((msg, level))),
    )
    ctx._notified = notified  # type: ignore[attr-defined]
    return ctx


async def test_watcher_registers_turn_end_and_command() -> None:
    api = _FakeAPI()
    trigger_compact.trigger_compact_extension(api)
    assert "turn_end" in api.hooks
    assert "trigger-compact" in api.commands
    assert callable(api.commands["trigger-compact"]["handler"])


async def test_watcher_does_not_fire_while_climbing_toward_threshold() -> None:
    api = _FakeAPI()
    trigger_compact.trigger_compact_extension(api)
    on_turn_end = api.hooks["turn_end"]

    # Below the threshold on both the previous and current reading: no schedule.
    ctx = _stub_ctx(1000)
    await on_turn_end({"type": "turn_end", "turn_index": 0}, ctx)
    ctx.get_context_usage.return_value = {
        "tokens": 2000,
        "context_window": 128000,
        "percent": 0.0,
    }
    await on_turn_end({"type": "turn_end", "turn_index": 1}, ctx)
    ctx.compact.assert_not_awaited()


async def test_watcher_fires_exactly_once_on_the_crossing_turn() -> None:
    api = _FakeAPI()
    trigger_compact.trigger_compact_extension(api)
    on_turn_end = api.hooks["turn_end"]
    ctx = _stub_ctx(None)

    readings = [
        90_000,  # under
        100_000,  # under (== threshold is not "crossed" — the check is strict >)
        150_000,  # CROSSES here (previous <= threshold, current > threshold)
        200_000,  # still over, but already tripped — must NOT fire again
    ]
    for i, tokens in enumerate(readings):
        ctx.get_context_usage.return_value = {
            "tokens": tokens,
            "context_window": 128000,
            "percent": 0.0,
        }
        await on_turn_end({"type": "turn_end", "turn_index": i}, ctx)

    ctx.compact.assert_awaited_once_with(defer=True)


async def test_watcher_ignores_unknown_usage() -> None:
    """``get_context_usage() -> None`` (unknown context_window) is a no-op, not a crash."""
    api = _FakeAPI()
    trigger_compact.trigger_compact_extension(api)
    on_turn_end = api.hooks["turn_end"]
    ctx = _stub_ctx(None)

    await on_turn_end({"type": "turn_end", "turn_index": 0}, ctx)

    ctx.compact.assert_not_awaited()


# ── full-loop: ``/trigger-compact`` through a real session + reload check ────


async def test_trigger_compact_command_compacts_immediately_and_reports(monkeypatch) -> None:
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _summary_response("TRIGGER-COMPACT-SUMMARY"),
    )
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[trigger_compact.trigger_compact_extension],
        compaction_settings=CompactionSettings(enabled=True, keep_recent_tokens=1),
    )
    log = session.session_log
    for i in range(3):
        log.append_message(_msg("user", f"u{i}"))
        log.append_message(_msg("assistant", f"a{i}"))

    result = await session.run_extension_command("trigger-compact", "")

    assert result.handled is True
    assert isinstance(result.output, str)
    assert "Compacted" in result.output
    assert "TRIGGER-COMPACT-SUMMARY" in result.output

    compactions = [e for e in log.entries() if e["type"] == "compaction"]
    assert len(compactions) == 1

    # Reload-invariance: a fresh fold over the raw entries keeps the compaction
    # boundary — the summary the model would see on reload is unchanged.
    reloaded = ConversationTree(log.entries(), log.cursor).context_for()
    reloaded_texts = "".join(
        block.get("text", "")
        for m in reloaded
        if isinstance(m, dict)
        for block in (m.get("content") or [])
        if isinstance(block, dict)
    )
    assert "TRIGGER-COMPACT-SUMMARY" in reloaded_texts


async def test_trigger_compact_command_reports_nothing_to_compact() -> None:
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[trigger_compact.trigger_compact_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    with patch(
        "tau_agent_core.agent_session.AgentSession._perform_compaction",
        AsyncMock(return_value=None),
    ):
        result = await session.run_extension_command("trigger-compact", "")

    assert result.handled is True
    assert result.output == "Nothing to compact yet."
