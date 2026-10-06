Experiment record (2026-10-05). Proof of concept, built on a branch; not merged and
not released. It builds the seams `docs/CODE-MODE.md` §5 names, before any
interpreter: nested tool calls, discovery, exposure, typed returns, and output
capture. Every new public name is marked experimental in its docstring.

Each claim below is marked **ran** (measured or tested here), **read** (from the
code), or **inferred**.

# Tool exposure, nested calls and typed returns

## 1. The API as built

### On `ExtensionContext` (what a tool or hook handler receives as `ctx`)

```python
ctx.list_tools(pattern=None, *, namespace=None, tag=None, exposure=None,
               include_hidden=False) -> list[ToolDescription]
ctx.describe_tool(name) -> ToolDescription                  # KeyError if absent
await ctx.execute_tool(name, args=None, *, parent_tool_call_id=None) -> NestedToolResult
await ctx.set_tool_exposure(name, exposure) -> None
ctx.list_extensions(pattern=None) -> list[ExtensionInfo]
ctx.find_extension(name) -> ExtensionInfo | None
```

`pattern` and `namespace` are `fnmatch` globs (`"git_*"`, `"mcp.*"`).

### On `AgentSession` (the same operations; `ctx` delegates here)

```python
session.describe_tools(*, include_hidden=False) -> list[ToolDescription]
await session.set_tool_exposure(name, exposure) -> None
session.list_extensions() -> list[ExtensionInfo]
await session.execute_tool(name, args, *, parent_tool_call_id=None) -> NestedToolResult
```

All four are triaged `NOT_EXPOSED` in the RPC capability audit, each with a reason.

### Types

```python
# tau_llm.tools — four new ToolDefinition fields, none sent to the model
ToolExposure = Literal["direct", "model-only", "callable", "deferred", "hidden"]
ToolDefinition.exposure: ToolExposure = "direct"
ToolDefinition.namespace: str | None = None      # "core" | "<ext>" | "<ext>.<sub>"
ToolDefinition.tags: list[str] = []
ToolDefinition.output_schema: dict | None = None

# tau_agent_core.tools.base
AgentToolResult.value: Any = None                # structured value; never sent, never stored

# tau_agent_core.tool_catalog
@dataclass(frozen=True) class ToolDescription:
    name, label, description, namespace, exposure, declared_exposure,
    tags, parameters, output_schema, execution_mode
    declared_to_model: bool; callable: bool; def stub() -> str
@dataclass(frozen=True) class ExtensionInfo: name, path, tools, commands

# tau_agent_core.tool_pipeline
@dataclass(frozen=True) class NestedToolResult:
    tool_call_id, tool_name, value, content, details
    text: str; def as_type(tp) -> Any            # validate value as a TypedDict/dataclass/model
class NestedCallError(ToolError): result: AgentToolResult | None
class ToolCallBlocked(NestedCallError): blocked_by: str
prepare_tool_call(tools, dispatcher, tool_call, *, parent_tool_call_id=None)
execute_prepared_call(tools, call, signal) -> AgentToolResult
apply_tool_result_hooks(dispatcher, result, input_args, *, parent_tool_call_id=None)
run_nested_call(*, tools, dispatcher, emit, signal, name, args, parent_tool_call_id=None)

# tau_agent_core.tool_schema
output_schema_for(tp) -> dict
output_schema_of(fn) -> dict | None
tool_from_function(fn, *, name=None, description=None, label=None, render=None,
                   exposure="direct", tags=None, execution_mode="parallel") -> dict
render_stub(name, description, parameters, output_schema) -> str
render_stubs(descriptions) -> str

# tau_agent_core.tool_output
capture_tool_output() -> ContextManager[CapturedOutput]
suspend_capture() -> ContextManager[None]

# typed built-ins
BashTool.run(command, timeout=None, *, signal=None) -> BashResult
    # BashResult: output, stdout, stderr, exit_code, truncated, timed_out, aborted
GrepTool.run(pattern, path=".", *, files=None, ignore_case=False, signal=None) -> GrepResult
    # GrepResult: matches: list[GrepMatch{path, line, text}], files_searched
```

### Changes to existing contracts

- `AgentEvent.parent_tool_call_id` and the same field on the RPC `WireEvent` and
  the serve schema. Additive, with no protocol version bump (§6).
- The `tool_call` and `tool_result` hook events carry `parent_tool_call_id` for a
  nested call only. A model-issued call's event is unchanged.
- `ExtensionRunner.emit_tool_result` also patches `value`, and its return value
  gains `patched`, the list of keys a handler set. Two runner tests that compared
  the exact dict were updated.
- `CONFIG_KEYS` gains `tool_exposure`.
- `AgentLoop` takes `tools_provider`, which it calls before each completion.
- `BlockedCall` and `ErrorCall` moved to `tool_pipeline`. `agent_loop` still
  exports both names.
- `bash` runs with `stdin=/dev/null` and no longer writes an unused temp file
  (§5).
- `tau-agent-core` declares `typing_extensions>=4.6`. It was already installed,
  because pydantic depends on it.

## 2. Nested tool calls

**One pipeline (ran).** `AgentLoop._prepare_tool_call`, `_execute_tool` and
`_apply_after_hooks` now delegate to three module functions in `tool_pipeline`.
`run_nested_call` calls the same three functions. A permission gate written as a
`tool_call` hook therefore covers a nested call with no change, and the test
`test_a_vetoed_nested_call_raises_tool_call_blocked_in_the_caller` checks that.
`agent_loop.py` lost 213 lines and gained 20 (`git diff --numstat`).

**Parent identity is a context variable (ran).** `execute_prepared_call` sets
`CURRENT_TOOL_CALL` to a `ToolCallFrame` while a tool runs, so `ctx.execute_tool`
needs no id argument. A nested call's id is `<parent id>/<n>`. The live run
produced `toolu_019u…PmQ/1`. Two parents running in parallel each record only
their own children, because each `asyncio.gather` task copies the context (test
`test_parallel_parents_each_record_only_their_own_nested_calls`). An explicit
`parent_tool_call_id` is accepted only outside a running tool. That is the hook
for a sandbox host acting on a script's behalf. Inside a tool, a contradicting
value raises `ValueError`.

**Recorded on the parent, not written as messages (ran).** A nested call's result
is not a `toolResult` entry, because the model never issued the call. The
parent's `details["nested_calls"]` holds one entry per call: `tool_call_id`,
`tool_name`, `status` (`ok`, `error`, `blocked` or `unavailable`),
`duration_ms`, `args`, and `error`/`blocked_by` when they apply. pi's caps carry
over: 256 entries, then a `nested_calls_omitted` count, and 32 KiB of argument
JSON per parent, after which an entry carries `args_omitted_bytes` in place of
`args`.

**Failure is an exception in the caller (ran).** A veto raises `ToolCallBlocked`.
A tool that is unknown, unreachable or invalid, or that reported `is_error`,
raises `NestedCallError` with the tool's own text. A nested `ToolHalt` raises
`ToolHalt`. All three subclass `ToolError`, so a tool that lets one propagate
reports that text to the model as its own failure. Recursion stops at depth 8
(`MAX_NESTING_DEPTH`).

**Events (ran).** `tool_execution_start` and `tool_execution_end` are emitted for
a nested call with `parent_tool_call_id` set, on the same bus with the same
stamping. The live run's JSONL showed the order parent start, child start, child
end, parent end.

**Rejected:** handing out a tool's `execute`. `get_all_tools()` already withheld
it, and that was right. Any callable reached through discovery would bypass the
hooks.

## 3. Discovery

**What is public (decided here).** Metadata for every tool on the asking cursor
that is not `hidden`, and `hidden` ones when asked for explicitly. A sub-agent
cursor's `TurnFrame.tools` allowlist applies, so a sub-agent cannot discover its
way out of its grant. Extensions are listed with their name, path, tool names
and qualified command names. Nothing returned is executable.

**Namespace is assigned, not claimed (ran).** A built-in gets `"core"`. An
extension tool gets the extension's name (`extension_owner(path)`), or
`<name>.<sub>` if it asks for one. That shape is for one MCP extension that
fronts several servers (CODE-MODE §7). Asking for `"core"` or another
extension's name raises at `register_tool`. A tool registered on an api bound to
no extension lands in `"extension"`.

**Rejected:** a ranker. `match_tools` filters by glob, namespace, tag and
exposure. BM25 or JMFTS ranking is CODE-MODE §7's `ToolRanker`, and it can rank
these same descriptions.

## 4. Exposure, per cursor, in the tree

| Exposure | Offered to the model | `ctx.execute_tool` |
|---|---|---|
| `direct` | yes | yes |
| `model-only` | yes | no |
| `callable` | no | yes |
| `deferred` | no, until loaded | yes |
| `hidden` | no | no |

pi calls the third mode `codemode`. τ calls it `callable` because a tool, an
extension or a script can reach it, not only a code-mode script.

**The active set lives in the tree (ran).** `set_tool_exposure(name, exposure)`
appends a `config` entry `{"tool_exposure": {...the whole map...}}` at the acting
cursor's leaf. Effective exposure is that tool's entry in
`config_at(entries, leaf)["tool_exposure"]`, or its declared default. No pending
state is kept anywhere, because the tree is the only store. Test
`test_masking_a_built_in_is_recorded_and_survives_resume_but_not_an_earlier_branch`
masks `bash` and checks three things: the next turn is not offered `bash`; a
second `AgentSession` over the same log (a resume) is not offered it either; and
a cursor at the leaf before the entry is still offered it. Built-ins are masked
like any other tool.

**Mid-turn loading (ran, live).** `AgentLoop` re-reads its declared tools through
`tools_provider` before every completion. A `tool_search` that loads a deferred
tool therefore makes it callable on the model's very next step. On
`claude-haiku-4-5` (`examples/62_tool_exposure.py`), the model called
`tool_search("text")`. That loaded `word_stats` and returned its stub. The model
called `word_stats` in the same turn: 3083 tokens in total.

**Prompt caching (read, inferred).** Descriptions never change. `tool_search`'s
description names no tools, and the declared list keeps registry order, so an
unchanged set serializes the same way (test
`test_an_unchanged_tool_set_serializes_identically_across_turns`). A load or a
mask does change the `tools` block, and every provider renders `tools` before
`system` (`PROMPT-CACHING.md`). So each load costs one full cache miss, paid at
the completion after the load. pi avoids the miss by keeping the declared set
fixed and appending a message. The trade is still open (§6).

## 5. Typed returns, and output capture

**Schema from the annotation (ran).** `output_schema_of(fn)` is pydantic's
`TypeAdapter(return_annotation).json_schema()`. It covers `TypedDict`,
dataclasses, pydantic models, primitives and containers (5 parametrized cases).
`bash` and `grep` were split into a typed `run()` and an `execute()` that renders
`run()`'s value as the same text as before. `_resolve_tools` derives each
built-in's `output_schema` from `run`. `tool_from_function` builds a whole
extension tool from one annotated function. Parameters come from the signature
via `create_model`, the output schema from the return annotation, `ctx` is
injected by name, and the model reads `render(value)` or compact JSON. A missing
annotation or description raises.

**The value reaches nested callers only (ran).** `value` rides `AgentToolResult`
and `NestedToolResult`. It is never put in a `toolResult` message or sent to the
model. **Decided:** a `tool_result` hook that rewrites `content` without
rewriting `value` sets `value` to `None`. Otherwise a redaction hook would hide a
secret from the model and pass it intact to the nested caller (test
`test_a_hook_that_rewrites_content_drops_the_stale_value`).

**Stubs from the registry (ran).** `render_stub` works from JSON Schema, not from
Python objects, so it also covers hand-written schemas and, later, MCP tools.
`TypedDict` classes are emitted before the classes that use them, and the output
runs under `exec` (tested):

```python
async def grep(pattern: str, path: str | None = None, files: list[str] | None = None,
               ignore_case: bool | None = None) -> GrepResult:
```

Optional parameters render as `X | None = None`, because the schema states no
default. The seven built-ins come to 2341 characters as stubs, against 3820 as
name, description and parameters JSON. The stubs carry return types; the JSON
does not.

**Two sharp edges (ran).** On Python 3.11, pydantic rejects `typing.TypedDict`
and requires `typing_extensions.TypedDict`; the error is passed through. Under
`from __future__ import annotations`, a type defined inside a function does not
resolve: `get_type_hints` raises `NameError`. Both are probably the first thing
an extension author hits.

**Who writes to stdio (read, ran).** No built-in tool prints. Of the examples,
`34_desktop_notify` writes escape sequences to `sys.stdout` from a hook, and
`ExtensionRunner._emit_error` prints to stderr when nothing listens. Two hazards
were measured with `probe_stdio.py`:

| Hazard | Before | After |
|---|---|---|
| A `print` in an extension tool under `tau -p --mode json` | 1 non-JSON line in 14 | 0; the text is in `details["captured_output"]` |
| `bash` running `cat` under a parent whose stdin is a protocol pipe (the `tau --mode rpc` arrangement) | the child read `{"type": "prompt", …}` off the parent's stdin | empty: stdin is `/dev/null` |

`tau --mode rpc` was already safe from `print`, because the transport rebinds
`sys.stdout` to stderr (read). It was not safe from the stdin inheritance.

**The capture policy (ran).** A proxy is installed once over
`sys.stdout`/`sys.stderr`. Each write goes to the buffer in a context variable,
if the running context has one, and to the wrapped stream otherwise. The proxy is
re-installed if a head rebinds the streams. `execute_prepared_call` opens a
buffer per tool call, and the text lands in `details["captured_output"]`, capped
at 8192 characters per stream with a `dropped` count. The cost: 5.4 µs per tool
call that prints nothing, about 0.6 µs extra per `print` outside any tool, and
about 3.3 ms extra per 1000 captured lines. The proxy delegates `isatty`,
`fileno` and `encoding`, so Rich and Textual detect the terminal as before.

**The capture's own failure, found live (ran).** The first live run showed no
nested events in the JSONL. The `--mode json` writer is a synchronous bus
subscriber, so it ran inside the calling tool's context, and the capture
swallowed the head's own event lines. `run_nested_call` now emits under
`suspend_capture()`, and `test_json_mode_tool_prints.py` pins the order of the
four events. The general rule this implies: **a protocol writer holds its stream
from the start and never looks up `sys.stdout` per write**, as the RPC
transport already does. The headless JSON writer does not do this yet.

The capture covers Python-level writes only. A subprocess that inherits fd 1,
or C code that writes to fd 1, bypasses it. For that the rule is per spawner.

## 6. What remains

- **Headless loses tree writes made from tools (ran live, read).** `tau -p
  --continue` did not keep `word_stats` loaded: the model's direct call returned
  `Unknown tool`, and it searched again. The cause: `tau -p` runs the
  `AgentSession` on a scratch `InMemorySessionLog` and copies only messages to the
  store (`backends.py:1452`, `headless.py:896`). The same must be true of every
  `api.append_entry` made during a `tau -p` turn (inferred, not run). The TUI binds
  its cursor and is not affected (read). This bug predates the experiment and is
  separate from it.
- **Open decisions.**
  1. Is a load a cache miss (as built), or a message with a fixed declared set
     (pi)?
  2. Does `parent_tool_call_id` on the wire get an RPC/serve minor-version bump?
     E3 allows additive fields; tau-code ignores unknown keys.
  3. Should an exposure the user set persist across sessions as a preference in
     `config.json`, beside the per-tree entry?
  4. Should `tools` in the config entry keep listing `self._tools` filtered by
     exposure (as built) or be dropped in favour of `tool_exposure`?
  5. Can a nested call reach a tool masked from the model only because the
     model must not see it (`callable`)? As built, yes; it is the point of
     `callable`.
- **Not built:** `on_update` for nested calls (still never passed, CODE-MODE §4);
  a core-side reading that renders `nested_calls` for all three heads; masked
  tools still appear in the system prompt's tool list, which is built once at
  session creation; a `ToolRanker`; the interpreter itself.
- **Measured nowhere:** whether a small local model uses `tool_search` reliably.
  The live run used `claude-haiku-4-5`, because `local-llm` was down.

## 7. Evidence

- Tests: `tau-agent-core/tests/test_tool_exposure_experiment.py` (34) and
  `tau-coding-agent/tests/test_json_mode_tool_prints.py` (1).
- Full suite (`pytest -rf --tb=line`, Python 3.11.2): the baseline at `7303954`
  was 6446 passed, 157 skipped, 6 deselected. The branch gave 6487 passed, 157
  skipped, 6 deselected and 1 failed. The failure was
  `test_checked_in_reference_is_current`, because `suspend_capture` was added
  after the reference was regenerated. After regenerating, that file passed
  (17/17), and the suite was not run again in full. The branch adds 35 new
  tests, and 7 example-contract cases are collected for `62_tool_exposure.py`.
  That is 42 against a measured delta of 41; the one-test difference was not
  traced.
- Static gates on all five `src` trees: `ruff check` clean, `ruff format
  --check` clean, `mypy` no issues in 140 files, comment-block scan 0. The
  pre-commit hook passed on every commit.
- `probe_stdio.py`: the stdio hazards and the capture overhead, Python 3.11.2.
- Live runs: two `tau -p --mode json -m haiku-4.5 -e examples/62_tool_exposure.py`
  runs, 3083 and 3445 tokens. Session files and raw JSONL stayed in a scratch
  directory and are not committed.
