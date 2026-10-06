"""Monty sandbox provider (``pydantic-monty``, the ``codemode-monty`` extra).

Monty is a Python subset interpreted by a Rust VM in a worker-process pool. A
checkout is one REPL session, so the ``session`` lifetime is a held checkout.
Three Monty defaults are overridden here (docs/CODE-MODE.md §6.2): ``print`` is
captured instead of reaching the host's stdout, a session that trips a memory or
time limit is discarded, and ``max_suspensions`` is raised from 1000 to cover the
configured call budget.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from tau_agent_core.codemode.prompt import MONTY_TEMPLATES, render_type_check_stubs
from tau_agent_core.codemode.protocol import (
    Lifetime,
    PromptTemplates,
    RunLimits,
    RunResult,
    SandboxUnavailable,
    ToolInvoker,
)
from tau_agent_core.codemode.scoped import ScopedProvider

_OUTPUT_HARD_CAP = 10 * 1024 * 1024


def _import_monty() -> Any:
    """``pydantic_monty``, or :class:`SandboxUnavailable` naming the extra."""
    try:
        import pydantic_monty
    except ImportError as exc:
        raise SandboxUnavailable(
            "the monty provider needs pydantic-monty: pip install 'ffwf-tau-agent-core[codemode-monty]'"
        ) from exc
    return pydantic_monty


@dataclass
class _Checkout:
    """One held Monty session and the context manager that owns it."""

    manager: Any
    session: Any


class _Tools:
    """Host object behind ``tools``: it holds the run's invoker and nothing else."""

    def __init__(self, invoker: ToolInvoker) -> None:
        self.invoker = invoker


_NAMESPACE_CLASS: Any = None


def _tools_namespace(monty: Any, tools: ToolInvoker) -> Any:
    """``tools`` for the sandbox: a ClassInstance whose methods are the tool names.

    The wrapper classes are built once per process, because Monty keys a class's
    sandbox identity on its qualified name and refuses a second class under it.
    """
    global _NAMESPACE_CLASS
    if _NAMESPACE_CLASS is None:

        class ToolNamespace(monty.ClassInstance):  # type: ignore[misc,name-defined]
            """Routes ``tools.<name>(**kwargs)`` to the invoker."""

            def get_attr(self, name: str, policy: Any) -> Any:
                """A coroutine function for an allowed tool name."""
                if policy is None or name not in policy:
                    raise self.attr_error(name)
                invoker: ToolInvoker = self.value.invoker

                async def call(*args: Any, **kwargs: Any) -> str:
                    if args:
                        raise TypeError(f"tools.{name}() takes keyword arguments only")
                    outcome = await invoker.invoke(name, kwargs)
                    if outcome.is_error:
                        raise RuntimeError(outcome.text)
                    return outcome.text

                return call

        _NAMESPACE_CLASS = ToolNamespace
    names = frozenset(s.name for s in tools.stubs())
    return _NAMESPACE_CLASS(_Tools(tools), allowed_methods=names)


class MontyProvider(ScopedProvider[_Checkout]):
    """Monty workers from one ``AsyncMonty`` pool.

    Args:
        lifetime: ``script``, ``turn`` or ``session``.
        type_check: Type-check each script against generated tool stubs before
            it runs, so a misspelled tool or a wrong argument type fails with no
            tool having run.
        min_processes: Workers kept warm in the pool.
        max_processes: Pool size cap; ``None`` is Monty's default.
    """

    name = "monty"
    language = "python"

    def __init__(
        self,
        lifetime: Lifetime = "script",
        *,
        type_check: bool = False,
        min_processes: int = 1,
        max_processes: int | None = None,
    ) -> None:
        super().__init__(lifetime)
        self._type_check = type_check
        self._min = min_processes
        self._max = max_processes
        self._monty: Any = None
        self._pool: Any = None

    def prompt(self) -> PromptTemplates:
        """Monty's default templates."""
        return MONTY_TEMPLATES

    async def start(self) -> None:
        """Import Monty and spawn the pool. Idempotent.

        Raises:
            SandboxUnavailable: ``pydantic-monty`` is not installed.
        """
        if self._pool is not None:
            return
        self._monty = _import_monty()
        pool = self._monty.AsyncMonty(min_processes=self._min, max_processes=self._max)
        await pool.__aenter__()
        self._pool = pool

    async def close(self) -> None:
        """Release every checkout, then the pool."""
        await super().close()
        if self._pool is not None:
            pool, self._pool = self._pool, None
            with contextlib.suppress(Exception):
                await pool.__aexit__(None, None, None)

    async def _open(self, limits: RunLimits, tools: ToolInvoker) -> _Checkout:
        """Check a session out of the pool with this run's limits."""
        await self.start()
        manager = self._pool.checkout(
            limits={
                "max_feed_duration_secs": limits.timeout_s,
                "max_memory": limits.memory_bytes,
                "max_suspensions": 4 * limits.max_calls + 100,
            },
            type_check=self._type_check,
            type_check_stubs=render_type_check_stubs(tools.stubs()) if self._type_check else None,
        )
        session = await manager.__aenter__()
        return _Checkout(manager, session)

    async def _destroy(self, instance: _Checkout) -> None:
        """Return the checkout to the pool."""
        with contextlib.suppress(Exception):
            await instance.manager.__aexit__(None, None, None)

    async def _execute(
        self,
        instance: _Checkout,
        code: str,
        tools: ToolInvoker,
        inputs: Mapping[str, Any],
        limits: RunLimits,
    ) -> RunResult:
        """One ``feed_run`` with output captured and Monty errors classified."""
        monty = self._monty
        chunks: list[str] = []
        size = [0]

        def collect(stream: Literal["stdout", "stderr"], text: str) -> None:
            if size[0] < _OUTPUT_HARD_CAP:
                chunks.append(text)
                size[0] += len(text)

        async def search_tools(query: str, limit: int = 8) -> list[dict[str, str]]:
            return tools.search(query, limit)

        async def describe_tool(name: str) -> str:
            try:
                return tools.describe(name)
            except KeyError as exc:
                raise RuntimeError(str(exc.args[0])) from None

        try:
            value = await instance.session.feed_run(
                code,
                inputs={"tools": _tools_namespace(monty, tools), **inputs},
                external_lookup={"search_tools": search_tools, "describe_tool": describe_tool},
                print_callback=collect,
            )
        except monty.MontyCrashedError as exc:
            return RunResult(
                output="".join(chunks),
                error=f"the sandbox worker crashed: {exc}",
                error_kind="sandbox",
                reset=True,
            )
        except monty.MontyTypingError as exc:
            return RunResult(
                output="".join(chunks),
                error=f"type check failed; nothing ran:\n{exc.display()}",
                error_kind="script",
            )
        except monty.MontyError as exc:
            return self._classify(exc, "".join(chunks), tools)
        return RunResult(output="".join(chunks), value=None if value is None else repr(value))

    @staticmethod
    def _classify(exc: Any, output: str, tools: ToolInvoker) -> RunResult:
        """A runtime or syntax error as a :class:`RunResult`."""
        inner = exc.exception() if hasattr(exc, "exception") else None
        text = exc.display() if hasattr(exc, "display") else str(exc)
        if isinstance(inner, MemoryError):
            return RunResult(
                output=output, error=text, error_kind="sandbox", limit="memory", reset=True
            )
        if isinstance(inner, TimeoutError):
            return RunResult(
                output=output, error=text, error_kind="timeout", limit="time", reset=True
            )
        if getattr(tools, "budget_exhausted", False) or "suspension" in text.lower():
            return RunResult(output=output, error=text, error_kind="sandbox", limit="calls")
        return RunResult(output=output, error=text, error_kind="script")
