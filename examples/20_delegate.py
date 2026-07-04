"""Example 20: Delegate — spawn subagents as isolated ``tau -p`` child processes.

A faithful τ port of pi's ``subagent`` extension
(``coding-agent/examples/extensions/subagent/index.ts``). The ``delegate`` tool
spawns a *separate* ``tau`` process per child so each child gets its own,
isolated context window; structured output is captured over pi-faithful
``--mode json`` (step S8 / E-json), whose per-message ``message_end`` carries the
``usage`` / ``model`` / ``stop_reason`` the delegate reads back.

Every child is spawned exactly as pi spawns its subagents
(``index.ts:288-324``)::

    tau -p --mode json --no-session --no-extensions \\
        [--model M] [--tools read,ls,...] \\
        [--append-system-prompt <tmpfile>] "Task: <task>"

``--no-extensions`` on the child is what makes recursion safe: a child never
re-loads ``delegate`` and so cannot fork-bomb.

Three modes (exactly one per call, pi ``modeCount === 1``):

* **single** — ``{task, ...}`` → one child.
* **parallel** — ``{tasks: [...]}`` → up to :data:`MAX_PARALLEL_TASKS` children,
  :data:`MAX_CONCURRENCY` at a time, each child's output capped at
  :data:`PER_TASK_OUTPUT_CAP` bytes in the rolled-up summary.
* **chain** — ``{chain: [...]}`` → children run in sequence; each step's
  ``{previous}`` placeholder is substituted with the prior step's final output.

Per-child limits (a τ addition on top of pi's spawn shape) are enforced by
reading the E-json child signals live: ``max_turns`` (assistant turns),
``max_seconds`` (wall clock), ``max_usd`` (priced from per-message tokens ×
config ``cost`` — E4.cost — since the child stream carries tokens, not dollars),
and *stuck-detection* (a child looping on the identical tool call). When a limit
trips, the child is killed and its ``stop_reason`` is set from the
:data:`_STOP_REASON` taxonomy.

**HARD CODE-GUARD (Fail-Early).** In **parallel** mode every child is forced
read-only: :func:`_guard_parallel_tools` refuses any write-classified tool
(:data:`WRITE_TOOLS`) by *raising* — it does not silently strip it — and forces
the read-only default allowlist when a task names no tools. Concurrent isolated
children must not race on the filesystem.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.delegate import delegate_extension  # loaded via importlib in tests

session = create_agent_session(
    model="gpt-4o", tools=["read"], extensions=[delegate_extension],
)
```
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# ── pi parity constants (index.ts:26-29) ────────────────────────────────────
MAX_PARALLEL_TASKS = 8
MAX_CONCURRENCY = 4
PER_TASK_OUTPUT_CAP = 50 * 1024  # 50 KB per task in the rolled-up summary

# ── HARD CODE-GUARD: write-tool classification (a small constant list) ───────
# Any built-in tool that can mutate the filesystem. Conservative: ``bash`` is a
# write tool because it can do anything. Parallel children are forbidden these.
WRITE_TOOLS: frozenset[str] = frozenset({"write", "edit", "bash"})
# The read-only allowlist a parallel child gets when it names no tools of its
# own (the complement of WRITE_TOOLS over τ's built-ins: read/ls/grep/find).
PARALLEL_READONLY_TOOLS: tuple[str, ...] = ("read", "ls", "grep", "find")

# ── stop_reason taxonomy (delegate-imposed reasons + child-reported ones) ────
_STOP_REASON = {
    "max_turns": "max_turns",
    "timeout": "timeout",
    "over_budget": "over_budget",
    "stuck": "stuck",
    "aborted": "aborted",
    "error": "error",
}
# Reasons that make a child a *failure* (pi isFailedResult + τ limit trips). A
# clean child ends with the child-reported "stop" (τ) / "end" and is a success.
_FAILED_REASONS: frozenset[str] = frozenset(
    {"error", "aborted", "timeout", "over_budget", "stuck", "max_turns"}
)

# Default stuck window: this many consecutive assistant turns repeating the
# identical tool-call signature counts as a loop.
_DEFAULT_STUCK_LIMIT = 3


# ── data carriers ────────────────────────────────────────────────────────────


@dataclass
class ChildUsage:
    """Rolled-up usage for one child (pi ``UsageStats``).

    ``cost`` is ``None`` when the child's model has no ``cost`` block in
    ``~/.tau/config.json`` — Fail-Early, an unpriced run reports *unknown*, not a
    fabricated ``$0``. It is a running float only when a price is known.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    turns: int = 0
    cost: float | None = None


@dataclass
class ChildResult:
    """Result of one spawned child (pi ``SingleResult``)."""

    task: str
    model: str | None
    tools: list[str]
    exit_code: int = 0
    final_output: str = ""
    stderr: str = ""
    stop_reason: str | None = None
    error_message: str | None = None
    step: int | None = None
    usage: ChildUsage = field(default_factory=ChildUsage)

    def to_details(self) -> dict[str, Any]:
        """Serialize into the tool result ``details`` payload."""
        return {
            "task": self.task,
            "model": self.model,
            "tools": list(self.tools),
            "exit_code": self.exit_code,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "step": self.step,
            "usage": {
                "input": self.usage.input,
                "output": self.usage.output,
                "cache_read": self.usage.cache_read,
                "cache_write": self.usage.cache_write,
                "context_tokens": self.usage.context_tokens,
                "turns": self.usage.turns,
                "cost": self.usage.cost,
            },
        }


@dataclass
class Limits:
    """Per-child budget / liveness limits (a τ addition over pi's spawn shape)."""

    max_usd: float | None = None
    max_seconds: float | None = None
    max_turns: int | None = None
    stuck_limit: int = _DEFAULT_STUCK_LIMIT


def _is_failed(result: ChildResult) -> bool:
    """A child failed if it exited non-zero or ended on a failure stop_reason."""
    return result.exit_code != 0 or (result.stop_reason in _FAILED_REASONS)


# ── config / cost lookup ─────────────────────────────────────────────────────


def _load_config() -> dict[str, Any]:
    """Read ``~/.tau/config.json`` (the same file the child resolves its model
    from). Returns ``{}`` when absent. Not a fallback: the delegate genuinely has
    no config to price against, and :func:`_resolve_cost` fails loudly only when
    a *budget* actually needs one.
    """
    path = Path.home() / ".tau" / "config.json"
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return loaded


def _resolve_cost(config: dict[str, Any], model: str | None) -> dict[str, Any] | None:
    """Resolve the ``{input, output, cache_read, cache_write}`` per-model price
    (USD per 1M tokens) for a child's *effective* model — its explicit ``model``
    or the config ``default_model``. ``None`` when unknown (unpriced).
    """
    effective = model or config.get("default_model")
    if not effective:
        return None
    entry = config.get("models", {}).get(effective, {})
    cost = entry.get("cost")
    return cost if isinstance(cost, dict) else None


def _price_increment(
    cost: dict[str, Any] | None, *, input_tokens: int, output_tokens: int, cache_read_tokens: int
) -> float | None:
    """Dollar cost of one completion, or ``None`` when unpriced (same formula as
    ``tau_coding_agent.backends.compute_cost_usd`` — kept inline so the example is
    importable without the TUI package on the path).
    """
    if cost is None:
        return None
    return (
        float(cost.get("input", 0.0)) / 1_000_000 * input_tokens
        + float(cost.get("output", 0.0)) / 1_000_000 * output_tokens
        + float(cost.get("cache_read", 0.0)) / 1_000_000 * cache_read_tokens
    )


# ── tool guard + argv construction ───────────────────────────────────────────


def _guard_parallel_tools(requested: list[str] | None) -> list[str]:
    """HARD CODE-GUARD (Fail-Early): a parallel child is read-only.

    Raises ``ValueError`` if the request names any write-classified tool — it is
    NOT silently stripped. When the request names no tools, force the read-only
    default allowlist so the child still has read/ls/grep/find.
    """
    if requested is None:
        return list(PARALLEL_READONLY_TOOLS)
    offending = sorted(t for t in requested if t in WRITE_TOOLS)
    if offending:
        raise ValueError(
            "parallel delegate children are read-only; refusing write tools "
            f"{offending} (parallel isolated children must not race on the "
            "filesystem — run these in chain mode if a write is required)"
        )
    return list(requested)


def _child_cli_args(
    *, model: str | None, tools: list[str] | None, system_prompt_path: str | None, task: str
) -> list[str]:
    """Build the child ``tau`` CLI arguments (pi ``runSingleAgent``, index.ts:288-324).

    Always headless JSON, ephemeral, extension-free. ``--model`` / ``--tools`` /
    ``--append-system-prompt`` are added only when supplied (pi: ``if agent.model``
    / ``if agent.tools``). The task text is passed as the single positional
    message, prefixed ``Task:`` exactly like pi.
    """
    args = ["-p", "--mode", "json", "--no-session", "--no-extensions"]
    if model:
        args += ["--model", model]
    if tools:
        args += ["--tools", ",".join(tools)]
    if system_prompt_path:
        args += ["--append-system-prompt", system_prompt_path]
    args.append(f"Task: {task}")
    return args


def _tau_invocation(args: list[str]) -> tuple[str, list[str]]:
    """Return ``(command, argv)`` that re-runs τ headlessly.

    τ analog of pi's ``getPiInvocation``: re-run the *current* Python interpreter
    against the CLI module (``python -m tau_coding_agent.cli``) rather than
    depending on a ``tau`` console script being on ``PATH`` — robust in a venv and
    in tests, and pi-faithful in spirit (pi re-runs its own runtime + entry).
    """
    return sys.executable, ["-m", "tau_coding_agent.cli", *args]


# ── child spawning + live limit enforcement ──────────────────────────────────


def _tool_call_signature(message: dict[str, Any]) -> str | None:
    """Signature of an assistant message's tool calls (name + args), or ``None``
    when it makes no tool call. Used for stuck-detection.
    """
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            parts.append((block.get("name"), json.dumps(block.get("arguments"), sort_keys=True)))
    return json.dumps(parts, sort_keys=True) if parts else None


def _final_text(message: dict[str, Any]) -> str:
    """Concatenate an assistant message's text blocks (pi ``getFinalOutput``)."""
    out = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "".join(out)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Terminate a child, escalating to SIGKILL if it lingers (pi kill path)."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()


async def _run_single_child(
    *,
    task: str,
    model: str | None,
    tools: list[str] | None,
    system_prompt: str | None,
    cwd: str,
    step: int | None,
    limits: Limits,
    cost: dict[str, Any] | None,
    signal: Any | None,
) -> ChildResult:
    """Spawn one ``tau -p --mode json`` child, stream its E-json events, roll up
    usage, and enforce the per-child limits live. Returns a :class:`ChildResult`.
    """
    result = ChildResult(task=task, model=model, tools=list(tools or []), step=step)
    if cost is not None:
        result.usage.cost = 0.0

    tmp_prompt: str | None = None
    if system_prompt and system_prompt.strip():
        fd, tmp_prompt = tempfile.mkstemp(prefix="tau-delegate-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)

    try:
        cli_args = _child_cli_args(
            model=model, tools=tools, system_prompt_path=tmp_prompt, task=task
        )
        command, argv = _tau_invocation(cli_args)
        proc = await asyncio.create_subprocess_exec(
            command,
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None

        override: str | None = None
        prev_sig: str | None = None
        repeat = 0

        async def _drain_stderr() -> None:
            data = await proc.stderr.read()  # type: ignore[union-attr]
            result.stderr += data.decode(errors="replace")

        stderr_task = asyncio.create_task(_drain_stderr())

        async def _read_stdout() -> str | None:
            nonlocal prev_sig, repeat
            async for raw in proc.stdout:  # type: ignore[union-attr]
                if signal is not None and signal.is_aborted():
                    return _STOP_REASON["aborted"]
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "message_end":
                    continue
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue

                # Roll up usage from this completion's message_end.
                result.usage.turns += 1
                usage = message.get("usage") or {}
                result.usage.input += int(usage.get("input_tokens") or 0)
                result.usage.output += int(usage.get("output_tokens") or 0)
                result.usage.cache_read += int(usage.get("cache_read_tokens") or 0)
                result.usage.cache_write += int(usage.get("cache_write_tokens") or 0)
                result.usage.context_tokens = int(usage.get("total_tokens") or 0)
                inc = _price_increment(
                    cost,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
                )
                if inc is not None and result.usage.cost is not None:
                    result.usage.cost += inc
                if message.get("model") and result.model is None:
                    result.model = message["model"]
                if message.get("stop_reason"):
                    result.stop_reason = message["stop_reason"]
                text = _final_text(message)
                if text:
                    result.final_output = text

                # Stuck-detection: identical tool-call signature repeating.
                sig = _tool_call_signature(message)
                if sig is not None and sig == prev_sig:
                    repeat += 1
                    if repeat >= limits.stuck_limit:
                        return _STOP_REASON["stuck"]
                else:
                    repeat = 0
                prev_sig = sig

                # Turn / budget limits.
                if limits.max_turns is not None and result.usage.turns >= limits.max_turns:
                    return _STOP_REASON["max_turns"]
                if (
                    limits.max_usd is not None
                    and result.usage.cost is not None
                    and result.usage.cost > limits.max_usd
                ):
                    return _STOP_REASON["over_budget"]
            return None

        try:
            override = await asyncio.wait_for(_read_stdout(), timeout=limits.max_seconds)
        except asyncio.TimeoutError:
            override = _STOP_REASON["timeout"]

        if override is not None:
            await _kill(proc)
        exit_code = await proc.wait()
        await stderr_task
        result.exit_code = exit_code

        if override is not None:
            result.stop_reason = override
            if result.error_message is None:
                result.error_message = f"child {override}"
        elif exit_code != 0 and result.stop_reason not in _FAILED_REASONS:
            result.stop_reason = _STOP_REASON["error"]
            if not result.error_message:
                result.error_message = result.stderr.strip() or f"child exited {exit_code}"
        return result
    finally:
        if tmp_prompt:
            try:
                os.unlink(tmp_prompt)
            except OSError:
                pass


async def _map_with_concurrency(
    items: list[Any], concurrency: int, fn: Callable[[Any, int], Awaitable[ChildResult]]
) -> list[ChildResult]:
    """Run ``fn(item, index)`` over ``items`` with a bounded worker pool (pi
    ``mapWithConcurrencyLimit``), preserving input order in the results.
    """
    if not items:
        return []
    limit = max(1, min(concurrency, len(items)))
    results: list[ChildResult | None] = [None] * len(items)
    nxt = 0
    lock = asyncio.Lock()

    async def _worker() -> None:
        nonlocal nxt
        while True:
            async with lock:
                cur = nxt
                nxt += 1
            if cur >= len(items):
                return
            results[cur] = await fn(items[cur], cur)

    await asyncio.gather(*[_worker() for _ in range(limit)])
    return [r for r in results if r is not None]


def _truncate(output: str) -> str:
    """Cap a child's output at :data:`PER_TASK_OUTPUT_CAP` bytes for the rolled-up
    summary (pi ``truncateParallelOutput``); full output stays in ``details``.
    """
    encoded = output.encode("utf-8")
    if len(encoded) <= PER_TASK_OUTPUT_CAP:
        return output
    clipped = encoded[:PER_TASK_OUTPUT_CAP].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(clipped.encode("utf-8"))
    return f"{clipped}\n\n[Output truncated: {omitted} bytes omitted; full output in details.]"


def _child_output(result: ChildResult) -> str:
    """The text to surface for a child (error text when failed, else final output)."""
    if _is_failed(result):
        return result.error_message or result.stderr.strip() or result.final_output or "(no output)"
    return result.final_output or "(no output)"


# ── the tool ─────────────────────────────────────────────────────────────────


def _extract_spec(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize one task/chain item into a child spec."""
    if "task" not in item or not str(item.get("task", "")).strip():
        raise ValueError("each delegate task requires a non-empty 'task'")
    tools = item.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise TypeError("'tools' must be a list of tool names")
    return {
        "task": item["task"],
        "model": item.get("model"),
        "tools": tools,
        "system_prompt": item.get("system_prompt"),
        "cwd": item.get("cwd"),
    }


def _make_limits(params: dict[str, Any]) -> Limits:
    limits = Limits(
        max_usd=params.get("max_usd"),
        max_seconds=params.get("max_seconds"),
        max_turns=params.get("max_turns"),
        stuck_limit=int(params.get("stuck_limit") or _DEFAULT_STUCK_LIMIT),
    )
    return limits


async def _delegate_execute(
    tool_call_id: str,
    params: dict[str, Any],
    signal: Any,
    on_update: Callable | None,
    ctx: Any,
) -> dict[str, Any]:
    """Execute the ``delegate`` tool (pi ``subagent`` execute)."""
    has_single = bool(params.get("task"))
    has_parallel = bool(params.get("tasks"))
    has_chain = bool(params.get("chain"))
    mode_count = has_single + has_parallel + has_chain
    if mode_count != 1:
        raise ValueError(
            "delegate: provide exactly one of 'task' (single), 'tasks' (parallel), "
            f"or 'chain' — got {mode_count}"
        )

    limits = _make_limits(params)
    config = _load_config()
    default_cwd = getattr(ctx, "cwd", ".") or "."

    # max_usd is enforceable only when the child's model has a price — Fail-Early:
    # refuse the budget rather than silently not enforcing it.
    def _cost_for(model: str | None) -> dict[str, Any] | None:
        cost = _resolve_cost(config, model)
        if limits.max_usd is not None and cost is None:
            raise ValueError(
                "delegate: max_usd set but the child model has no 'cost' block in "
                "~/.tau/config.json to price against"
            )
        return cost

    # ── parallel ────────────────────────────────────────────────────────────
    if has_parallel:
        raw = params["tasks"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("delegate: 'tasks' must be a non-empty list")
        if len(raw) > MAX_PARALLEL_TASKS:
            raise ValueError(
                f"delegate: too many parallel tasks ({len(raw)}); max is {MAX_PARALLEL_TASKS}"
            )
        specs = [_extract_spec(item) for item in raw]
        # HARD CODE-GUARD: force every parallel child read-only (raises on write).
        for spec in specs:
            spec["tools"] = _guard_parallel_tools(spec["tools"])

        async def _run(spec: dict[str, Any], index: int) -> ChildResult:
            return await _run_single_child(
                task=spec["task"],
                model=spec["model"],
                tools=spec["tools"],
                system_prompt=spec["system_prompt"],
                cwd=spec["cwd"] or default_cwd,
                step=None,
                limits=limits,
                cost=_cost_for(spec["model"]),
                signal=signal,
            )

        results = await _map_with_concurrency(specs, MAX_CONCURRENCY, _run)
        succeeded = sum(1 for r in results if not _is_failed(r))
        summaries = []
        for r in results:
            status = "completed" if not _is_failed(r) else f"failed ({r.stop_reason})"
            summaries.append(
                f"### [{r.model or 'default'}] {status}\n\n{_truncate(_child_output(r))}"
            )
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Parallel: {succeeded}/{len(results)} succeeded\n\n"
                    + "\n\n---\n\n".join(summaries),
                }
            ],
            "details": {"mode": "parallel", "results": [r.to_details() for r in results]},
            "is_error": succeeded < len(results),
        }

    # ── chain ─────────────────────────────────────────────────────────────────
    if has_chain:
        raw = params["chain"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("delegate: 'chain' must be a non-empty list")
        specs = [_extract_spec(item) for item in raw]
        results = []
        previous = ""
        for i, spec in enumerate(specs):
            task = spec["task"].replace("{previous}", previous)
            result = await _run_single_child(
                task=task,
                model=spec["model"],
                tools=spec["tools"],
                system_prompt=spec["system_prompt"],
                cwd=spec["cwd"] or default_cwd,
                step=i + 1,
                limits=limits,
                cost=_cost_for(spec["model"]),
                signal=signal,
            )
            results.append(result)
            if _is_failed(result):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Chain stopped at step {i + 1} "
                            f"({spec['model'] or 'default'}): {_child_output(result)}",
                        }
                    ],
                    "details": {"mode": "chain", "results": [r.to_details() for r in results]},
                    "is_error": True,
                }
            previous = result.final_output
        return {
            "content": [{"type": "text", "text": results[-1].final_output or "(no output)"}],
            "details": {"mode": "chain", "results": [r.to_details() for r in results]},
        }

    # ── single ────────────────────────────────────────────────────────────────
    spec = _extract_spec(params)
    result = await _run_single_child(
        task=spec["task"],
        model=spec["model"],
        tools=spec["tools"],
        system_prompt=spec["system_prompt"],
        cwd=spec["cwd"] or default_cwd,
        step=None,
        limits=limits,
        cost=_cost_for(spec["model"]),
        signal=signal,
    )
    if _is_failed(result):
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Agent {result.stop_reason or 'failed'}: {_child_output(result)}",
                }
            ],
            "details": {"mode": "single", "results": [result.to_details()]},
            "is_error": True,
        }
    return {
        "content": [{"type": "text", "text": result.final_output or "(no output)"}],
        "details": {"mode": "single", "results": [result.to_details()]},
    }


DELEGATE_TOOL = {
    "name": "delegate",
    "label": "Delegate",
    "description": (
        "Delegate tasks to subagents that run in isolated `tau` child processes. "
        "Modes: single (task), parallel (tasks array — read-only children only), "
        "chain (sequential, with a {previous} placeholder). Optional per-child "
        "limits: max_usd, max_seconds, max_turns, stuck_limit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Task for single mode."},
            "model": {"type": "string", "description": "Child model (single mode)."},
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool allowlist for the child (single mode).",
            },
            "system_prompt": {
                "type": "string",
                "description": "Appended to the child's system prompt (single mode).",
            },
            "tasks": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Parallel: array of {task, model?, tools?, system_prompt?}.",
            },
            "chain": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Chain: array of {task, ...}; task may contain {previous}.",
            },
            "max_usd": {"type": "number", "description": "Per-child USD budget."},
            "max_seconds": {"type": "number", "description": "Per-child wall-clock cap."},
            "max_turns": {"type": "integer", "description": "Per-child assistant-turn cap."},
            "stuck_limit": {
                "type": "integer",
                "description": "Consecutive identical tool calls before 'stuck'.",
            },
        },
        "required": [],
    },
    "execute": _delegate_execute,
    "execution_mode": "sequential",
}


def delegate_extension(api: Any) -> None:
    """Extension that registers the ``delegate`` tool."""
    api.register_tool(DELEGATE_TOOL)
