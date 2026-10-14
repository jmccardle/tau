"""Render code mode's prompt for every provider × lifetime against τ's built-in tools.

    venv/bin/python experiments/sandbox-providers/render_prompts.py > rendered.md
"""

from __future__ import annotations

from tau_agent_core.codemode.extension import CodeMode, CodeModeConfig
from tau_agent_core.codemode.protocol import LIFETIMES
from tau_agent_core.sdk import _resolve_tools

TOOLS = _resolve_tools(["read", "bash", "edit", "write", "grep", "find", "ls"])


def main() -> None:
    for provider in ("cpython", "monty", "typescript"):
        for lifetime in LIFETIMES:
            mode = CodeMode(CodeModeConfig.from_mapping({"provider": provider, "lifetime": lifetime}))
            print(f"## {provider} × {lifetime}\n")
            print("### tool description\n\n```text")
            print(mode.description.rstrip())
            print("```\n\n### system section\n\n````markdown")
            print(mode.section(TOOLS).rstrip())
            print("````\n")


if __name__ == "__main__":
    main()
