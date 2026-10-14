"""Probe: does the prompt cache survive a deferred-tool load and a mask?

Runs real completions against the ``haiku-4.5`` entry of the user's τ config
(an Anthropic model, automatic ``cache_control``). It never prints the key.

1. Threshold: two identical one-turn requests per prefix size; whether the
   second reads the cache shows the minimum cacheable prefix.
2. Cache-safe behaviour (as built): a long system prompt, then a turn that
   finds ``word_stats`` with ``tool_search`` and calls it through ``call_tool``,
   then a mask of ``bash`` by ``set_tool_exposure``, then a plain turn.
3. Old behaviour: the same steps with the declared set changed, through
   ``redeclare_tools``, which is what loading or masking did on the first branch.

Per completion it prints input, cache-read and cache-write tokens.

Run from the repo root: ``venv/bin/python experiments/tool-exposure/probe_cache.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from tau_coding_agent.backends import build_model_from_config

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.sdk import _resolve_tools
from tau_agent_core.session_log import InMemorySessionLog

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "haiku-4.5"


def _entry() -> dict[str, Any]:
    config = json.loads((Path.home() / ".tau" / "config.json").read_text())
    return dict(config["models"][ENTRY])


def _system_prompt(rules: int) -> str:
    lines = ["You are a terse assistant. Follow the numbered house rules below."]
    for i in range(1, rules + 1):
        lines.append(
            f"Rule {i}: when a request touches topic area {i}, answer in plain words, "
            f"cite the rule number {i}, and never invent facts about area {i}."
        )
    return "\n".join(lines)


def _ext62() -> Any:
    spec = importlib.util.spec_from_file_location("ext62", ROOT / "examples/62_tool_exposure.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.register


def _session(system_prompt: str, entry: dict[str, Any]) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=build_model_from_config(entry),
        api_key=entry.get("api_key"),
        system_prompt=system_prompt,
        tools=_resolve_tools(["read", "bash", "grep", "ls"], cwd=str(ROOT)),
        extensions=[_ext62()],
        cwd=str(ROOT),
    )


def _usage_rows(label: str, messages: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            u = m.get("usage") or {}
            rows.append(
                {
                    "step": label,
                    "input": u.get("input_tokens", 0),
                    "cache_read": u.get("cache_read_tokens", 0),
                    "cache_write": u.get("cache_write_tokens", 0),
                    "tools_called": [
                        b.get("name") for b in m.get("content", []) if b.get("type") == "toolCall"
                    ],
                }
            )
    return rows


async def threshold(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Two identical requests per prefix size; the second shows whether it cached."""
    out = []
    for rules in (60, 90, 130):
        session = _session(_system_prompt(rules), entry)
        first = _usage_rows(f"rules={rules} #1", await session.prompt("Reply with exactly: ok"))
        again = _session(_system_prompt(rules), entry)
        second = _usage_rows(f"rules={rules} #2", await again.prompt("Reply with exactly: ok"))
        out += first + second
    return out


async def scenario(entry: dict[str, Any], *, cache_safe: bool) -> list[dict[str, Any]]:
    """Warm, load a deferred tool, mask bash, plain turn: per-completion cache usage."""
    tag = "new" if cache_safe else "old"
    session = _session(_system_prompt(260), entry)
    rows = _usage_rows(f"{tag}: warm", await session.prompt("Reply with exactly: ready"))
    if cache_safe:
        load = (
            "You have no word-statistics tool listed. Use tool_search with the query "
            "'word statistics', then call the tool it finds through call_tool on the text "
            "'the cat saw the other cat'. Reply with only the unique-word count."
        )
        rows += _usage_rows(f"{tag}: load", await session.prompt(load))
        await session.set_tool_exposure("bash", "hidden")
    else:
        change = await session.redeclare_tools({"word_stats": True})
        assert change.invalidates_cache
        load = (
            "Call word_stats on the text 'the cat saw the other cat'. "
            "Reply with only the unique-word count."
        )
        rows += _usage_rows(f"{tag}: load", await session.prompt(load))
        await session.redeclare_tools({"bash": False})
    rows += _usage_rows(f"{tag}: after mask", await session.prompt("Reply with exactly: ok"))
    return rows


async def main() -> None:
    entry = _entry()
    report = {
        "threshold": await threshold(entry),
        "cache_safe": await scenario(entry, cache_safe=True),
        "old_behaviour": await scenario(entry, cache_safe=False),
    }
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
