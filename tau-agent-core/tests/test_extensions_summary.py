"""E5 §5 (S34) — ``summarize_extensions`` per-extension name/path/tools/commands/hooks.

The ``/extensions`` palette surface reads a populated ``LoadExtensionsResult`` and
must report, PER extension, what it registered. The registry stores tools/commands
globally (by name) with no per-extension attribution, so the accessor reads each
extension's own runner bucket (labelled by file path) — the one place recording
which extension owns which tool/command/hook.

These tests load REAL file extensions through ``AgentSession.load_extensions`` (the
live-bind seam) so the attribution is proven end-to-end, not against a hand-built
struct: a regression that drops per-extension attribution FAILS here.

Reference: docs/EXTENSIONS-E5-WIRING.md §5, S34.
"""

from __future__ import annotations

from pathlib import Path

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.sdk import summarize_extensions
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


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


# An extension that registers a tool, a command, and TWO hooks — every field the
# /extensions listing shows for one extension, so the accessor can be proven whole.
_FULL_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    api.register_tool({
        "name": "probe",
        "description": "a probe tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
    api.register_command("hello", {"description": "say hi"})
    api.on("tool_result", lambda event, ctx: None)
    api.on("tool_call", lambda event, ctx: None)
"""

# A second, differently-shaped extension: proves attribution is per-extension (its
# tool/command/hook must NOT leak into the first extension's summary).
_HOOK_ONLY_EXT = """
def register(api):
    api.on("before_agent_start", lambda event, ctx: None)
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


async def test_summarize_reports_name_path_tools_commands_hooks(tmp_path):
    """One loaded extension → its name, path, tools, commands, and hooks."""
    ext = _write(tmp_path / "full_ext.py", _FULL_EXT)
    session = _make_session()

    result = await session.load_extensions([str(ext)], discover=False)
    infos = summarize_extensions(result)

    assert len(infos) == 1
    info = infos[0]
    assert info.name == "full_ext"
    assert info.path == str(ext)
    assert info.tools == ["probe"]
    assert info.commands == ["hello"]
    # Hooks are sorted for a stable listing.
    assert info.hooks == ["tool_call", "tool_result"]


async def test_summarize_attributes_per_extension(tmp_path):
    """Two extensions → each summary carries ONLY its own registrations."""
    full = _write(tmp_path / "full_ext.py", _FULL_EXT)
    hook_only = _write(tmp_path / "hook_ext.py", _HOOK_ONLY_EXT)
    session = _make_session()

    result = await session.load_extensions([str(full), str(hook_only)], discover=False)
    infos = {i.name: i for i in summarize_extensions(result)}

    assert set(infos) == {"full_ext", "hook_ext"}
    # The hook-only extension contributed nothing else — no bleed from full_ext.
    assert infos["hook_ext"].tools == []
    assert infos["hook_ext"].commands == []
    assert infos["hook_ext"].hooks == ["before_agent_start"]
    # And full_ext keeps its own registrations.
    assert infos["full_ext"].tools == ["probe"]


async def test_summarize_empty_result_is_empty_list(tmp_path):
    """No extensions loaded → an empty summary (errors live on the result itself)."""
    session = _make_session()
    result = await session.load_extensions(None, discover=False)
    assert summarize_extensions(result) == []
