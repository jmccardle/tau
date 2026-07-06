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
trips, the child is killed and its ``stop_reason`` is set from the imposed
taxonomy (:data:`ext_kit.spawn.FAILED_REASONS`).

**HARD CODE-GUARD (Fail-Early).** In **parallel** mode every child is forced
read-only: :func:`_guard_parallel_tools` refuses any write-classified tool
(:data:`WRITE_TOOLS`) by *raising* — it does not silently strip it — and forces
the read-only default allowlist when a task names no tools. Concurrent isolated
children must not race on the filesystem.

## S59 — refactored onto ``ext_kit.spawn``

The whole spawn machinery this demo once hand-rolled — the child argv, the
subprocess lifecycle / kill, the E-json usage roll-up, the live limit enforcement
(turns / wall-clock / budget / stuck-loop), and the bounded worker pool — is the
exact pattern S53 distilled into :mod:`ext_kit.spawn`
(``docs/EXTENSIONS-DEMO-ROADMAP.md §4``: "Extracted + hardened from
``20_delegate``"). S59 refactors the demo to CONSUME that kit as the proof the
abstraction is the right one, with behavior preserved:

* :func:`ext_kit.spawn.spawn_tau` runs each child (usage roll-up + limit
  enforcement + abort propagation), returning a :class:`ext_kit.spawn.ChildResult`.
* :class:`ext_kit.spawn.WorkerPool` bounds the parallel fan-out.
* :func:`ext_kit.spawn.build_child_args` builds the child argv.

The demo keeps only what is genuinely its own: the three-mode dispatch, the
``Task:`` prompt prefix, the ``{previous}`` chain substitution, the config/cost
lookup, the parallel read-only guard, and the output truncation / summarisation.

## Usage

```python
from tau_agent_core.sdk import create_agent_session
from examples.delegate import delegate_extension  # loaded via importlib in tests

session = create_agent_session(
    model="gpt-4o", tools=["read"], extensions=[delegate_extension],
)
```

Reference: EXTENSIONS-IMPLEMENTATION.md §E-demo-1, §8 S9;
docs/EXTENSIONS-DEMO-ROADMAP.md §4 S53 / §7 S59 (refactored onto ``ext_kit.spawn``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

# ── import the kit (it lives alongside the demos in examples/) ───────────────
# The file-path extension loader (``tau -e examples/20_delegate.py``) does not add
# the extension's own directory to ``sys.path``, and the test harness loads this
# file by path too — so bootstrap ``examples/`` onto the path before importing the
# kit, whether run directly, imported, or loaded via ``-e`` (D-E6-3).
_EXAMPLES_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from ext_kit import spawn  # noqa: E402  (path insertion must precede the import)

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

    Delegates the argv shape to :func:`ext_kit.spawn.build_child_args` (always
    headless JSON, ephemeral, extension-free; ``--model`` / ``--tools`` /
    ``--append-system-prompt`` added only when supplied) and passes the task text
    as the single positional message, prefixed ``Task:`` exactly like pi — the one
    piece of wording the kit leaves to the caller.
    """
    return spawn.build_child_args(
        prompt=f"Task: {task}",
        model=model,
        tools=tools,
        system_prompt_path=system_prompt_path,
    )


# ── result summarisation ──────────────────────────────────────────────────────


def _child_details(child: spawn.ChildResult, *, task: str, step: int | None) -> dict[str, Any]:
    """Serialize one child's :class:`ext_kit.spawn.ChildResult` into the tool
    result ``details`` payload (adding the demo-specific ``task`` / ``step``).
    """
    usage = child.usage
    return {
        "task": task,
        "model": child.model,
        "tools": list(child.tools),
        "exit_code": child.exit_code,
        "stop_reason": child.stop_reason,
        "error_message": child.error_message,
        "step": step,
        "usage": {
            "input": usage.input,
            "output": usage.output,
            "cache_read": usage.cache_read,
            "cache_write": usage.cache_write,
            "context_tokens": usage.context_tokens,
            "turns": usage.turns,
            "cost": usage.cost,
        },
    }


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


def _child_output(child: spawn.ChildResult) -> str:
    """The text to surface for a child (error text when failed, else final output)."""
    if child.failed:
        return child.error_message or child.stderr.strip() or child.final_output or "(no output)"
    return child.final_output or "(no output)"


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


def _make_limits(params: dict[str, Any]) -> spawn.SpawnLimits:
    """Build the per-child :class:`ext_kit.spawn.SpawnLimits` from the tool params."""
    return spawn.SpawnLimits(
        max_usd=params.get("max_usd"),
        max_seconds=params.get("max_seconds"),
        max_turns=params.get("max_turns"),
        stuck_limit=int(params.get("stuck_limit") or spawn.DEFAULT_STUCK_LIMIT),
    )


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

    async def _spawn(spec: dict[str, Any]) -> spawn.ChildResult:
        """Spawn one child through the kit over a normalised spec."""
        return await spawn.spawn_tau(
            f"Task: {spec['task']}",
            model=spec["model"],
            tools=spec["tools"],
            cwd=spec["cwd"] or default_cwd,
            system_prompt=spec["system_prompt"],
            limits=limits,
            cost=_cost_for(spec["model"]),
            signal=signal,
        )

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

        # Bounded fan-out via the kit's WorkerPool (pi mapWithConcurrencyLimit);
        # results come back in input order.
        pool = spawn.WorkerPool(MAX_CONCURRENCY)

        async def _run(spec: dict[str, Any], _index: int) -> spawn.ChildResult:
            return await _spawn(spec)

        results = await pool.map(_run, specs)
        succeeded = sum(1 for r in results if not r.failed)
        summaries = []
        for r in results:
            status = "completed" if not r.failed else f"failed ({r.stop_reason})"
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
            "details": {
                "mode": "parallel",
                "results": [
                    _child_details(r, task=spec["task"], step=None)
                    for r, spec in zip(results, specs, strict=True)
                ],
            },
            "is_error": succeeded < len(results),
        }

    # ── chain ─────────────────────────────────────────────────────────────────
    if has_chain:
        raw = params["chain"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("delegate: 'chain' must be a non-empty list")
        specs = [_extract_spec(item) for item in raw]
        details: list[dict[str, Any]] = []
        previous = ""
        for i, spec in enumerate(specs):
            step_spec = dict(spec)
            step_spec["task"] = spec["task"].replace("{previous}", previous)
            result = await _spawn(step_spec)
            details.append(_child_details(result, task=step_spec["task"], step=i + 1))
            if result.failed:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Chain stopped at step {i + 1} "
                            f"({spec['model'] or 'default'}): {_child_output(result)}",
                        }
                    ],
                    "details": {"mode": "chain", "results": details},
                    "is_error": True,
                }
            previous = result.final_output
        return {
            "content": [{"type": "text", "text": previous or "(no output)"}],
            "details": {"mode": "chain", "results": details},
        }

    # ── single ────────────────────────────────────────────────────────────────
    spec = _extract_spec(params)
    result = await _spawn(spec)
    child_details = _child_details(result, task=spec["task"], step=None)
    if result.failed:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Agent {result.stop_reason or 'failed'}: {_child_output(result)}",
                }
            ],
            "details": {"mode": "single", "results": [child_details]},
            "is_error": True,
        }
    return {
        "content": [{"type": "text", "text": result.final_output or "(no output)"}],
        "details": {"mode": "single", "results": [child_details]},
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


#: The module-level ``register`` the file-path loader looks up (``tau -e
#: examples/20_delegate.py`` → ``getattr(module, "register")``). It IS
#: :func:`delegate_extension`; the alias makes the demo loadable through the public
#: ``-e`` surface used by the live procedures (EXTENSIONS-LIVE-PROCEDURES.md;
#: EXTENSIONS-E5-WIRING.md §6 / S37).
register = delegate_extension
