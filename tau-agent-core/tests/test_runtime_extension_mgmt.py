"""E10 §6 (S70) — runtime extension management: enable / disable / reload.

Lifts the D-E5-6 read-only stance on ``/extensions``. A loaded file extension can
be torn down and detached at runtime (its hooks stop firing), brought back, or
re-imported from disk — each using the S41 ``session_shutdown`` / ``session_start``
lifecycle hooks for clean teardown / bring-up.

This suite pins the agent-core seam directly on ``AgentSession`` (the frontends
just forward to it):
  * disable removes the runner bucket so a hook STOPS firing, unwinds the
    extension's registry tools/commands, and fires ``session_shutdown`` first;
  * enable re-invokes ``register`` (hook fires again, registry restored) and fires
    ``session_start``;
  * reload RE-IMPORTS the file (on-disk edits take effect) with a teardown +
    bring-up around it;
  * a bad target is a reportable no-op (``ok=False``), a broken reload RAISES
    (Fail-Early);
  * the management state is runtime-only (never persisted onto the tree), so the
    tree-as-truth invariant is untouched.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §6 S70.
"""

from __future__ import annotations

from pathlib import Path

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="gpt-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://example.invalid/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


# A file extension that: registers a ``probe`` tool + a ``pcmd`` command, edits every
# tool_result (appends _MARK), and records the reason it was torn down / brought up.
# The three tokens are substituted per test (no str.format — the body is full of
# dict braces) so reload can point the SAME path at a different _MARK.
_EXT_TEMPLATE = '''
import pathlib

_SHUTDOWN = pathlib.Path("__SHUTDOWN__")
_START = pathlib.Path("__START__")
_MARK = "__MARK__"


async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "raw"}]}


def register(api):
    api.register_tool({
        "name": "probe",
        "description": "probe",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
    api.register_command("pcmd", {"description": "probe cmd", "handler": lambda a, c: None})

    def on_tool_result(event, ctx):
        content = event.get("content") or []
        text = content[0].get("text", "") if content else ""
        return {"content": [{"type": "text", "text": text + _MARK}]}

    api.on("tool_result", on_tool_result)
    api.on("session_shutdown", lambda e, c: _SHUTDOWN.write_text(str(e.get("reason", ""))))
    api.on("session_start", lambda e, c: _START.write_text(str(e.get("reason", ""))))
'''


def _write_ext(path: Path, shutdown: Path, start: Path, mark: str) -> Path:
    src = (
        _EXT_TEMPLATE.replace("__SHUTDOWN__", str(shutdown))
        .replace("__START__", str(start))
        .replace("__MARK__", mark)
    )
    path.write_text(src)
    return path


async def _fire_tool_result(session: AgentSession) -> str | None:
    """Fire the tool_result hook and return the resulting text (``None`` if no hook)."""
    patched = await session._extension_runner.emit_tool_result(
        {"type": "tool_result", "content": [{"type": "text", "text": "base"}]}
    )
    if patched is None:
        return None
    return patched["content"][0]["text"]


# ── disable ───────────────────────────────────────────────────────────────────


async def test_disable_stops_hook_firing(tmp_path):
    """After disable the tool_result hook no longer fires (bucket detached)."""
    ext = _write_ext(
        tmp_path / "probe_ext.py", tmp_path / "down.txt", tmp_path / "up.txt", " +MARK"
    )
    session = _session()
    await session.load_extensions([str(ext)], discover=False)

    assert session._extension_runner.has_handlers("tool_result") is True
    assert await _fire_tool_result(session) == "base +MARK"

    result = await session.disable_extension("probe_ext")

    assert result.ok is True
    assert result.action == "disable"
    assert session._extension_runner.has_handlers("tool_result") is False
    assert await _fire_tool_result(session) is None


async def test_disable_fires_session_shutdown_teardown(tmp_path):
    """Disable fires the extension's ``session_shutdown`` BEFORE detaching it (S41)."""
    down = tmp_path / "down.txt"
    ext = _write_ext(tmp_path / "probe_ext.py", down, tmp_path / "up.txt", " +MARK")
    session = _session()
    await session.load_extensions([str(ext)], discover=False)

    assert not down.exists()
    await session.disable_extension("probe_ext")
    assert down.read_text() == "disable"


async def test_disable_unregisters_tools_and_commands(tmp_path):
    """Disable unwinds the extension's registry tools + commands."""
    ext = _write_ext(
        tmp_path / "probe_ext.py", tmp_path / "down.txt", tmp_path / "up.txt", " +MARK"
    )
    session = _session()
    await session.load_extensions([str(ext)], discover=False)

    assert "probe" in session._registry.get_active_tools()
    assert "pcmd" in session._registry.get_commands()

    await session.disable_extension("probe_ext")

    assert "probe" not in session._registry.get_active_tools()
    assert "pcmd" not in session._registry.get_commands()


# ── enable ────────────────────────────────────────────────────────────────────


async def test_enable_rebinds_hook_and_registry(tmp_path):
    """Enable re-invokes register: the hook fires again + tools/commands return."""
    up = tmp_path / "up.txt"
    ext = _write_ext(tmp_path / "probe_ext.py", tmp_path / "down.txt", up, " +MARK")
    session = _session()
    await session.load_extensions([str(ext)], discover=False)
    await session.disable_extension("probe_ext")

    result = await session.enable_extension("probe_ext")

    assert result.ok is True
    assert session._extension_runner.has_handlers("tool_result") is True
    assert await _fire_tool_result(session) == "base +MARK"
    assert "probe" in session._registry.get_active_tools()
    assert "pcmd" in session._registry.get_commands()
    # Bring-up fired session_start with reason "enable".
    assert up.read_text() == "enable"


# ── reload ────────────────────────────────────────────────────────────────────


async def test_reload_reimports_fresh_source(tmp_path):
    """Reload re-imports the file so an on-disk edit takes effect."""
    down = tmp_path / "down.txt"
    up = tmp_path / "up.txt"
    path = tmp_path / "probe_ext.py"
    _write_ext(path, down, up, " +V1")
    session = _session()
    await session.load_extensions([str(path)], discover=False)
    assert await _fire_tool_result(session) == "base +V1"

    # Edit the file on disk, then reload — the new marker must take effect.
    _write_ext(path, down, up, " +V2")
    result = await session.reload_extension("probe_ext")

    assert result.ok is True
    assert await _fire_tool_result(session) == "base +V2"
    # Teardown + bring-up both ran with reason "reload".
    assert down.read_text() == "reload"
    assert up.read_text() == "reload"


async def test_reload_broken_file_raises_and_leaves_torn_down(tmp_path):
    """A broken reload propagates (Fail-Early); the extension stays torn down."""
    path = tmp_path / "probe_ext.py"
    _write_ext(path, tmp_path / "down.txt", tmp_path / "up.txt", " +MARK")
    session = _session()
    await session.load_extensions([str(path)], discover=False)

    path.write_text("raise RuntimeError('boom on reload')\n")
    try:
        await session.reload_extension("probe_ext")
    except RuntimeError as exc:
        assert "boom on reload" in str(exc)
    else:
        raise AssertionError("reload of a broken file should raise")

    # Torn down: the old hook is gone and nothing re-bound.
    assert session._extension_runner.has_handlers("tool_result") is False
    assert await _fire_tool_result(session) is None


# ── target resolution + no-op outcomes ────────────────────────────────────────


async def test_resolve_by_stem_and_full_path(tmp_path):
    """A disable target resolves by file stem OR by the full loaded path."""
    path = tmp_path / "probe_ext.py"
    _write_ext(path, tmp_path / "down.txt", tmp_path / "up.txt", " +MARK")
    session = _session()
    await session.load_extensions([str(path)], discover=False)

    # By full path.
    r1 = await session.disable_extension(str(path))
    assert r1.ok is True
    await session.enable_extension(str(path))
    # By stem.
    r2 = await session.disable_extension("probe_ext")
    assert r2.ok is True


async def test_unknown_target_is_reportable_noop(tmp_path):
    """An unknown target reports ``ok=False`` — no raise, no fabrication."""
    session = _session()
    result = await session.disable_extension("nope")
    assert result.ok is False
    assert "nope" in result.message


async def test_double_disable_and_double_enable_are_noops(tmp_path):
    """Disabling twice / enabling an enabled extension is a reported no-op."""
    path = tmp_path / "probe_ext.py"
    _write_ext(path, tmp_path / "down.txt", tmp_path / "up.txt", " +MARK")
    session = _session()
    await session.load_extensions([str(path)], discover=False)

    assert (await session.enable_extension("probe_ext")).ok is False  # already enabled
    assert (await session.disable_extension("probe_ext")).ok is True
    assert (await session.disable_extension("probe_ext")).ok is False  # already disabled


async def test_list_managed_reflects_enabled_disabled(tmp_path):
    """``list_managed_extensions`` reflects the live enabled/disabled state."""
    path = tmp_path / "probe_ext.py"
    _write_ext(path, tmp_path / "down.txt", tmp_path / "up.txt", " +MARK")
    session = _session()
    await session.load_extensions([str(path)], discover=False)

    assert session.list_managed_extensions() == [(str(path), True)]
    await session.disable_extension("probe_ext")
    assert session.list_managed_extensions() == [(str(path), False)]
    await session.enable_extension("probe_ext")
    assert session.list_managed_extensions() == [(str(path), True)]
