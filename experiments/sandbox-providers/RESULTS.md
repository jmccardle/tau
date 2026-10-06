Experiment record (2026-10-05). A working proof of concept of code mode's
sandbox-provider layer (docs/CODE-MODE.md §6), on branch
`worktree-agent-ac377c4f237392a9f`. Not merged, not a design decision.

Claims are marked **[ran]** (measured or executed here), **[read]** (taken from
code or docs), **[inferred]** or **[assumed]**.

# Sandbox providers for code mode: results

## 1. The requirement

> Ship compatibility for both Monty and CPython, and add a template item to the
> system prompt. Another sandbox system (TypeScript, a Cloudflare isolate, …)
> could be plugged in, and the system prompt would give the agent the prompting
> required to use that sandbox appropriately. The prompting is adjustable from
> the sandbox config itself, so a persistent sandbox ("repl" / notebook) explains
> its behaviour differently from a new-every-message or new-every-turn one.

All of that is built. Every provider × lifetime renders its own prompt, and every
piece of it can be replaced from config (§4, §5).

## 2. Where it lives, and why

`tau-agent-core/src/tau_agent_core/codemode/`, a subpackage of the core
distribution, plus `examples/codemode.py` as the `-e` entry point.

- **Core, not a head.** The protocol, the prompt rendering and the result
  formatting are derivations every head needs (CLAUDE.md, "three heads"); in
  `tau_coding_agent` only one head would get them.
- **Not a new distribution.** A sixth dist means a sixth `src` tree in every gate,
  a lockstep pin, a PyPI name and a release-matrix entry, for ~2200 lines with no
  new hard dependency. **[inferred]** That cost is worth paying only if code mode
  becomes its own product; it can be split out later without changing imports
  outside the subpackage.
- **Optional deps stay optional.** The CPython provider is stdlib only. Monty is
  imported inside `MontyProvider.start()`; without it, starting raises
  `SandboxUnavailable` naming the new extra `ffwf-tau-agent-core[codemode-monty]`
  (`pydantic-monty>=1.1,<1.2`). The extra is added to `[dev]` so the suite
  exercises Monty; that is an open decision (§10).
- **The entry point is `examples/codemode.py`**, not a numbered example, because
  the file stem is the config key and John's sketch names it `codemode`.

| File | Lines | Holds |
|---|---:|---|
| `codemode/protocol.py` | 240 | `SandboxProvider`, `ToolInvoker`, `RunResult`, `RunLimits`, `PromptTemplates`, … |
| `codemode/scoped.py` | 152 | Lifetime scoping, per-instance lock, host wall-clock deadline, abort polling |
| `codemode/cpython.py` + `_cpython_child.py` | 262 + 207 | CPython subprocess provider and its stdlib-only child |
| `codemode/monty.py` | 235 | Monty provider |
| `codemode/typescript.py` | 63 | Prompt-only TypeScript provider: the plug-in surface |
| `codemode/prompt.py` | 424 | Stub rendering (Python, `.pyi`, TypeScript), templates, overrides |
| `codemode/invoker.py` | 222 | `DirectToolInvoker` (bypasses hooks), BM25 tool search |
| `codemode/extension.py` | 368 | Config, the `codemode` tool, the system-prompt section |

## 3. The protocol **[read: the code as written]**

```python
Lifetime  = Literal["script", "turn", "session"]
ErrorKind = Literal["script", "timeout", "aborted", "sandbox"]

@dataclass(frozen=True)
class RunLimits:
    timeout_s: float = 30.0          # wall clock, tool time included; host kills
    memory_bytes: int = 512 MiB      # RLIMIT_AS (cpython) / max_memory (monty)
    max_calls: int = 1000            # nested tool calls per run
    output_chars: int = 8000         # what the model reads; the rest goes to a file

@dataclass
class RunResult:
    output: str                      # everything printed, uncut
    value: str | None                # repr of the trailing expression
    error: str | None
    error_kind: ErrorKind | None
    limit: str | None                # "memory" | "time" | "calls"
    calls: list[NestedCall]          # name, args (2 KiB), ok, duration_ms, error
    reset: bool                      # a persistent instance was discarded
    duration_ms: float

class ToolInvoker(Protocol):         # one per run; owns the reachable tool set
    def stubs(self) -> list[ToolStub]: ...
    async def invoke(self, name: str, args: dict) -> ToolOutcome: ...   # never raises
    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]: ...
    def describe(self, name: str) -> str: ...
    @property
    def calls(self) -> list[NestedCall]: ...

class SandboxProvider(Protocol):
    name: str                        # config name
    language: str                    # "python" | "typescript": picks the stub renderer
    lifetime: Lifetime
    lifetimes: frozenset[Lifetime]   # what it can serve
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def run(self, code: str, *, tools: ToolInvoker, inputs=None,
                  limits: RunLimits, scope: str, signal=None) -> RunResult: ...
    async def end_scope(self, scope: str) -> None: ...
    def prompt(self) -> PromptTemplates: ...

@dataclass(frozen=True)
class PromptTemplates:               # all {{field}} templates
    system: str                      # the system-prompt section
    description: str                 # the codemode tool description
    lifetime: Mapping[str, str]      # one paragraph per lifetime
    language_notes: str
    language_label: str
```

A concrete provider subclasses `ScopedProvider` and writes three methods,
`_open`, `_execute`, `_destroy`. The base keys instances by `scope` (empty for
`script`, `"<lifetime>:<cursor id>"` otherwise), serializes runs on one
instance, and enforces the deadline and abort **from the host**, killing the
instance, because an interpreter's own clock cannot be trusted inside C code
(CODE-MODE.md §6.2). A `script` instance is destroyed after every run; a
persistent one only when a run sets `reset`.

What the script sees, identical in both Python providers **[ran: shared suite]**:
`await tools.<name>(**kwargs) -> str`; a failed tool raises `RuntimeError` with
the tool's text (CPython raises the subclass `ToolError`); positional arguments
raise `TypeError`; `await search_tools(query, limit=8)` and
`await describe_tool(name)`; `print` and the trailing expression are the only
output.

## 4. Config **[read]**

`~/.tau/config.json` → `extensions.codemode`, or `--ext-config codemode.KEY=VALUE`.
Every key is optional; an unknown key or value raises at load.

```json
{"extensions": {"codemode": {
  "provider": "cpython",          // "cpython" | "monty" | "typescript" (prompt only)
  "lifetime": "script",           // "script" | "turn" | "session"
  "stubs": "snapshot",            // "snapshot": tools at first turn, then frozen | "none"
  "type_check": false,            // monty: ty-check against generated stubs first
  "timeout_s": 30, "memory_mb": 512, "max_calls": 1000, "output_chars": 8000,
  "exclude": [],                  // tools never callable from scripts (codemode always is)
  "env_passthrough": [],          // cpython: extra host env vars for the child
  "prompt": {                     // any subset; {{field}} placeholders allowed
    "system": "…", "description": "…", "language_notes": "…", "language_label": "…",
    "lifetime": {"session": "This is a Jupyter-style kernel: …"}
  }
}}}
```

Template fields: `tool_name`, `provider`, `language`, `language_label`,
`language_notes`, `lifetime`, `lifetime_text`, `lifetime_brief`, `limits`,
`stubs`, `tool_names`. `lifetime_text` and `language_notes` are themselves
rendered, so an override can name fields too. An unknown field raises
`PromptFieldError`, matching `SystemPromptFieldError`.

`CONFIG_SCHEMA` declares the scalar keys as a `ui.form`; `prompt`, `exclude` and
`env_passthrough` cannot be expressed as form fields, so
`set_extension_config` (which replaces the slice wholesale against the schema)
would reject them. **[inferred from docs/extensions.md]**

## 5. The prompt

### 5.1 How it reaches the model

Two outputs per provider × lifetime: a **tool description** and a **system
section**. **[ran: `test_codemode_turn_end_to_end`]**

- The description is rendered once at registration, with no tool list, so it is
  byte-stable for the session whatever registers later.
- The section is rendered at the first `before_agent_start`, from the tools
  present then (`stubs: "snapshot"`), and frozen. The hook appends it to the
  system prompt and is idempotent, so the prompt is the same on every call; the
  test asserts byte equality between turns.

**Why a hook and not a `{{codemode}}` field.** `_build_system_prompt` runs
before extensions load, and extension tools never reach `{{tools}}` at all (it
is built from the built-ins only — `sdk.py:1385`, `backends.py:1421`) **[read]**.
A real template item needs a core seam: an extension-contributed prompt field,
resolved per turn, that a custom prompt may place with `{{codemode}}` and that is
appended otherwise, as `{{tools}}` is. That is a small core change and the most
direct reading of "a template item"; it is open decision 1.

### 5.2 Rendered examples

All nine provider × lifetime combinations, rendered against the seven built-ins,
are in `rendered-prompts.md` (regenerate with `render_prompts.py`). **[ran]**
CPython × script, in full:

````markdown
## Code mode

The `codemode` tool runs Python 3 source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. Carry anything you need forward by printing it, or recompute it.

This is full CPython with its standard library, run in a separate process with the same file access as the bash tool but no API keys in its environment. Prefer tools over direct file or network access, so the user sees what happened.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    …edit, write, grep, find, ls…
```
````

What changes per lifetime (the `lifetime_text` paragraph; Python providers share it):

| Lifetime | Paragraph |
|---|---|
| script | Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. Carry anything you need forward by printing it, or recompute it. |
| turn | Calls made while answering one user message share an interpreter: variables, functions and imports persist from call to call until you reply, then the interpreter is reset. |
| session | The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. It is reset after a timeout, a memory-limit trip or a crash, and the result says so when that happens; redefine what you need then. |

What changes per provider (the `language_notes` paragraph):

| Provider | Notes |
|---|---|
| cpython | This is full CPython with its standard library, run in a separate process with the same file access as the bash tool but no API keys in its environment. Prefer tools over direct file or network access, so the user sees what happened. |
| monty | The interpreter is Monty, a sandboxed subset of Python. It has functions, closures, comprehensions, f-strings, try/except, dataclasses and async/await, and these modules: asyncio, json, re, math, datetime, collections, itertools, functools, typing, random, time, base64. It does NOT have class inheritance (so no custom exception classes), generators or `yield` (a generator expression builds a list, so `next(x for x in ...)` fails: index a list comprehension instead), `match`, `del`, or any third-party package, and it has no filesystem or network: use tools for those. `asyncio.gather` takes no keyword arguments, so there is no `return_exceptions`: wrap a call in try/except inside a helper coroutine to keep one failure from failing the gather. |
| typescript | There is no `require`, no `fetch` and no filesystem: use tools. (Its whole section is different: `tools.read({ path })`, `Promise.all`, `console.log`, `return`.) |

The two `gather`/generator sentences in Monty's notes were added **because of the
live smoke** (§8): each cost Haiku a failed script before it was there.

Tool descriptions (CPython × session; Monty differs only in the label; TypeScript
in syntax):

```text
Run a Python 3 script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. See "Code mode" in the system prompt.
```

TypeScript stubs, as the third provider renders them **[ran]**:

```ts
declare const tools: {
  /** Read the contents of a file at the given path. */
  read(args: { path: string; offset?: number; limit?: number }): Promise<string>;
  /** Execute a shell command via subprocess. */
  bash(args: { command: string; timeout?: number }): Promise<string>;
  …
};
```

## 6. Benchmarks

`experiments/sandbox-providers/bench.py`, Python 3.11.2, 8 cores, shared machine at
load average ~3.7 (other sessions running). A no-op tool (`noop(i)`) through the
real `DirectToolInvoker`, so every call includes schema validation. **[ran]**

| measure | cpython | monty |
|---|---:|---:|
| cold start + first run (ms) | 56.9 | 8.3 |
| warm run, lifetime=script, back-to-back (ms, median of 50) | 31.0 | 0.32 |
| warm run, lifetime=script, 250 ms apart (ms, median of 20) | 4.2 | 0.94 |
| warm run, lifetime=session (ms, median of 50) | 0.15 | 0.12 |
| run with 1 tool call, session (ms, median of 50) | 0.27 | 0.60 |
| 1000 sequential tool calls (ms, median of 5) | 110.6 | 353.2 |
| per sequential call (µs) | 110 | 353 |
| 1000 calls via `asyncio.gather` (ms, median of 5) | 50.4 | 365.6 |

The invoker alone (validation + `execute`, no sandbox) costs 7.8 µs a call.

Reading it:

- **CPython `script` lifetime is a process per run.** The provider keeps one spare
  child spawned (`prewarm`), so with any gap between runs — and a model inference
  is always a gap of seconds — a fresh interpreter costs ~4 ms, the kill and
  reap of the old one included. Back-to-back, the spare is not ready yet and the
  run pays most of a spawn (31 ms). Cold, ~57 ms.
- **Per call, CPython's pipe beats Monty 3×**, and `gather` halves CPython's cost
  again while Monty serializes. Raw Monty on the same machine and load, outside
  this code: 196 µs per call for a sync external function, 358 µs for an async
  one **[ran: probe]**, so the provider adds nothing measurable; the async host
  path is the cost. CODE-MODE.md's 92 µs was a different load; an earlier run here
  at load ~10 gave 457–591 µs, so these numbers move with the machine (gap 9).
- **Neither matters next to a model call.** 1000 calls is 0.1–0.4 s.

## 7. Tests **[ran]**

`tau-agent-core/tests/test_codemode_providers.py` runs one script suite against
both providers (parametrized), `test_codemode_extension.py` covers the tool in a
real `AgentSession` and the prompt rendering.

| Case | cpython | monty |
|---|---|---|
| tool call, print, trailing value | pass | pass |
| two calls via `asyncio.gather` run concurrently (< 550 ms for 2 × 300 ms) | pass | pass |
| tool `ToolError` text reaches the script as a catchable exception | pass | pass |
| non-`ToolError` exception: model reads the internal-error text, not the exception | pass | pass |
| `codemode` is not callable from a script | pass | pass |
| bad argument types fail schema validation | pass | pass |
| timeout on a CPU loop (host kill, < 3 s for 0.5 s) | pass | pass |
| timeout while a tool is running (tool time counts) | pass | pass |
| memory limit (200 MiB) → `sandbox` / `memory` | pass | pass |
| call budget → `sandbox` / `calls` | pass | pass |
| env scrubbed: `TAU_*` and `*_API_KEY` invisible | pass | pass (Monty has no host env) |
| `search_tools` / `describe_tool` | pass | pass |
| lifetime=script forgets | pass | pass |
| lifetime=session persists (variable + function) | pass | pass |
| session reset after timeout | pass | pass |
| session scopes (cursors) are separate | pass | pass |
| lifetime=turn persists in a turn, resets at `user_turn_end` | pass | pass |
| abort signal → `aborted` | pass | pass |
| syntax error → `script` | pass | pass |
| end to end: `AgentSession.prompt` → `codemode` → `read`×2 + `ls` | pass | pass |
| Monty `type_check`: misspelled tool fails with zero calls made | — | pass |

Plus prompt tests (nine renders distinct per lifetime, description independent of
the tool set, overrides, unknown fields raise, TypeScript and `.pyi` stubs,
output cap writes the full text to a file, TypeScript provider refuses to run).
**48 tests, all pass.**

Full repo suite, `pytest -rf --tb=line` from the worktree root: **6494 passed,
157 skipped, 6 deselected, 0 failed** in 563 s. Static gates over the five `src`
trees (`ruff check`, `ruff format --check`, `mypy` in one call,
`strip_comment_blocks.py --check`): clean. `test_packaging.py` needed one line,
mapping the `pydantic_monty` import to the `pydantic-monty` requirement.

## 8. Live smoke: Haiku 4.5 **[ran]**

The local model was down (GPU in use), so per John's instruction the smoke used
`haiku-4.5` (Anthropic API). Five runs, about 50k tokens in total. Task, in a
directory of three `.txt` files and one `.md`: *"Use the codemode tool … list the
.txt files, read each one, count its lines, and find which .txt file contains
'needle'."* Command: `tau -p --mode json --model haiku-4.5 --no-extensions
--no-session --no-context-files -e examples/codemode.py --ext-config
codemode.provider=…`.

| Run | Provider, stubs | Scripts | Valid on attempt | Nested calls (final script) | Tokens in / out (cache r / w) |
|---|---|---:|---|---|---|
| 1 | cpython, snapshot | 1 | 1st | find×1, read×3 (gather) | 6741 / 297 (0 / 0) |
| 2 | monty, snapshot | 3 | 3rd | find×1, read×3 | 7019 / 865 (4151 / 4471) |
| 3 | monty, snapshot, + `gather` note | 2 | 2nd | ls×1, read×3 | 7137 / 714 (0 / 4184) |
| 4 | monty, + generator note | 1 | 1st | find×1, read×3 (gather) | 7134 / 388 (0 / 0) |
| 5 | cpython, stubs=none | 1 | 1st | read×3 | 6175 / 382 (0 / 0) |

What failed, and what it teaches:

- **Run 2.** `asyncio.gather(..., return_exceptions=True)` →
  `NotImplementedError: gather() does not yet support keyword arguments`, twice;
  Haiku then fell back to a sequential loop. The first failed attempt had already
  run `find` and three `read`s: side effects before failure, as CODE-MODE.md
  warned. `type_check` would not have caught it (ty checks against CPython's
  typeshed, where the keyword exists). **[inferred]**
- **Run 3.** `next(gen_expr, None)` → `TypeError: 'list' object is not an
  iterator`: Monty materializes a generator expression as a list.
- **Run 4**, with both notes in the prompt: valid first time, gather used.
- **Run 5**, no stubs: Haiku never called `search_tools`; it guessed `tools.read`
  from the description's example and listed the directory with `os.listdir`,
  which CPython allows. That is the filesystem side channel: those reads did not
  go through a tool, so nothing records them.
- Every Haiku script counted lines with `content.split('\n')` and so reported one
  extra line per file (runs 1, 3, 4, 5); run 2's final script used `splitlines()`
  and was right. A model error, not a harness one.
- τ itself printed "No prompt-cache reads in this turn" in run 3. The section is
  byte-stable across completions (asserted in the test), and the prompt was 4190
  tokens, at the edge of Haiku's cacheable minimum. **[assumed]** Not investigated.

So: Haiku writes valid CPython scripts first time (3/3 runs); against Monty it
needed the subset's sharp edges in the prompt, and wrote a valid script first time
once they were there (1 run). One run each is an anecdote, not a rate.

## 9. What the PoC reaches past

- **`DirectToolInvoker` bypasses hooks.** No `tool_call` veto, no `tool_result`
  rewrite, no `tool_execution_*` events, so a permission gate does not see script
  calls, and heads cannot render them except from `details.calls`. It is one class
  behind `ToolInvoker`; `ctx.execute_tool` (CODE-MODE.md §5.1, building on another
  branch) replaces it.
- **Two private reaches**: `ctx._require_session()._turn_tools()` for the tool
  list, and `session.acting_cursor.id` for scoping. Both want a public `ctx`
  accessor.
- **Tools return text only.** There is no `output_schema` (CODE-MODE.md §5.3), so
  scripts parse prose.

## 10. Gaps and open decisions

1. **A real `{{codemode}}` system-prompt field** (§5.1) instead of a
   `before_agent_start` append. The append is last-wins with any other extension
   that sets `system_prompt` from that hook. **Decision for John.**
2. **`session` lifetime is a hidden channel.** The heap is keyed by cursor, not by
   tree position: navigating to an earlier node or forking keeps state written on
   the abandoned branch. CODE-MODE.md §6.4 allows a persistent interpreter only as
   a replayable cache. The prompt says what persists, honestly, but the tree does
   not. Options: drop the instance on navigate/fork (cheap), or record each
   script and replay with recorded tool results (Cloudflare's design). **Decision.**
3. **Monty limits are per checkout.** In `turn`/`session`, a later run's limits do
   not change Monty's own (the first run's apply); the host deadline still does.
   CPython's `RLIMIT_AS` is likewise fixed at spawn.
4. **CPython's process is the user.** Filesystem and network are reachable
   (run 5). Landlock/seccomp (CODE-MODE.md §6.2) are not wired. A script can also
   write its channel fd directly and forge messages — same trust as `bash`.
5. **CPython drops fd-level output.** A subprocess's or C extension's writes to
   fd 1 go to `/dev/null`; only `print`/`sys.stdout` is captured. stderr is a pipe
   read only when the child dies, so a script flooding fd 2 could block. **[inferred]**
6. **Calls still running when a script ends** are cancelled (CODE-MODE.md §6.5).
7. **Interruption.** Abort kills the interpreter; a sequential tool (`bash`) called
   from parallel script code runs concurrently, because the invoker ignores
   `execution_mode`.
8. **`[dev]` now pulls `pydantic-monty`.** It has wheels for every supported
   Python (CODE-MODE.md §6.2) **[read]**, but the release matrix has not run it.
9. **Monty per-call cost** (§6) is several times the 92 µs in CODE-MODE.md, on a
   machine at load ~10; a quiet re-run is owed before the number is quoted.
10. **The local model is unmeasured** (CODE-MODE.md §8). Haiku is not τ's default.
11. **Docs.** No `docs/` record or `INDEX.md` line yet; this file is the record
    until a decision is taken.
