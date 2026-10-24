"""The bridge from a script's ``tools.<name>(...)`` to τ's real tools.

:class:`PipelineToolInvoker` sends every script call through ``ctx.execute_tool``,
so it takes the path a model-issued call takes: argument validation, the
``tool_call`` veto, the ``tool_result`` rewrite, and ``tool_execution_*`` events
carrying ``parent_tool_call_id``. A permission gate written as a hook covers
script calls (docs/CODE-MODE.md §3, §5.1).
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from tau_agent_core.codemode.prompt import describe_stub
from tau_agent_core.codemode.protocol import NestedCall, ToolOutcome, ToolStub, elapsed_ms
from tau_agent_core.tool_pipeline import NestedToolResult
from tau_agent_core.tool_schema import stub_name, stub_names
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolError

ExecuteTool = Callable[[str, dict[str, Any]], Awaitable[NestedToolResult]]
"""One nested call: the signature of ``ctx.execute_tool`` without its keyword."""

_TOKEN = re.compile(r"[a-z0-9]+")
_ARGS_CAP = 2048


def _tokens(text: str) -> list[str]:
    """Lower-case word tokens, with ``snake_case`` split."""
    return _TOKEN.findall(text.lower().replace("_", " "))


class _Bm25:
    """BM25 over each tool's name, description and parameter names."""

    def __init__(self, stubs: Sequence[ToolStub]) -> None:
        self._docs = [
            Counter(
                _tokens(
                    f"{s.name} {s.name} {s.description} {' '.join(s.parameters.get('properties') or {})}"
                )
            )
            for s in stubs
        ]
        self._stubs = list(stubs)
        n = len(self._docs) or 1
        self._avg = sum(sum(d.values()) for d in self._docs) / n
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(d.keys())
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def rank(self, query: str, limit: int) -> list[ToolStub]:
        """The ``limit`` best stubs with a positive score, best first."""
        terms = _tokens(query)
        scored: list[tuple[float, int]] = []
        for i, doc in enumerate(self._docs):
            length = sum(doc.values())
            score = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    score += (
                        self._idf[t] * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self._avg))
                    )
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda p: (-p[0], p[1]))
        return [self._stubs[i] for _, i in scored[:limit]]


class PipelineToolInvoker:
    """A :class:`~tau_agent_core.codemode.protocol.ToolInvoker` over ``ctx.execute_tool``.

    ``tools`` only supplies the stubs and discovery. Every call runs through
    ``execute``, which validates, runs the hooks and the tool, and raises on any
    failure. A failure becomes an error outcome the script reads, never a raise.
    A script reaches each tool as ``tools.<stub name>`` (``my-tool`` as
    ``tools.my_tool``); :meth:`invoke` and :meth:`describe` take that name or the
    real one.

    Args:
        tools: The tools a script may call, e.g. the turn's tool list.
        execute: Runs one nested call, normally ``ctx.execute_tool``. Its parent
            is the running ``codemode`` call, which ``execute`` finds itself.
        exclude: Names never callable from a script (``codemode`` itself).
        unavailable: Excluded tools a script may name, each with the text a
            call or :meth:`describe` answers instead of ``Unknown tool``.
        language: Stub language for :meth:`describe`.
        max_calls: Calls answered before every further call fails.

    Raises:
        ValueError: two tools share a stub name
            (:func:`~tau_agent_core.tool_schema.stub_names`).
    """

    def __init__(
        self,
        tools: Iterable[AgentTool],
        execute: ExecuteTool,
        *,
        exclude: Iterable[str] = ("codemode",),
        unavailable: Mapping[str, str] | None = None,
        language: str = "python",
        max_calls: int = 1000,
    ) -> None:
        excluded = set(exclude)
        self._tools = {t.name: t for t in tools if t.name not in excluded}
        self._real = stub_names(self._tools)
        refusals = dict(unavailable or {})
        self._refusals = {**{stub_name(n): t for n, t in refusals.items()}, **refusals}
        self._stubs = [
            ToolStub(t.name, t.description, dict(t.parameters)) for t in self._tools.values()
        ]
        self._execute = execute
        self._language = language
        self._max_calls = max_calls
        self._calls: list[NestedCall] = []
        self._started = 0
        self._index: _Bm25 | None = None
        self.budget_exhausted = False

    def stubs(self) -> list[ToolStub]:
        """The callable tools, in the order they were given."""
        return list(self._stubs)

    @property
    def calls(self) -> list[NestedCall]:
        """Every call answered so far, in completion order."""
        return list(self._calls)

    async def invoke(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run ``name`` with ``args``; a failure is an error outcome, never a raise."""
        start = time.perf_counter()
        self._started += 1
        if self._started > self._max_calls:
            self.budget_exhausted = True
            return self._record(
                name,
                args,
                start,
                ToolOutcome(
                    f"call budget exhausted: a script may make {self._max_calls} tool calls", True
                ),
            )
        tool = self._tools.get(self._real.get(name, name))
        if tool is None:
            refusal = self._refusals.get(name, f"Unknown tool: {name}")
            return self._record(name, args, start, ToolOutcome(refusal, True))
        name = tool.name
        try:
            outcome = ToolOutcome((await self._execute(name, args)).text)
        except ToolError as exc:
            outcome = ToolOutcome(str(exc), True)
        except Exception as exc:
            blocks = AgentToolResult.from_exception(name, exc).content
            outcome = ToolOutcome("\n".join(str(b.get("text", "")) for b in blocks), True)
        return self._record(name, args, start, outcome)

    def _record(
        self, name: str, args: dict[str, Any], start: float, outcome: ToolOutcome
    ) -> ToolOutcome:
        """Append the call record and pass the outcome through."""
        try:
            text = json.dumps(args, default=str)
        except (TypeError, ValueError):
            text = repr(args)
        self._calls.append(
            NestedCall(
                name=name,
                args=text[:_ARGS_CAP],
                ok=not outcome.is_error,
                duration_ms=elapsed_ms(start),
                error=outcome.text[:_ARGS_CAP] if outcome.is_error else None,
            )
        )
        return outcome

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        """BM25 over the callable tools; ``[{name, description}]``, best first, by stub name."""
        if self._index is None:
            self._index = _Bm25(self._stubs)
        return [
            {
                "name": s.attribute,
                "description": s.description.strip().splitlines()[0]
                if s.description.strip()
                else "",
            }
            for s in self._index.rank(query, max(1, int(limit)))
        ]

    def describe(self, name: str) -> str:
        """Signature, description and schema of one tool, by stub name or real name.

        Raises:
            KeyError: ``name`` is not callable from a script.
        """
        real = self._real.get(name, name)
        for stub in self._stubs:
            if stub.name == real:
                return describe_stub(stub, self._language)
        if name in self._refusals:
            raise KeyError(self._refusals[name])
        raise KeyError(f"no tool named {name!r} is callable from scripts")
