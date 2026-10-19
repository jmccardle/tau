Experiment record (2026-10-05). Proof of concept, built on a branch; not merged and
not released. It builds the seams `docs/CODE-MODE.md` §5 names, before any
interpreter: nested tool calls, discovery, exposure, typed returns, and output
capture. Every new public name is marked experimental in its docstring.

Reworked 2026-10-06: the declared `tools` block no longer changes on its own.
Loading a deferred tool and masking a tool now leave it byte-identical, and the
prompt cache holds across both (measured, §4). Changing the block takes an
explicit `redeclare_tools`. The RPC wire is now 2.2 and the serve wire 0.9.

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
await session.set_tool_exposure(name, exposure) -> None   # never changes the tools block
session.preview_redeclare(changes: dict[str, bool]) -> ToolBlockChange
await session.redeclare_tools(changes: dict[str, bool]) -> ToolBlockChange  # user action only
session.list_extensions() -> list[ExtensionInfo]
await session.execute_tool(name, args, *, parent_tool_call_id=None) -> NestedToolResult
```

`describe_tools`, `execute_tool`, `list_extensions` and `set_tool_exposure` are
triaged `NOT_EXPOSED` in the RPC capability audit, each with a reason.
`redeclare_tools` and `preview_redeclare` are not on `ctx`, because changing the
block is a user's decision, made through a head.

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
    name, label, description, namespace, exposure, declared, declared_exposure,
    tags, parameters, output_schema, execution_mode
    model_may_call: bool; callable: bool; def stub() -> str

# tau_agent_core.tool_dispatch — deferred tools reached without a block change
tool_search_definition() -> dict       # model-only; returns stubs, declares nothing
call_tool_definition() -> dict         # model-only; call_tool(name, arguments)
search_deferred(descriptions, query, limit=8) -> list[ToolDescription]

# tau_agent_core.prompt_cache — pure, for every head
@dataclass(frozen=True) class ToolBlockChange:
    added, removed, changed: tuple[str, ...]; reordered: bool; invalidates_cache: bool
tool_block_change(before, after) -> ToolBlockChange
@dataclass(frozen=True) class ExtensionInfo: name, path, tools, commands

# tau_agent_core.tool_pipeline
@dataclass(frozen=True) class NestedToolResult:
    tool_call_id, tool_name, value, content, details
    text: str; def as_type(tp) -> Any            # validate value as a TypedDict/dataclass/model
class NestedCallError(ToolError): result: AgentToolResult | None
class ToolCallBlocked(NestedCallError): blocked_by: str
prepare_tool_call(tools, dispatcher, tool_call, *, parent_tool_call_id=None, gate=None)
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
  the serve schema. The RPC protocol goes from 2.1 to 2.2 and the serve protocol
  from 0.8 to 0.9, so a client has a version from which it can rely on the field.
  Serve's hello refuses a mismatch, so a 0.8 client, tau-code included, is refused
  until it moves to 0.9.
- The `tool_call` and `tool_result` hook events carry `parent_tool_call_id` for a
  nested call only. A model-issued call's event is unchanged.
- `ExtensionRunner.emit_tool_result` also patches `value`, and its return value
  gains `patched`, the list of keys a handler set. Two runner tests that compared
  the exact dict were updated.
- `CONFIG_KEYS` gains `tool_exposure` and `tool_declaration`.
- `AgentLoop` takes `call_gate(name) -> reason | None`, which refuses a
  model-issued call before validation. The first branch's `tools_provider`
  (re-reading the declared tools before each completion) is removed.
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

## 4. Exposure, per cursor, in the tree, with a fixed tools block

| Exposure | In the `tools` block by default | Model call | `call_tool` | `ctx.execute_tool` |
|---|---|---|---|---|
| `direct` | yes | yes | yes | yes |
| `model-only` | yes | yes | no | no |
| `callable` | no | refused | refused | yes |
| `deferred` | no | through `call_tool` | yes | yes |
| `hidden` | no | refused | refused | no |

pi calls the third mode `codemode`. τ calls it `callable` because a tool, an
extension or a script can reach it, not only a code-mode script.

**Two separate facts, both in the tree (ran).** Whether the model's `tools` block
declares a tool is fixed by its author's exposure, and only an explicit
`redeclare_tools({name: bool})` writes a `tool_declaration` config entry that
changes it. What a tool may *do* is its effective exposure: the author's default,
unless `set_tool_exposure` wrote a `tool_exposure` config entry. Both are read
with `config_at(entries, leaf)`, so a resume or fork behaves the same and a
branch taken earlier does not. No pending state is kept anywhere.

**Masking keeps the tool declared (ran).** A masked tool stays in the block, so
the block is unchanged. The loop's `call_gate` refuses a model call to it before
validation, with the reason "Tool 'bash' is not available in this conversation:
it was set to 'hidden'. Do not call it again." The model is told in advance by an
appended, persisted `tool_exposure` custom message, visible to the model. When no
turn is running the message is appended at once. During a turn it goes on the
cursor's steer queue, which delivers it after the batch's tool results. Writing
it at once mid-turn would put a user message between a `tool_use` and its
`tool_result`. Test
`test_masking_keeps_the_tools_block_refuses_the_call_and_tells_the_model` covers
four things: the block bytes are equal before and after; a call to the masked
tool is refused; a resume over the same log refuses too; and a cursor placed
before the entry still runs the tool.

**Loading a deferred tool is a message, not a declaration (ran).**
`tool_dispatch` provides two `model-only` tools with fixed descriptions.
`tool_search(query)` returns the matching deferred tools as Python stubs in its
result, which is an appended message. `call_tool(name, arguments)` is declared
from the first request and runs its target through `ctx.execute_tool`, so hooks
and vetoes apply and the call is recorded as a nested call. It refuses a target
that is not `direct` or `deferred`; a `callable` tool is for code, not the model.
A script in code mode calls the target directly.

**Pure warning for heads (ran).** `prompt_cache.tool_block_change(before,
after)` reports added, removed, changed and reordered tools, and whether the
change invalidates the cache. `session.preview_redeclare(changes)` applies it to
a pending redeclaration, so every head can warn before the user confirms.

*Corrected 2026-10-17 (#20, §8):* `preview_redeclare` ignored a name that is not
a tool while `redeclare_tools` raised for it; the preview now raises the same
`ValueError`. Both writers also wrote entries that changed nothing: they compared
override maps rather than effective state, so declaring an already-declared tool,
or setting a tool to its default exposure, appended a config entry (and, for an
exposure, a notice to the model). Each now writes only when the effective
declaration set or exposure changes.

**What pi does (read, corrected).** The first version of this record said pi
"keeps the declared set fixed and appends a message". That is wrong for tool
search. It came from CODE-MODE §5.2, which says it of pi's code-mode tool
description and the MCP server list. pi 1.0's `tool_search`
(`packages/coding-agent/src/extensions/tool-search/tool.ts:209`) calls
`tools.setActiveTools([...active, ...matches])`, which changes the declared set:
its result text says the tools "are available from your next call". pi keeps the
cache only at the provider layer. With Anthropic's mid-conversation tool-changes
beta (`mid-conversation-tool-changes-2026-07-01`, `supportsMidConvoToolChanges`),
`anthropic-messages.ts:1210-1229` keeps the request's `tools` list growing only.
The initial tools carry the breakpoint, every later tool is sent with
`defer_loading: true`, and a placeholder deferred tool is declared from the first
request. The comment there says this is because the first deferred tool otherwise
caused a measured full miss. Without that compat flag, pi sends the current set
and misses. τ's design needs no provider feature.

**Measured on `claude-haiku-4-5` (ran, `probe_cache.py`).** Per completion:
uncached input / cache read / cache write, in tokens.

| Step | As built (cache-safe) | Old behaviour (block changed) |
|---|---|---|
| warm | 3 / 0 / 10685 | 3 / 10685 / 0 |
| load: first completion | 3 / **10685** / 59 (`tool_search`) | 3 / **0** / **10796** (`word_stats` declared) |
| load: next | 6 / 10744 / 150 (`call_tool`) | 5 / 10796 / 87 |
| load: answer | 5 / 10894 / 127 | — |
| plain turn after the mask | 3 / **11021** / 31 | 3 / **0** / **10792** (`bash` withdrawn) |

The old `warm` row read the cache entry the new run had just written, because
the prefix was identical. Each block change re-bills about 10.8k tokens as a
write. At Haiku 4.5's documented rates ($1/MTok input, writes 1.25× for a
5-minute TTL and 2× for 1 hour, reads 0.1×), one miss on this prefix costs
$0.0135 against $0.0011 for the hit it replaced. That is 12.5× per request on
τ's 5-minute TTL, and 20× on a 1-hour TTL. The cost grows linearly with the
prefix, so a 100k-token conversation pays about $0.125 per load or mask, against
$0.01.

**Minimum cacheable prefix (ran).** A 3488-token prompt neither wrote nor read
the cache on two identical requests. A 4565-token prompt wrote 4565 and then
read 4565. The minimum therefore lies between those two figures, which is
consistent with the documented 4096 for Haiku 4.5 and with τ's
`MIN_CACHEABLE_PROMPT_TOKENS = 4096`.

**Unchanged from the first version:** the earlier live run of
`examples/62_tool_exposure.py` (3083 tokens) used the old loading path. The
example now uses `tool_search` and `call_tool`, and the cache probe above
exercises it.

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

*Corrected 2026-10-17 (#20, §8):* as first built, a parameter or property name
that is not a Python identifier (`from`, `my-arg`) was written as-is, so the stub
did not parse; and `render_stubs` dropped only exact-text repeats, so two shapes
sharing a title both emitted `class X`. Both are fixed; §8 says how.

**Two sharp edges (ran).** On Python 3.11, pydantic rejects `typing.TypedDict`
and requires `typing_extensions.TypedDict`; the error is passed through. Under
`from __future__ import annotations`, a type defined inside a function does not
resolve: `get_type_hints` raises `NameError`. Both are probably the first thing
an extension author hits.

*Corrected 2026-10-17 (#20, §8):* the second edge was wider than stated.
`get_type_hints(fn)` resolved *every* annotation, so `output_schema_of` raised
`NameError` for an unresolvable parameter when only the return was wanted, and
`tool_from_function` raised for a `ctx: ExtensionContext` imported under
`TYPE_CHECKING`, though it never uses `ctx`'s type. Now only the annotations
used are evaluated. On 3.14 a type local to the defining function resolves too,
because annotations are read in `FORWARDREF` format; on 3.11–3.13 under the
future import it still does not.

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
  1. ~~Is a load a cache miss, or a message with a fixed declared set?~~ Decided
     2026-10-06: the block never changes on its own (§4).
  2. ~~Does `parent_tool_call_id` get a minor-version bump?~~ Done: RPC 2.2,
     serve 0.9. tau-code must bump its hello to 0.9.
  3. Should an exposure the user set persist across sessions as a preference in
     `config.json`, beside the per-tree entry?
  4. Should `tools` in the config entry keep listing the declared built-ins (as
     built), or be dropped in favour of `tool_declaration`?
  5. Can a nested call reach a tool masked from the model only because the
     model must not see it (`callable`)? As built, yes; it is the point of
     `callable`.
  6. An extension that calls `register_tool` mid-session, or a reload, still
     changes the block at the next prompt. That is a user-visible event (loading
     an extension), but no head yet uses `tool_block_change` to warn about it.
  7. Should `tool_search` and `call_tool` be registered by core whenever any
     deferred tool exists, rather than by an extension? That would change the
     block when the first deferred tool appears.
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
- After the 2026-10-06 rework, the experiment file collects 39 tests. The full
  suite gave 6492 passed, 157 skipped, 6 deselected and 1 failed: the RPC
  capability audit, because `redeclare_tools` and `preview_redeclare` were not
  yet triaged. Once they were triaged, that file and the four generated-doc and
  schema test files passed (87 tests). The full suite was not run again.
- `probe_cache.py`: the cache measurements in §4, run on `claude-haiku-4-5`
  2026-10-06. About 77k prompt tokens, nearly all of them cache reads and
  writes, so under 5 cents.
- Static gates on all five `src` trees: `ruff check` clean, `ruff format
  --check` clean, `mypy` no issues in 140 files, comment-block scan 0. The
  pre-commit hook passed on every commit.
- `probe_stdio.py`: the stdio hazards and the capture overhead, Python 3.11.2.
- Live runs: two `tau -p --mode json -m haiku-4.5 -e examples/62_tool_exposure.py`
  runs, 3083 and 3445 tokens. Session files and raw JSONL stayed in a scratch
  directory and are not committed.

## 8. Corrections from issue #20 (2026-10-17)

The docstring pass of 2026-10-14 wrote each defect below into the docstring as
current behaviour. Each fixed one has a test under "issue #20" in
`test_tool_exposure_experiment.py`; that test failed before the fix.

**Fixed (ran).**

- `preview_redeclare` raises `ValueError` for a name that is not a tool, as
  `redeclare_tools` always did. A preview that accepts what the action refuses
  tells a head the change is fine, and then the change fails.
- `redeclare_tools` writes a `tool_declaration` entry only when the set of
  declared tools changes. `set_tool_exposure` writes only when the tool's
  effective exposure changes, counting its default. Both used to compare
  override maps, so `redeclare_tools({"bash": True})` on a tool declared by
  default, or `set_tool_exposure("bash", "direct")` on a `direct` tool, wrote a
  tree entry. The exposure case also appended a notice telling the model that
  nothing had changed. Setting a tool back to its default after an override
  still writes, because there the effective exposure does change.
- `render_stub` keeps a name that cannot be a Python identifier exactly as it
  is, and does not rename it. A script calls a tool by keyword and the invoker
  passes the keywords on unchanged, so `_from` in the stub would be a name the
  tool rejects. A `TypedDict` with such a key uses the functional form,
  `X = TypedDict("X", {"from": int})`. A tool with such a parameter is rendered
  as `async def f(**kwargs: Unpack[f_arguments])`. A key that class syntax would
  name-mangle (`__x`) counts as such a name. `render_stubs` imports `Unpack`.
- `render_stubs` uses one writer for the whole module. When a different shape
  arrives under a class name already in use, it becomes `Name_2`, and the stub
  that uses it refers to `Name_2`. The renamed class's nested classes are
  rendered again under the new name. A class is still written once. Raising an
  error was the alternative. It loses because pydantic titles are class names,
  so two extensions that each return a `Result` would make the listing fail, and
  the class names exist only in the stub, never at run time.
- `output_schema_of` evaluates only the return annotation, and
  `tool_from_function` evaluates only the annotations it uses: the return and
  every parameter except `ctx`. Both build a function that carries only those
  annotations and the original's globals and type parameters, then call
  `typing.get_type_hints` on it. On 3.14 the raw annotations are read in
  `annotationlib.Format.FORWARDREF` format, because without it reading
  `__annotations__` evaluates all of them. An unresolvable return still raises
  `NameError`.
- `CapturedOutput.write` raises `ValueError` for a stream other than `"stdout"`
  or `"stderr"`, where it used to write it to stderr.
- `ExtensionContext.find_extension` raises `ValueError` naming every path when
  two loaded extensions share a name. Two files with the same stem in different
  extension directories do this, and so do two inline factories with the same
  function name. It used to return the first one silently.

**Left for a decision.**

- *Output-capture proxies stay installed after the first capture.* The proxy
  does nothing outside a capture: a write goes straight to the wrapped stream.
  But it is a process-wide change that an embedder did not ask for. The options:
  (a) keep it as it is, documented. (b) Count the active captures, and put the
  wrapped streams back when the count reaches zero, but only if `sys.stdout` is
  still the proxy. The cost of (b): a task a tool spawned keeps the copied
  context, so a print it makes after the call ends now reaches the real stdout.
  Under `tau -p --mode json` that is a non-JSON line in the event stream, the
  hazard §5 measured. (c) Have the library never install the proxies, and have
  each head install them at startup. An SDK embedder would then get no capture
  unless it asks for it.
- *`search_deferred` matches substrings, so `"a"` matches almost every deferred
  tool.* §3 rejected a ranker on purpose. Since then, code mode's
  `codemode/invoker.py` has built `_Bm25` for its own `search_tools`, so τ has
  two search semantics for one question. The options: (a) match whole tokens.
  `"a"` still matches every description that uses the article. (b) Reuse `_Bm25`
  for `search_deferred`. IDF ranks a word every tool shares last, but a query
  still returns `limit` results whenever that many contain it. (c) Wait for
  CODE-MODE §7's `ToolRanker` and route both callers through it. (b) reverses
  §3's decision, which is why it is not built here.

**Found while fixing, not fixed (read).** All three were fixed on 2026-10-18 as
issues #32, #33 and #34; §9 records how.

- `codemode/prompt.py`'s `python_signature` builds the stubs that code mode puts
  in the model's prompt. It is not `render_stub`, and it **silently drops** a
  parameter whose name is not an identifier. A required `from` disappears from
  the signature the model reads. This is the same defect as the `render_stub`
  one above, with a worse result, in the renderer code mode actually uses. The
  fix is either a second copy of the treatment above or one renderer for both.
  Which one is a code-mode design question.
- `render_stub` still runs the *tool* name through `_identifier`. A tool named
  `my-tool` is stubbed as `my_tool`, which `call_tool(name=...)` would not find.
- A `$ref` that names no entry in `$defs` renders as a bare class name that is
  never defined, so the stub raises `NameError` when it is executed.

**Evidence (ran).** Nine new tests, all failing before the fixes; the experiment
file collects 48. The packaging gate flagged `annotationlib` as an undeclared
third-party module, because it reads the 3.11 `sys.stdlib_module_names`; it now
carries a `LATER_STDLIB` table. The annotation path was checked by hand on
3.14.8. Full suite on Python 3.11.2: 6978 passed, 159 skipped, 6 deselected,
3 xfailed. The four static gates pass on all five `src` trees.

## 9. Stub names and references, issues #32–#34 (2026-10-18)

Each fix has a test, under "issues #32/#33" in `test_codemode_providers.py` or
"issues #33, #34" in `test_tool_exposure_experiment.py`. The first nine
failed against the code before the fix (ran).

- **One renderer (#32).** Code mode's prompt, its `ty` stub file and its
  `describe_tool` all render through `tool_schema.StubNamespace`, which
  `render_stub` and `render_stubs` also use. The old `python_signature`,
  which dropped a parameter such as `from`, is gone. Code mode passes
  `keyword_only=True` and renders `-> str`, because a script receives a tool's
  text. For the seven core tools the prompt's stub block is byte-identical to
  before (ran, diffed). Two things change on purpose. A tool whose parameters
  include an object with properties now gets a `TypedDict` class above
  `class tools:` where it used to read `dict[str, Any]`. An `enum` renders as
  `Literal['a']`, where `json.dumps` used to give `Literal["a"]`, and
  `Literal[true]`, which is not valid Python. The block stays a pure function of
  the tool set, so the cache holds for an unchanged set.
- **Stub names (#33).** `stub_name` makes a tool name an identifier
  (`my-tool` → `my_tool`, `from` → `from_`), and `stub_names` maps a set of
  them back, raising `ValueError` when two tools share one. The dispatchers
  accept the stub name through that mapping, rather than the stub carrying the
  real name. A code-mode script has no syntax for `tools.my-tool`, so code mode
  needed the mapping whichever option was chosen; using it for `call_tool` too
  keeps one rule. A tool whose stub name another tool shares, hidden ones
  included, is a configuration fault: `tool_search` refuses to list it and
  `call_tool` refuses it by real name or stub name, each naming every tool in
  the clash. A clash among tools a call does not touch does not block it.
  The check runs per call, not at registration, because two such names are
  legal in the declared tools block and only the stub surfaces confuse them
  (revised in review, 2026-10-18; it first refused every search while any
  clash existed, and `call_tool` skipped the check for a real name). The
  code-mode invoker raises at construction on a collision, and it no longer
  leaves out a tool whose name is not an identifier. Monty refuses an
  attribute that starts with `_` (ran), so a stub name never gains one: a
  keyword gains a trailing `_`, and a result that cannot start an identifier
  (`1tool`) or starts with `_` when the real name did not (`-x`) gains `t_`.
  A real `t_1tool` beside `1tool` is a collision and raises.
  Only a tool whose real name starts with `_` still stubs that way. The Monty
  provider raises `ValueError` naming such a tool when it builds the prompt or
  runs a script, in either stub mode, rather than listing a tool it cannot call.
  `check_stubs` is an optional provider hook (a no-op on `ScopedProvider`, absent
  from the `SandboxProvider` protocol), so an out-of-tree provider need not
  define it. The CPython child
  now refuses only dunders, so it calls such a tool.
- **Unresolved `$ref` (#34).** A `$ref` is an RFC 6901 pointer into the
  schema that holds it (the parameters, or the output schema), with `~0` and
  `~1` decoded, so `#/$defs/X`, draft-07 `#/definitions/X`,
  zod-to-json-schema's `#/properties/src` and `#` all resolve. The first build
  resolved only the last token against `$defs` and raised on the other three,
  which would have stopped code mode's prompt for one such MCP tool (found in
  review). A target object with properties is a class, so a recursive
  reference names it; any other target renders inline, and a cycle through
  one renders `Any` at the cycle. A `$defs` enum now renders as its `Literal`,
  where it used to be an empty `TypedDict`. A pointer that does not resolve,
  or one that is not local (`other.json#/x`, `#anchor`), raises `ValueError`
  naming the tool and the `$ref`. Code mode raises the same error, because it
  renders through the same writer.

**One bad schema no longer turns code mode off (#55, 2026-10-19).** The
renderer still raises, now as `tool_schema.StubError` (a `ValueError` carrying
`tool` and `reason`), and the `render_stub` tests above still test that. One
level up, `CodeMode` renders each candidate tool's stub on its own
(`codemode.prompt.stub_failures`) and leaves out each one that fails. The
section, the script namespace and `describe_tool` never see it, and every
other tool works. The derivation is in `tau_agent_core`. `CodeMode.section`
queues one notice per left-out tool, "tool 'X' is unavailable in code mode:
$ref '…' is not a local '#/...' pointer" (or "does not resolve (no 'Y' in the
schema)"). The `before_agent_start` handler sends each through
`ctx.ui.notify(..., "warning")`, the extension notice path every head already
renders: a toast in the TUI, an `extension`/`notify` record under
`tau -p --mode json`, a stderr line under `tau -p` text and under
`tau --mode rpc` (its stderr contract, item 3), and a `ui` event from the serve
daemon. The notice goes out once, when the section is built. The exclusion is
a pure function of the tool set, so the section stays byte-stable.
`tool_search` lists a match whose stub fails as
`# tool 'X' is unavailable: <reason>` and reports it in
`details["unavailable"]`. `call_tool` refuses such a tool by any name, with
the same reason, because the model can only have learned of it from that
listing. A refusal for exposure now names the real tool when the call used a
stub name: `'my_inner' (tool 'my-inner') is not available to you`.

**Left open (read).** In Monty's `type_check` mode, `ty` cannot check
`tools.my_tool(**{"from": 2})`, because it types the dict literal as
`dict[str, int]`, not as the `TypedDict`. Annotating the dict works:
`args: "my_tool_arguments" = {...}` type-checks and runs (ran). The prompt does
not say so. TypeScript stubs now quote a key that is not an identifier, but
the TypeScript provider has no runtime, so this is unrun. A recursive class
stub (`class T(TypedDict): kids: list[T]`) reads correctly but raises
`NameError` if executed, because the class body names itself; master did the
same for a recursive `$defs` entry.
