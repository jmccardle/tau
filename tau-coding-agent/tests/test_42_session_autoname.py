"""Tests for ``examples/42_session_autoname.py`` — ambient metadata (S64 row 2).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §5 S64. Pi original:
``~/Development/pi/packages/coding-agent/examples/extensions/session-name.ts``.

Proves:

* the ported ``/session-name [name]`` command: set with an argument, show the
  current value (or the "No session name set" fallback) with none — same
  usage/messages as pi's original;
* the ADDED ``message_end`` observer auto-names the session from the first
  user message the first time an assistant turn completes, and never
  overwrites a name that already exists (manual or auto);
* naming is durable via ``ExtensionAPI.set_session_name`` ->
  ``Session.append_session_info`` (S64's fix to the previously-dead
  ``_session_name``-attribute no-op) and RELOAD-INVARIANT: a freshly loaded
  on-disk ``Session`` reports the same ``.name``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_ai.types import Model

from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "42_session_autoname.py"
_spec = importlib.util.spec_from_file_location("session_autoname_42_example", _PATH)
assert _spec is not None and _spec.loader is not None
autoname_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = autoname_mod
_spec.loader.exec_module(autoname_mod)


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


def _session_with_autoname(tmp_path: Path) -> tuple[AgentSession, Session]:
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    autoname_mod.session_autoname_extension(
        agent._bind_extension_api("examples/42_session_autoname.py")
    )
    return agent, live


async def _emit_message_end(agent: AgentSession, message: dict) -> None:
    await agent._events.emit(AgentEvent(type="message_end", timestamp=0, message=message))


# ── the ported manual command ────────────────────────────────────────────────


async def test_command_registered(tmp_path):
    agent, _live = _session_with_autoname(tmp_path)
    assert agent._registry.get_command("session-name") is not None


async def test_show_with_no_name_set(tmp_path):
    agent, _live = _session_with_autoname(tmp_path)
    result = await agent.run_extension_command("session-name", "")
    assert result.output == "No session name set"


async def test_set_then_show(tmp_path):
    agent, _live = _session_with_autoname(tmp_path)
    set_result = await agent.run_extension_command("session-name", "My Session")
    assert set_result.output == "Session named: My Session"

    show_result = await agent.run_extension_command("session-name", "")
    assert show_result.output == "Session: My Session"


# ── the added ambient auto-name observer ────────────────────────────────────


async def test_assistant_message_end_auto_names_from_first_user_message(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    live.append_message(_msg("user", "let's refactor the auth module"))
    live.append_message(_msg("assistant", "sure, starting now"))

    await _emit_message_end(agent, {"role": "assistant", "content": []})

    assert live.name == "let's refactor the auth module"


async def test_auto_name_truncates_long_first_message(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    long_text = "x" * 80
    live.append_message(_msg("user", long_text))

    await _emit_message_end(agent, {"role": "assistant", "content": []})

    assert live.name == "x" * 50 + "..."


async def test_auto_name_does_not_overwrite_an_existing_manual_name(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    await agent.run_extension_command("session-name", "Custom Name")
    live.append_message(_msg("user", "hello there"))

    await _emit_message_end(agent, {"role": "assistant", "content": []})

    assert live.name == "Custom Name"


async def test_auto_name_fires_only_once(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    live.append_message(_msg("user", "first topic"))
    await _emit_message_end(agent, {"role": "assistant", "content": []})
    assert live.name == "first topic"

    live.append_message(_msg("user", "second topic, unrelated"))
    await _emit_message_end(agent, {"role": "assistant", "content": []})
    assert live.name == "first topic"


async def test_non_assistant_or_missing_message_events_are_ignored(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    live.append_message(_msg("user", "hello"))

    await agent._events.emit(AgentEvent(type="message_end", timestamp=0, message=None))
    await _emit_message_end(agent, {"role": "user", "content": []})

    assert live.name is None


async def test_no_user_message_yet_does_not_crash(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    await _emit_message_end(agent, {"role": "assistant", "content": []})
    assert live.name is None


# ── durability / reload-invariance ──────────────────────────────────────────


async def test_auto_named_session_survives_reload(tmp_path):
    agent, live = _session_with_autoname(tmp_path)
    live.append_message(_msg("user", "the reload test topic"))
    await _emit_message_end(agent, {"role": "assistant", "content": []})
    session_path = live.path
    assert session_path is not None

    reloaded = Session.load(session_path)
    assert reloaded.name == "the reload test topic"
