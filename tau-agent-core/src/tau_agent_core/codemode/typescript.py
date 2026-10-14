"""A TypeScript provider with a prompt and no runtime: the plug-in surface, shown.

What a real one would add is :meth:`TypeScriptProvider._open`,
``_execute`` and ``_destroy`` — e.g. a ``deno``/QuickJS child speaking the same
JSON-lines channel as ``_cpython_child.py``, or a Cloudflare Worker reached over
HTTP. Everything else (lifetimes, deadline, abort, the prompt, the ``codemode``
tool, stub rendering as ``declare const tools: {...}``) is already provided.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tau_agent_core.codemode.prompt import TYPESCRIPT_TEMPLATES
from tau_agent_core.codemode.protocol import (
    PromptTemplates,
    RunLimits,
    RunResult,
    SandboxUnavailable,
    ToolInvoker,
)
from tau_agent_core.codemode.scoped import ScopedProvider


class TypeScriptProvider(ScopedProvider[None]):
    """TypeScript scripts; prompt only. Running one raises :class:`SandboxUnavailable`."""

    name = "typescript"
    language = "typescript"

    def prompt(self) -> PromptTemplates:
        """TypeScript's default templates."""
        return TYPESCRIPT_TEMPLATES

    async def start(self) -> None:
        """Refuse: there is no runtime behind this provider.

        Raises:
            SandboxUnavailable: always.
        """
        raise SandboxUnavailable(
            "the typescript provider is an interface stub with no runtime; "
            "it renders prompts but cannot run scripts"
        )

    async def _open(self, limits: RunLimits, tools: ToolInvoker) -> None:
        """Refuse, as :meth:`start` does."""
        await self.start()

    async def _execute(
        self,
        instance: None,
        code: str,
        tools: ToolInvoker,
        inputs: Mapping[str, Any],
        limits: RunLimits,
    ) -> RunResult:
        """Unreachable: :meth:`_open` refuses first."""
        raise SandboxUnavailable("no runtime")

    async def _destroy(self, instance: None) -> None:
        """Nothing to release."""
