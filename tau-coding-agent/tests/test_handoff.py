"""Smoke test for ``examples/40_handoff.py`` — τ-native flagship (S63 row 2).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S63. No pi original — this is the
τ-native demo the roadmap names (pi needs ``newSession`` + custom UI; τ does it
in two tree ops).

``/handoff`` runs ``ctx.summarize_branch`` (root → whole-history summary,
mutating the SOURCE session) then ``ctx.fork(mode="export")`` (copies the
now-condensed session into a NEW file, source untouched by the export itself).
Requires a file-backed ``Session`` (``fork(mode="export")`` Fail-Early raises
on an in-memory log — see ``23_context_surgeon``'s equivalent test), so this
lives alongside ``test_context_surgeon.py`` rather than in ``tau-agent-core``.

Layers:

* the command runs through ``AgentSession.run_extension_command`` directly (a
  ``register_command`` handler runs standalone, not under the live agent loop
  — no need to fake the provider network boundary at all, only the branch
  summarizer LLM call);
* **reload-invariance**: the exported file is re-loaded from disk via a fresh
  ``Session.load`` + ``ConversationTree`` fold, and the summary text on the
  new file's active path is asserted byte-identical to what the command
  reported — the handoff file is not a snapshot-in-RAM, it is durable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.conversation_tree import ConversationTree

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


# ── load the example module (its filename is not a valid identifier) ─────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HANDOFF_PATH = _REPO_ROOT / "examples" / "40_handoff.py"
_spec = importlib.util.spec_from_file_location("handoff_example", _HANDOFF_PATH)
assert _spec is not None and _spec.loader is not None
handoff = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = handoff
_spec.loader.exec_module(handoff)


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _summary_response(text: str):
    async def _impl(model, context, options=None):
        from tau_ai.types import AssistantMessage, TextContent

        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",  # type: ignore[arg-type]
            timestamp=0,
        )

    return _impl


@pytest.fixture
def isolate_tau_dir(tmp_path, monkeypatch):
    """Point the default sessions base at tmp so an export fork writes there."""
    monkeypatch.setattr("tau_coding_agent.session_store.TAU_DIR", tmp_path / "tau")
    return tmp_path


def _file_session(tmp_path) -> tuple[AgentSession, Session]:
    live = Session.create(str(tmp_path), "gpt-4o", "openai", base_dir=tmp_path / "tau" / "sessions")
    agent = AgentSession(
        session_log=live,
        model=_model(),
        extensions=[handoff.handoff_extension],
        compaction_settings=CompactionSettings(enabled=False),
    )
    return agent, live


def _reported_new_path(output: str) -> str:
    prefix = "Handoff session created: "
    assert output.startswith(prefix)
    return output[len(prefix) :].split("\n", 1)[0].strip()


def _text_of(messages: list[Any]) -> str:
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
    return "\n".join(out)


async def test_handoff_reports_new_session_and_summary(monkeypatch, isolate_tau_dir) -> None:
    monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("HANDOFF-SUMMARY"))
    tmp_path = isolate_tau_dir
    agent, live = _file_session(tmp_path)
    live.append_message(_msg("user", "let's refactor auth"))
    live.append_message(_msg("assistant", "sure, starting now"))
    live.append_message(_msg("user", "use JWT"))
    live.append_message(_msg("assistant", "done, switched to JWT"))

    result = await agent.run_extension_command("handoff", "focus on the auth decisions")

    assert result.handled is True
    output = result.output
    assert isinstance(output, str)
    assert "Handoff session created:" in output
    assert "HANDOFF-SUMMARY" in output
    assert "tau -p --session" in output

    new_path = _reported_new_path(output)
    assert Path(new_path).exists()
    assert new_path != str(live.path)


async def test_handoff_condenses_the_source_session(monkeypatch, isolate_tau_dir) -> None:
    """The SOURCE session's active path collapses to the summary (summarize_branch
    mutates the live log — the same tradeoff ``ctx.compact`` makes)."""
    monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("HANDOFF-SUMMARY"))
    tmp_path = isolate_tau_dir
    agent, live = _file_session(tmp_path)
    live.append_message(_msg("user", "u0"))
    live.append_message(_msg("assistant", "a0"))

    await agent.run_extension_command("handoff", "")

    branch_summaries = [e for e in live.entries() if e["type"] == "branch_summary"]
    assert len(branch_summaries) == 1
    assert "HANDOFF-SUMMARY" in branch_summaries[0]["summary"]
    # The active path no longer carries the raw prior turns.
    active = ConversationTree(live.entries(), live.cursor).context_for()
    assert "u0" not in _text_of(active)


async def test_handoff_new_session_survives_reload(monkeypatch, isolate_tau_dir) -> None:
    """Reload-invariance: the exported file's summary is durable, not RAM-only."""
    monkeypatch.setattr("tau_ai.client.complete_simple", _summary_response("HANDOFF-SUMMARY"))
    tmp_path = isolate_tau_dir
    agent, live = _file_session(tmp_path)
    live.append_message(_msg("user", "u0"))
    live.append_message(_msg("assistant", "a0"))

    result = await agent.run_extension_command("handoff", "")
    new_path = _reported_new_path(result.output)

    # Fresh load — a new Session/ConversationTree, no shared in-memory state
    # with the object the command mutated.
    reloaded_session = Session.load(Path(new_path))
    reloaded_tree = ConversationTree(reloaded_session.entries(), reloaded_session.cursor)
    reloaded_active = reloaded_tree.context_for()
    assert "HANDOFF-SUMMARY" in _text_of(reloaded_active)


async def test_handoff_with_no_history_reports_and_does_not_crash(isolate_tau_dir) -> None:
    tmp_path = isolate_tau_dir
    agent, _live = _file_session(tmp_path)

    result = await agent.run_extension_command("handoff", "")

    assert result.handled is True
    assert result.output == "Nothing to hand off yet — start a conversation first."
