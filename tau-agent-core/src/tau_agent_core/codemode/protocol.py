"""The sandbox-provider contract for code mode (docs/CODE-MODE.md §6.1).

A provider runs one script and reports what it printed, what it returned and
which tools it called. Which interpreter does that, and how long it lives, is
configuration: the agent sees the difference only through the prompt text the
provider contributes (:class:`PromptTemplates`).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Lifetime = Literal["script", "turn", "session"]
"""How long one interpreter lives.

``script`` is a new interpreter per call. ``turn`` reuses one for every call made
while answering one user prompt. ``session`` keeps one per cursor until the
session ends, the way a REPL or notebook kernel does.
"""

LIFETIMES: tuple[Lifetime, ...] = ("script", "turn", "session")

ErrorKind = Literal["script", "timeout", "aborted", "sandbox"]
"""Why a run failed: the script raised, ran out of time, was aborted, or the
sandbox itself failed (crash, memory limit, call budget, no runtime)."""


@dataclass(frozen=True)
class RunLimits:
    """Per-run resource limits.

    Attributes:
        timeout_s: Wall-clock seconds, tool time included. The host kills the run.
        memory_bytes: Heap or address-space cap for the interpreter.
        max_calls: Nested tool calls one run may make.
        output_chars: Characters of output the model reads before the cap cuts.
    """

    timeout_s: float = 30.0
    memory_bytes: int = 512 * 1024 * 1024
    max_calls: int = 1000
    output_chars: int = 8000


@dataclass
class NestedCall:
    """One tool call a script made.

    Attributes:
        name: The tool called.
        args: Its arguments, as JSON text cut to 2 KiB.
        ok: Whether the tool succeeded.
        duration_ms: Host-side time from request to answer.
        error: The tool's error text when ``ok`` is false.
    """

    name: str
    args: str
    ok: bool
    duration_ms: float
    error: str | None = None


@dataclass
class RunResult:
    """What one run produced.

    Attributes:
        output: Everything the script printed, uncut.
        value: ``repr`` of the trailing expression's value, or ``None``.
        error: The error text when the run failed, else ``None``.
        error_kind: Which kind of failure, else ``None``.
        limit: Which limit tripped (``"memory"``, ``"time"``, ``"calls"``), if any.
        calls: The nested tool calls, in completion order.
        reset: True when the interpreter's state was discarded by this run, so a
            persistent lifetime starts empty on the next call.
        duration_ms: Host-side wall time of the run.
    """

    output: str = ""
    value: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    limit: str | None = None
    calls: list[NestedCall] = field(default_factory=list)
    reset: bool = False
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """True when the run completed without error."""
        return self.error_kind is None


@dataclass(frozen=True)
class ToolStub:
    """What a script needs to know to call one tool.

    Attributes:
        name: The tool's name, also its attribute on ``tools``.
        description: The tool's description.
        parameters: Its JSON-schema ``parameters`` object.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass
class ToolOutcome:
    """A nested call's answer, as the script receives it.

    Attributes:
        text: The tool's text content, joined.
        is_error: True when the tool failed; the script then sees an exception.
    """

    text: str
    is_error: bool = False


@runtime_checkable
class ToolInvoker(Protocol):
    """The bridge a sandbox calls tools through.

    One per run. It owns the tool set a script may reach, so a provider never
    sees τ's tool objects, and it records every call it answers.
    """

    def stubs(self) -> list[ToolStub]:
        """The tools callable from a script, in a stable order."""
        ...

    async def invoke(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool and return its outcome; never raises for a tool failure."""
        ...

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        """Rank the callable tools against ``query``; ``[{name, description}]``."""
        ...

    def describe(self, name: str) -> str:
        """One tool's signature and description, rendered for the script's language."""
        ...

    @property
    def calls(self) -> list[NestedCall]:
        """Every call answered so far."""
        ...


@dataclass(frozen=True)
class PromptTemplates:
    """A provider's prompt contribution, as ``{{field}}`` templates.

    Rendered by :func:`tau_agent_core.codemode.prompt.render_prompt`. Every field
    can be replaced from config (``codemode.prompt``).

    Attributes:
        system: The system-prompt section.
        description: The ``codemode`` tool's description. Keep it free of anything
            that changes mid-session, or every change invalidates the prompt cache.
        lifetime: One paragraph per lifetime, explaining what persists.
        language_notes: What the language lacks or does differently.
        language_label: The language as the prompt names it.
    """

    system: str
    description: str
    lifetime: Mapping[str, str]
    language_notes: str
    language_label: str


@runtime_checkable
class SandboxProvider(Protocol):
    """An interpreter that runs code-mode scripts.

    ``scope`` names the interpreter instance a run uses: the provider keeps one
    per scope for ``turn`` and ``session`` lifetimes and opens a fresh one per run
    for ``script``. The caller picks scope keys (a turn id, a cursor id) and ends
    them with :meth:`end_scope`.

    Attributes:
        name: Config name, e.g. ``"cpython"``.
        language: Stub language, ``"python"`` or ``"typescript"``.
        lifetime: The configured lifetime.
        lifetimes: The lifetimes this provider can serve.
    """

    name: str
    language: str
    lifetime: Lifetime
    lifetimes: frozenset[Lifetime]

    async def start(self) -> None:
        """Acquire long-lived resources (a worker pool). Idempotent."""
        ...

    async def close(self) -> None:
        """Release every instance and resource."""
        ...

    async def run(
        self,
        code: str,
        *,
        tools: ToolInvoker,
        inputs: Mapping[str, Any] | None = None,
        limits: RunLimits,
        scope: str,
        signal: Any = None,
    ) -> RunResult:
        """Run ``code`` in the instance for ``scope``.

        Never raises for a script failure: that is a :class:`RunResult` with an
        ``error_kind``. Raises only for a misuse of the provider itself.
        """
        ...

    async def end_scope(self, scope: str) -> None:
        """Discard the instance held for ``scope``, if any."""
        ...

    def prompt(self) -> PromptTemplates:
        """The default prompt templates for this provider."""
        ...


class SandboxUnavailable(RuntimeError):
    """The provider has no runtime here: a missing extra, binary or service."""


def elapsed_ms(start: float) -> float:
    """Milliseconds since ``start``, a ``time.perf_counter()`` reading."""
    return (time.perf_counter() - start) * 1000.0
