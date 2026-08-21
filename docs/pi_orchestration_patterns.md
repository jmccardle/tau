# Pi orchestration patterns

Design patterns for adding sub-agents, task pipelines, goal loops, and cost
controls to **pi** (`@earendil-works/pi-coding-agent`) — a harness that ships
*none* of these on purpose and exposes them as composition seams instead.

## The two planes

Pi deliberately omits sub-agents, to-dos, plan mode, max-step limits, and cost
ceilings. Decide where each pattern lives:

- **Orchestration plane — Python, outside pi.** Parallel/sequential pipelines,
  goal loops, cost ceilings, model routing, stuck-detection. A "sub-agent" is a
  `pi --mode json` subprocess with its own `--model`; you stream its events,
  meter them, and kill it when it misbehaves. Full observability — pi's thesis.
- **Agent-facing plane — TypeScript, inside pi.** Only what the *running* agent
  must trigger itself: prompt-template slash commands and
  `registerTool`/`registerCommand` extensions, plus a `pi.on("turn_end")` hook
  for in-session budget enforcement. Mostly thin shims over the Python layer.

Run modes you'll use: `--print` (one-shot text), `--mode json` (one JSON object
per line on stdout — the orchestration interface), `--mode rpc` (JSONL over
stdin/stdout for streaming control), and the TS SDK (`createAgentSession`).

---

## 1. Model & provider routing

### Declare local endpoints once

`~/.pi/agent/models.json` — local Qwen via vLLM/Ollama (`openai-completions`).
**Confirm exact keys against `docs/models.md`**; this mirrors the documented
`pi-ai` `Model` object shape.

```jsonc
{
  "providers": {
    "local-vllm": {
      "api": "openai-completions",
      "baseUrl": "http://localhost:8000/v1",
      "apiKey": "dummy",
      "models": [
        { "id": "qwen3.6-27b", "name": "Qwen3.6 27B (local)", "reasoning": true,
          "input": ["text"], "contextWindow": 81920, "maxTokens": 8192,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 } },
        { "id": "qwen3.5-2b", "name": "Qwen3.5 2B (local agent)", "reasoning": false,
          "input": ["text"], "contextWindow": 32768, "maxTokens": 4096,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 } }
      ]
    }
  }
}
```

The verified *programmatic* equivalent (from pi-ai), useful if you go the SDK
route — note it's the same field set the JSON mirrors:

```ts
import { Model, stream } from "@earendil-works/pi-ai";
const qwen: Model<"openai-completions"> = {
  id: "qwen3.6-27b", name: "Qwen3.6 27B", api: "openai-completions",
  provider: "local-vllm", baseUrl: "http://localhost:8000/v1",
  reasoning: true, input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 81920, maxTokens: 8192,
};
```

After this, `pi --model local-vllm/qwen3.6-27b "…"` works in any subprocess.
`pi --list-models` shows everything your auth exposes.

### Route by task, scope by tool

Three levers per role: **model** (`--model provider/id`), **thinking**
(`--thinking off|minimal|low|medium|high|xhigh`, or the `:high` suffix), and
**capability** (`--tools read,grep,find,ls` for a read-only reviewer;
`--no-builtin-tools`/`--no-tools` to start from nothing). Keep micro-subagents
lean and hermetic with `-nc --no-extensions --no-skills` so you don't load
`AGENTS.md` + every extension into a 2B-model call.

---

## 2. The orchestrator

`pi_orchestra.py` — the whole layer. Each sub-agent is a metered `pi --mode json`
subprocess. Prices below are **illustrative**; pi-ai already carries real
per-token costs in its registry, so only fill these if you want a custom basis
(e.g. local = 0, or a notional electricity cost for your 4090).

```python
#!/usr/bin/env python3
"""pi_orchestra.py — a thin Python orchestration layer over `pi`.

pi ships no sub-agents, no to-dos, no max-step limits, no cost ceilings.
We keep all of that here and drive pi as a subprocess. Every "sub-agent" is a
`pi --mode json` run with its own --model; we stream its events, meter
tokens -> $ against our own price table, enforce cost/wall/turn ceilings, and
watch for stuck loops.

Requires: pi on PATH (npm i -g --ignore-scripts @earendil-works/pi-coding-agent).
"""
from __future__ import annotations
import asyncio, json, os, re, time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ---- model registry ---------------------------------------------------------
@dataclass(frozen=True)
class Model:
    ref: str                       # value for `pi --model`, e.g. "anthropic/claude-sonnet-4-5"
    thinking: str | None = None     # off|minimal|low|medium|high|xhigh
    price_in: float = 0.0           # USD / 1M tokens  (illustrative; see note above)
    price_out: float = 0.0
    price_cache_read: float = 0.0
    price_cache_write: float = 0.0

# Example roster. Route by task, not by habit. Swap refs for whatever
# `pi --list-models` shows on your account.
MODELS = {
    "plan":       Model("anthropic/claude-opus-4-5",   "high",    15, 75, 1.5, 18.75),
    "implement":  Model("anthropic/claude-sonnet-4-5", "medium",   3, 15, 0.3,  3.75),
    "review":     Model("anthropic/claude-opus-4-5",   "high",    15, 75, 1.5, 18.75),
    "mechanical": Model("local-vllm/qwen3.5-2b",       "off"),         # cheap local
    "summarize":  Model("local-vllm/qwen3.6-27b",      "minimal"),     # bigger local
}

# ---- limits + result --------------------------------------------------------
@dataclass
class Limits:
    max_usd: float = 1.0
    max_seconds: float = 600.0
    max_turns: int = 40
    max_tool_errors: int = 5       # N consecutive tool errors => stuck
    stuck_repeat: int = 3          # N identical consecutive tool calls => stuck

@dataclass
class Result:
    text: str = ""
    session_id: str | None = None
    tokens: dict = field(default_factory=lambda: {"input":0,"output":0,"cacheRead":0,"cacheWrite":0})
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    wall_s: float = 0.0
    stop_reason: str = "done"      # done|budget|timeout|turns|stuck|error
    model: str = ""
    task: str = ""

def _text_of(message: dict) -> str:
    out = []
    for blk in message.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            out.append(blk.get("text", ""))
    return "".join(out)

def _cost(tok: dict, m: Model) -> float:
    return (tok["input"]      / 1e6 * m.price_in
          + tok["output"]     / 1e6 * m.price_out
          + tok["cacheRead"]  / 1e6 * m.price_cache_read
          + tok["cacheWrite"] / 1e6 * m.price_cache_write)

async def _terminate(proc: asyncio.subprocess.Process):
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()

async def run_pi(task: str, model: Model, *, limits: Limits = Limits(),
                 cwd: str | None = None, session_args: Sequence[str] | None = None,
                 extra_args: Sequence[str] | None = None,
                 ledger: "Ledger | None" = None) -> Result:
    cmd = ["pi", "--mode", "json", "--model", model.ref]
    if model.thinking:
        cmd += ["--thinking", model.thinking]
    cmd += list(session_args or ["--no-session"])     # ephemeral by default
    cmd += list(extra_args or [])
    cmd.append(task)

    # No per-spawn network chatter; big JSON lines need a fat StreamReader buffer.
    env = {**os.environ, "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, limit=10 * 1024 * 1024,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)   # flip to PIPE+drain to debug

    r = Result(model=model.ref, task=task)
    start = time.monotonic()
    recent: deque = deque(maxlen=limits.stuck_repeat)
    consec_err = 0

    try:
        while True:
            remaining = limits.max_seconds - (time.monotonic() - start)
            if remaining <= 0:
                r.stop_reason = "timeout"; break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                r.stop_reason = "timeout"; break
            if not line:
                break  # EOF -> agent finished on its own
            try:
                ev = json.loads(line.decode().strip() or "{}")
            except json.JSONDecodeError:
                continue
            t = ev.get("type")

            if t == "session":
                r.session_id = ev.get("id")
            elif t == "turn_start":
                r.turns += 1
                if r.turns > limits.max_turns:
                    r.stop_reason = "turns"; break
            elif t == "tool_execution_start":
                r.tool_calls += 1
                sig = (ev.get("toolName"), json.dumps(ev.get("args"), sort_keys=True))
                recent.append(sig)
                if len(recent) == recent.maxlen and len(set(recent)) == 1:
                    r.stop_reason = "stuck"; break   # same tool+args N times
            elif t == "tool_execution_end":
                if ev.get("isError"):
                    r.tool_errors += 1; consec_err += 1
                    if consec_err >= limits.max_tool_errors:
                        r.stop_reason = "stuck"; break
                else:
                    consec_err = 0
            elif t in ("message_end", "turn_end"):
                msg = ev.get("message", {})
                if msg.get("role") == "assistant":
                    u = msg.get("usage", {}) or {}   # confirm keys vs pi-ai AssistantMessage
                    for k in r.tokens:
                        r.tokens[k] += int(u.get(k, 0) or 0)
                    r.cost_usd = _cost(r.tokens, model)
                    txt = _text_of(msg)
                    if txt:
                        r.text = txt                 # last assistant text wins
                    if r.cost_usd >= limits.max_usd:
                        r.stop_reason = "budget"; break

        if r.stop_reason != "done":
            await _terminate(proc)
        else:
            await proc.wait()
    except Exception as e:
        r.stop_reason = "error"; r.text = f"{type(e).__name__}: {e}"
        await _terminate(proc)
    finally:
        r.wall_s = round(time.monotonic() - start, 2)
        if ledger is not None:
            ledger.log(r)
    return r
```

### Parallel (`fan_out`)

```python
async def fan_out(jobs: Iterable[tuple[str, Model]], *, limits: Limits = Limits(),
                  concurrency: int = 4, ledger: "Ledger | None" = None) -> list[Result]:
    sem = asyncio.Semaphore(concurrency)
    async def one(task, model):
        async with sem:
            return await run_pi(task, model, limits=limits, ledger=ledger)
    return await asyncio.gather(*(one(t, m) for t, m in jobs))
```

### Sequential (`pipeline`)

Each step is `(build_prompt, model)`; `build_prompt(prior_text)` returns the
next prompt, so step N's output is a component of step N+1's prompt.

```python
async def pipeline(steps: Sequence[tuple[Callable[[str], str], Model]], *,
                   seed: str = "", limits: Limits = Limits(),
                   ledger: "Ledger | None" = None) -> list[Result]:
    carry, out = seed, []
    for build_prompt, model in steps:
        res = await run_pi(build_prompt(carry), model, limits=limits, ledger=ledger)
        out.append(res); carry = res.text
    return out
```

---

## 3. Goal loops

### Implementer ↔ evaluator until a goal condition

Alternate two runs until the evaluator emits `VERDICT: PASS` or you exhaust
rounds/budget. The implementer keeps its session across rounds (memory of prior
attempts); the evaluator is ephemeral and read-only (it can run tests via bash
but not edit).

```python
@dataclass
class Verdict:
    passed: bool
    notes: str

def parse_verdict(text: str) -> Verdict:
    return Verdict("VERDICT: PASS" in text.upper(), text)

async def refine_until(goal: str, *, implementer: Model, evaluator: Model,
                       max_rounds: int = 5, max_usd: float = 5.0,
                       cwd: str | None = None, ledger: "Ledger | None" = None) -> list[Result]:
    spent, history, sid, runs = 0.0, "", None, []
    for rnd in range(1, max_rounds + 1):
        impl_prompt = (
            f"Goal:\n{goal}\n\n"
            f"Previous evaluator feedback:\n{history or '(none)'}\n\n"
            "Make the smallest change that moves toward the goal, then run the "
            "tests. Summarize exactly what you changed."
        )
        sess = ["--session", sid] if sid else ["--name", f"refine-{int(time.time())}"]
        impl = await run_pi(impl_prompt, implementer, cwd=cwd,
                            limits=Limits(max_usd=max_usd - spent), session_args=sess, ledger=ledger)
        sid = impl.session_id or sid
        spent += impl.cost_usd
        runs.append(impl)
        if spent >= max_usd:
            return runs

        eval_prompt = (
            f"Goal:\n{goal}\n\nImplementer's report:\n{impl.text}\n\n"
            "Independently verify: read the changed files and run the tests. "
            "Give a short rationale, then a final line exactly: "
            "`VERDICT: PASS` or `VERDICT: FAIL`."
        )
        ev = await run_pi(eval_prompt, evaluator, cwd=cwd,
                          limits=Limits(max_usd=max_usd - spent),
                          session_args=["--no-session"],
                          extra_args=["--tools", "read,grep,find,ls,bash"], ledger=ledger)
        spent += ev.cost_usd
        runs.append(ev)
        v = parse_verdict(ev.text)
        history += f"\n[round {rnd}] {'PASS' if v.passed else 'FAIL'}: {v.notes[:500]}"
        if v.passed or spent >= max_usd:
            return runs
    return runs
```

### To-do runner — fresh session per task = "compaction between tasks"

Headless pi can't `/compact` on demand (it's interactive; auto-compaction only
fires on overflow). The clean equivalent is a **brand-new `--no-session` per
task**: zero carried context to compact, full observability, and a natural
hand-off point. The agent checks items off `TODO.md` itself.

```python
TODO_RE = re.compile(r"^- \[( |x)\] (.+)$")

def next_unchecked(todo: Path) -> str | None:
    for line in todo.read_text().splitlines():
        m = TODO_RE.match(line.strip())
        if m and m.group(1) == " ":
            return m.group(2).strip()
    return None

async def run_todo(todo: Path, model: Model, *, limits: Limits = Limits(),
                   ledger: "Ledger | None" = None):
    while (task := next_unchecked(todo)) is not None:
        prompt = (
            f"Work the single task below to completion, then edit {todo} to check "
            f"it off (`- [ ]` -> `- [x]`). Run tests if relevant. Do not start "
            f"other tasks.\n\nTASK: {task}"
        )
        res = await run_pi(prompt, model, limits=limits,
                           session_args=["--no-session"], ledger=ledger)
        if next_unchecked(todo) == task:   # agent didn't tick it -> stop, don't loop forever
            print(f"task not completed (stop={res.stop_reason}): {task!r}")
            break
```

---

## 4. Cost limits & tracking

### Ledger + analysis — make objectives trackable

Every `run_pi` call logs a row. Group by model for **$/successful-run**, the
number you actually route on.

```python
class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
    def log(self, r: Result):
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), **asdict(r)}) + "\n")

def report(ledger_path: str | Path):
    import pandas as pd
    df = pd.read_json(ledger_path, lines=True)
    df["ok"] = df["stop_reason"].eq("done")
    g = df.groupby("model").agg(
        runs=("task", "size"),
        ok_rate=("ok", "mean"),
        mean_cost=("cost_usd", "mean"),
        mean_wall=("wall_s", "mean"),
        mean_turns=("turns", "mean"),
        total_cost=("cost_usd", "sum"),
    )
    g["usd_per_ok"] = g["total_cost"] / (g["runs"] * g["ok_rate"]).replace(0, float("nan"))
    return g.sort_values("usd_per_ok")
```

`stop_reason` doubles as your stuck/timeout/budget telemetry: a model that keeps
landing on `stuck` or `turns` for a given task-type is one you reroute or
escalate. To **escalate on stuck**, re-queue the same task on a bigger model:

```python
async def run_with_escalation(task, primary: Model, fallback: Model, **kw):
    r = await run_pi(task, primary, **kw)
    if r.stop_reason in ("stuck", "turns", "timeout", "error"):
        r2 = await run_pi(task, fallback, **kw)
        return r2 if r2.stop_reason == "done" else r
    return r
```

### In-agent budget guard (optional, TS)

If you want the *running* agent to self-limit — your "cost limit defined at
request start, in the agent" — a small extension. **Sketch: confirm the
`ExtensionAPI` surface and the abort/notify methods against `docs/extensions.md`**;
the event/usage field shapes are from `docs/json.md`.

```ts
// ~/.pi/agent/extensions/budget-guard.ts
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const budget = Number(process.env.PI_BUDGET_USD ?? "0");
  let spent = 0;
  pi.on("turn_end", async (event: any, ctx: any) => {
    const u = event?.message?.usage;
    if (u?.cost != null) spent += u.cost;                 // if pi precomputes per-message cost
    if (budget > 0 && spent >= budget) {
      ctx?.notify?.(`Budget $${budget.toFixed(2)} hit (spent $${spent.toFixed(2)}). Aborting.`);
      ctx?.abort?.();                                     // stop the loop; verify method name
    }
  });
}
```

Set the budget at request start: `PI_BUDGET_USD=2.50 pi -e ~/.pi/agent/extensions/budget-guard.ts "big task"`.

---

## 5. Exposing patterns to the agent (slash commands)

Prompt templates are Markdown in `~/.pi/agent/prompts/`, invoked `/name`. `$@`
expands to the user's text after the command (named `{{vars}}` also exist — see
`docs/prompt-templates.md`).

`prompts/review.md` — capability-scoped read-only reviewer sub-agent:

```markdown
---
description: Spawn a read-only reviewer sub-agent and report only its findings
---
Spawn a sub-agent via bash and report only its findings. Do NOT review yourself:

    pi --print --no-session --tools read,grep,find,ls \
       --model anthropic/claude-opus-4-5:high \
       "Review for bugs, security, and error handling: $@"
```

`prompts/fanout.md` — parallel sub-agents with no extra tooling (bash is
synchronous and there's no background bash, so background within one call):

```markdown
---
description: Run the listed sub-tasks as parallel pi sub-agents
---
Launch each sub-task below as a parallel `pi` sub-agent in a SINGLE bash call:
background each with `&`, then `wait`. Use a named model per line if given.
Collect and report each result with its model and cost. Do NOT do the work yourself.

$@
```

The agent then emits something like:

```bash
pi --print --no-session --model anthropic/claude-sonnet-4-5  "subtask A" > a.out & p1=$!
pi --print --no-session --model local-vllm/qwen3.6-27b       "subtask B" > b.out & p2=$!
wait $p1 $p2
cat a.out b.out
```

For metered parallelism (cost ceilings, stuck-detection), point the template at
your Python CLI instead and let `fan_out` own the spawning.

---

## Demo wiring

```python
async def _demo():
    led = Ledger("pi_runs.jsonl")

    # parallel fan-out across models (read-only work is the safe parallel case)
    await fan_out([
        ("Audit auth/ for security issues; list findings only.", MODELS["review"]),
        ("List every TODO/FIXME under src/ with file:line.",      MODELS["mechanical"]),
        ("Summarize what the build does in 5 bullets.",           MODELS["summarize"]),
    ], ledger=led)

    # sequential: research -> plan -> implement
    await pipeline([
        (lambda _: "Research how config loading works; output a findings doc.", MODELS["mechanical"]),
        (lambda f: f"Given these findings, propose a migration plan:\n{f}",      MODELS["plan"]),
        (lambda p: f"Implement step 1 of this plan:\n{p}",                       MODELS["implement"]),
    ], ledger=led)

    # goal loop
    await refine_until("Make `pytest -q` pass with zero failures.",
                       implementer=MODELS["implement"], evaluator=MODELS["review"],
                       max_rounds=5, max_usd=4.0, ledger=led)

    print(report("pi_runs.jsonl"))

if __name__ == "__main__":
    asyncio.run(_demo())
```

---

## Caveats / things to confirm

1. **Token usage field keys.** `run_pi` reads `message.usage` with
   `input/output/cacheRead/cacheWrite` — these match pi's footer legend and the
   `cost` keys on the custom-model object, but verify against `AssistantMessage`
   in `pi-ai`'s `types.ts`. (Alternatively skip this and trust pi's own cost in
   `/session` / `--mode json` — but owning the price table is what lets you put
   local models on the same axis as API ones.)
2. **`models.json` schema** is extrapolated from the documented `Model` object;
   confirm exact keys in `docs/models.md` / `docs/custom-provider.md`.
3. **`ExtensionAPI` abort/notify** in the budget guard is a sketch — confirm the
   real method names in `docs/extensions.md`.
4. **Don't parallelize implementation.** Mario's explicit warning: parallel
   sub-agents that *write* tend to turn a codebase into a pile of garbage. Keep
   parallel for read-only fan-out (review, search, summarize); use sequential or
   single-threaded for anything that edits.
5. **RPC framing.** If you switch to `--mode rpc`, split records on `\n` only —
   not a generic line reader. `--mode json` over `readline()` is fine.
