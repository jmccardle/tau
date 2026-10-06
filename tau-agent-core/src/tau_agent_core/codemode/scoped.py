"""Lifetime and deadline handling shared by every concrete provider.

A provider subclasses :class:`ScopedProvider` and supplies three things: how to
open an interpreter instance, how to run code in one, and how to destroy one.
This class decides when an instance is reused (the lifetime), serializes runs on
one instance, and enforces the wall-clock deadline and the abort signal from the
host side, because an interpreter's own clock cannot be trusted to fire inside C
code (docs/CODE-MODE.md §6.2: ``sys.settrace`` missed a 0.2 s deadline by 2.7 s).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from tau_agent_core.codemode.protocol import (
    Lifetime,
    PromptTemplates,
    RunLimits,
    RunResult,
    ToolInvoker,
    elapsed_ms,
)

InstanceT = TypeVar("InstanceT")

_ABORT_POLL_S = 0.05


class ScopedProvider(Generic[InstanceT]):
    """Base for providers whose instances live for a script, a turn or a session.

    Args:
        lifetime: The configured lifetime.
    """

    name = "abstract"
    language = "python"
    lifetimes: frozenset[Lifetime] = frozenset({"script", "turn", "session"})

    def __init__(self, lifetime: Lifetime = "script") -> None:
        if lifetime not in self.lifetimes:
            raise ValueError(
                f"provider {self.name!r} supports lifetimes {sorted(self.lifetimes)}, not {lifetime!r}"
            )
        self.lifetime: Lifetime = lifetime
        self._instances: dict[str, InstanceT] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Acquire long-lived resources. Default: none."""

    async def close(self) -> None:
        """Destroy every held instance."""
        for scope in list(self._instances):
            await self.end_scope(scope)

    async def end_scope(self, scope: str) -> None:
        """Destroy the instance held for ``scope``, if any."""
        instance = self._instances.pop(scope, None)
        self._locks.pop(scope, None)
        if instance is not None:
            await self._destroy(instance)

    def prompt(self) -> PromptTemplates:
        """The provider's default prompt templates."""
        raise NotImplementedError

    async def _open(self, limits: RunLimits, tools: ToolInvoker) -> InstanceT:
        """Create one interpreter instance."""
        raise NotImplementedError

    async def _execute(
        self,
        instance: InstanceT,
        code: str,
        tools: ToolInvoker,
        inputs: Mapping[str, Any],
        limits: RunLimits,
    ) -> RunResult:
        """Run ``code`` in ``instance``. No deadline here; :meth:`run` owns it."""
        raise NotImplementedError

    async def _destroy(self, instance: InstanceT) -> None:
        """Kill and release ``instance``. Must not raise."""
        raise NotImplementedError

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
        """Run ``code`` under the configured lifetime, deadline and abort signal."""
        key = "" if self.lifetime == "script" else scope
        lock = self._locks.setdefault(key, asyncio.Lock()) if key else asyncio.Lock()
        async with lock:
            start = time.perf_counter()
            instance = self._instances.get(key) if key else None
            if instance is None:
                instance = await self._open(limits, tools)
                if key:
                    self._instances[key] = instance
            task = asyncio.ensure_future(self._execute(instance, code, tools, inputs or {}, limits))
            kind = await self._wait(task, limits.timeout_s, signal)
            if kind is None and task.exception() is not None:
                exc = task.exception()
                result = RunResult(
                    error=f"sandbox failure: {type(exc).__name__}: {exc}",
                    error_kind="sandbox",
                    reset=True,
                )
            elif kind is None:
                result = task.result()
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                verb = "timed out" if kind == "timeout" else "was aborted"
                result = RunResult(
                    error=f"the script {verb} after {elapsed_ms(start) / 1000:.2f} s and was killed",
                    error_kind=kind,
                    limit="time" if kind == "timeout" else None,
                    reset=True,
                )
            result.calls = tools.calls
            result.duration_ms = elapsed_ms(start)
            if result.reset or not key:
                if key:
                    self._instances.pop(key, None)
                await self._destroy(instance)
                result.reset = result.reset and bool(key)
            return result

    @staticmethod
    async def _wait(task: asyncio.Future[Any], timeout: float, signal: Any) -> Any:
        """``None`` when ``task`` finished, else ``"timeout"`` or ``"aborted"``."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            done, _ = await asyncio.wait({task}, timeout=min(remaining, _ABORT_POLL_S))
            if done:
                return None
            if signal is not None and signal.is_aborted():
                return "aborted"
