"""#43: ``--tools`` and ``--exclude-tools`` reach extension tools, checked after extensions load.

Reference: docs/EXTENSION-PACKAGES.md §11. In 0.13.0 ``--tools web_fetch -e
search.py`` raised ``Unknown tool: web_fetch`` before the extension had
registered anything, and ``--exclude-tools`` silently ignored extension tools.
"""

from __future__ import annotations

import pytest

from tau_coding_agent.backends import create_backend, resolve_tool_filters, resolve_tool_names

_SEARCH = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    for name in ("web_fetch", "web_search"):
        api.register_tool({
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
            "execute": _exec,
        })
"""


def _backend(tmp_path, **config):
    ext = tmp_path / "search.py"
    ext.write_text(_SEARCH)
    backend = create_backend(
        {"backend": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1", **config}
    )
    return backend, str(ext)


def _offered(backend) -> list[str]:
    return sorted(t.name for t in backend.agent_session._build_turn_tools())


async def test_an_extension_tool_can_be_allowlisted(tmp_path):
    backend, ext = _backend(tmp_path, tools=["web_fetch"])
    await backend.load_extensions([ext], discover=False)
    assert _offered(backend) == ["web_fetch"]


async def test_built_ins_and_extension_tools_share_one_allowlist(tmp_path):
    backend, ext = _backend(tmp_path, tools=["read", "web_fetch"])
    await backend.load_extensions([ext], discover=False)
    assert _offered(backend) == ["read", "web_fetch"]


async def test_a_name_nothing_registers_still_raises_after_loading(tmp_path):
    backend, ext = _backend(tmp_path, tools=["web_fetch", "web_fecth"])
    with pytest.raises(ValueError, match="Unknown tool: web_fecth"):
        await backend.load_extensions([ext], discover=False)


async def test_exclude_tools_reaches_extension_tools(tmp_path):
    backend, ext = _backend(tmp_path, exclude_tools=["web_search", "bash"])
    await backend.load_extensions([ext], discover=False)
    offered = _offered(backend)
    assert "web_fetch" in offered and "web_search" not in offered and "bash" not in offered


async def test_an_unknown_exclusion_raises_too(tmp_path):
    backend, ext = _backend(tmp_path, exclude_tools=["web_serch"])
    with pytest.raises(ValueError, match="Unknown tool: web_serch"):
        await backend.load_extensions([ext], discover=False)


def test_the_built_in_resolution_skips_extension_names_and_suppression_drops_the_allowlist():
    assert resolve_tool_names({"tools": ["read", "web_fetch"]}) == ["read"]
    assert resolve_tool_filters({"tools": [], "no_tools": "builtin"}) == (None, [])
    assert resolve_tool_filters({}) == (None, [])
