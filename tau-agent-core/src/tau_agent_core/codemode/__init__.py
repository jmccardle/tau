"""Code mode: the model writes a script, and the tools run inside it.

Experimental (docs/CODE-MODE.md; experiments/sandbox-providers/RESULTS.md). The
pieces, bottom-up: :mod:`.protocol` (the provider contract), :mod:`.scoped`
(lifetimes and deadlines), :mod:`.cpython` and :mod:`.monty` (two runtimes),
:mod:`.typescript` (a prompt-only stub), :mod:`.prompt` (stubs and templates),
:mod:`.invoker` (the bridge to τ's tools) and :mod:`.extension` (the tool).
"""

from tau_agent_core.codemode.protocol import (
    LIFETIMES,
    ErrorKind,
    Lifetime,
    NestedCall,
    PromptTemplates,
    RunLimits,
    RunResult,
    SandboxProvider,
    SandboxUnavailable,
    ToolInvoker,
    ToolOutcome,
    ToolStub,
)

__all__ = [
    "LIFETIMES",
    "ErrorKind",
    "Lifetime",
    "NestedCall",
    "PromptTemplates",
    "RunLimits",
    "RunResult",
    "SandboxProvider",
    "SandboxUnavailable",
    "ToolInvoker",
    "ToolOutcome",
    "ToolStub",
]
