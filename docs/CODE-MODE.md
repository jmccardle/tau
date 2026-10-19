# Code mode: the model writes a script, the tools run inside it

Research record (2026-10-05), nothing built. This is a candidate centrepiece for
0.13.0, not yet its committed target. It defines "code mode" as a range of
features, prices each one against τ as it is, and names the seams it would need.

## 1. What is wrong, measured

Three mechanisms produce the savings that code mode claims. They need different
machinery, so each one is measured separately here.

**Declaration cost is small in τ today.** The seven built-in tools serialize to
4044 characters of OpenAI function JSON, about 1011 tokens at four characters per
token (`create_all_tools`, 2026-10-05). The three `tau-jmfts` tools add about 500
tokens, and an example extension tool averages about 145 tokens. Compare the
figures the sources give for MCP: 13.7k tokens for Playwright MCP, ~55k for the
GitHub MCP server, and Anthropic's 150,000 → 2,000. τ has no MCP client, so it
does not have this problem yet. It gets the problem when it gets MCP.

**Intermediate results are most of what τ stores.** Across the 100 sessions under
`~/.tau/sessions` on the development machine, `toolResult` messages hold 76.5%
of message-content characters (16.9M of 22.1M). The distribution is set by `grep`:
57 calls, median 1004 characters, p90 108,292, max 3,318,255, 10.9M in total.
`read` is next: 285 calls, median 3879, p90 19,886. A result stays on the path,
so it is sent again on every later request until compaction removes it. These are
stored sizes. They measure what enters the tree, not what one provider bills.

**Round trips are many.** In the same sessions, a run of consecutive
tool-calling completions, ended by a completion with no tool call, has median
length 4 and p90 33 (n = 124). Each completion in such a run re-reads the whole
prefix, and the model is idle while the tools run.

So for τ the case for code mode is the second and third mechanisms: keeping
results out of the context, and collapsing a chain of calls into one script. The
first mechanism only becomes τ's problem when it gains MCP (§7).

## 2. Code mode is a range, not one feature

The sources (Cloudflare 2025-09-26, Anthropic 2025-11-04, FastMCP's `CodeMode`
transform, UTCP `code-mode`, Livshits 2026-05-15, Earendil 2026-09-29, pi 1.0.2)
use the name for six cumulative levels:

| Level | The model gets | Example |
|---|---|---|
| 0 | Every tool schema, every turn | The common MCP client |
| 1 | Tools withheld until a search loads them | pi `deferred` + `tool_search` |
| 2 | A shell and self-documenting CLIs | pi before 1.0; `gh --help` |
| 3 | One execute tool, whole typed API in its description | Cloudflare as published |
| 4 | One execute tool, with discovery calls inside the runtime | FastMCP, Anthropic, UTCP, pi `searchTools`, runline |
| 5 | State that outlives one script | pi `store`/`load`; Anthropic `./skills` |
| 6 | Harness abilities other than tools, reachable from scripts | pi `models.classify`; Anthropic PII tokenizing |

Level 1 removes declaration cost and needs no interpreter. Levels 3–6 each need
one. Every level-4 implementation converged on the same three operations —
search, describe, execute — and ranks tools with BM25 or something close
(FastMCP, pi, runline's MiniSearch).

Livshits states the trade the vendor posts omit. Tool declarations are paid in
context on every turn. Code mode is paid in inferences, at least one extra per
namespace the model has not used yet, and it loses the first-shot accuracy that
fine-tuned tool calling has. His rule is per capability: declare the hot path,
put the long tail behind discovery. pi 1.0 is built that way, with five exposure
modes (§5.2).

## 3. What a sandbox is for in τ

τ gives the model `bash`, which runs arbitrary code with the user's privileges.
A code-mode interpreter is therefore not a security boundary against the model,
whatever it is built from. Earendil draws the same line for pi: the interpreter
runs on the harness side, which is trusted, and bash runs on the other.

What the interpreter buys τ:

1. **A scoped bridge.** The script reaches only the tools it was given, and every
   call passes through the same hooks as a model-issued call, so a permission
   gate written as a `tool_call` hook covers script calls with no new code.
2. **Credentials stay in the harness.** API keys and session internals are not
   names a script can read.
3. **Results stay out of the context.** Only what the script prints or returns
   reaches the model (§1, the second mechanism).
4. **Resource limits** on time, memory and call count.

A design that claims more than these four is claiming a boundary bash already
removed.

## 4. Where τ stands

| Seam | State |
|---|---|
| Output schema on a tool | Absent (`tau_llm/tools.py:103-110`) |
| Exposure modes, namespaces, tags, annotations | Absent |
| Active tool set | Extension tools only; built-ins are not maskable; held in memory and not written to the tree (`extensions/registry.py:144`) |
| Per-cursor tool list | Exists: `TurnFrame.tools` (`cursor.py:36-60`) |
| A tool calling another tool through the pipeline | Absent. Validation and hooks exist only inside `AgentLoop._prepare_tool_call` and `_apply_after_hooks` |
| Calling another extension's tool | Absent. `get_all_tools()` returns `ToolInfo` without `execute` |
| `on_update` during execution | Declared, never passed: `_execute_tool` calls `tool.execute(tool_call_id=, args=, signal=)` (`agent_loop.py:1519`) |
| State read by ancestry | Exists: custom entries on the cursor's path |
| JMFTS hybrid and BM25 search, scoped by `parent_id` and `usetype` | Exists (`tau_jmfts/client.py:567`) |
| A `customEntry` searchable in JMFTS | No: its content projection is `""` (`tau_jmfts/store.py:130`) |
| Any interpreter or sandbox | None |

The active set has a second defect besides its scope. Because it is not in the
tree, a resumed session can declare a different tool set than the one its
entries were written under. That breaks the tree-as-truth rule
(`EXTENSIONS-ORCHESTRATION-PLAN.md` §4), and tool search would make it visible: a
tool loaded by a search would vanish on reload. pi records such loads in the
transcript for this reason.

## 5. The seams, before any interpreter

These are worth building whether or not a sandbox follows. Each delivers
something on its own.

### 5.1 A nested tool call through the pipeline

`ctx.execute_tool(name, args)` runs validate → `tool_call` → execute →
`tool_result`, the same path as a model-issued call, and carries a
`parent_tool_call_id`. The parent's result records its nested calls (name,
arguments up to a size cap, status, duration), as pi's `nestedCalls` does with
limits of 256 calls and 32 KiB of arguments.

On its own this lets one extension call another extension's tool, with the hooks
applied. Cost: split `_prepare_tool_call` and `_apply_after_hooks` out of the
loop so `ctx` reaches them; a parent id on `tool_execution_*` events on the RPC
and serve wires, which is a minor-version change; and a core-side reading of the
nested calls that all three heads render.

### 5.2 Exposure, namespaces and tags on the definition

Add `exposure`, `namespace` (name, description, instructions), `tags` and
`output_schema` to `ToolDefinition`. pi's five modes are a working vocabulary:

| Exposure | Declared to the model | Callable from a script |
|---|---|---|
| `direct` | yes | yes |
| `model-only` | yes | never — the execute tool itself, and tool search |
| `codemode` | no; found by in-script discovery | yes |
| `deferred` | after a tool search loads it | yes |
| `hidden` | no | no |

`model-only` is what stops a script from starting a script or changing what the
model sees. Write active-set changes as `config` entries, the way per-cursor
config already works (`CURSORS.md`), so a resume declares what was declared, and
let built-ins be masked like any other tool.

Cost: small, with one risk. A tool description that changes as tools are
registered invalidates the prompt cache (`PROMPT-CACHING.md`). pi keeps the
execute tool's description fixed and appends a changed server list to the
conversation as a message instead.

### 5.3 Structured results

A tool with an `output_schema` returns a value to scripts and text to the model.
`bash` in pi resolves to `{output, truncated, full_output_path, exit_code,
wall_time_seconds}` with up to 1 MiB of output, while the model's own view of it
stays capped. Without structured results a script parses prose, which is the
composition problem Earendil attributes to MCP servers.

## 6. The interpreter

### 6.1 A provider protocol

One protocol, `run(code, *, tools, inputs, limits) -> Result`, as FastMCP's
`SandboxProvider` has, so the choice of interpreter is configuration rather than
design. The candidates and their costs are in §6.2.

### 6.2 Candidates

| Candidate | Language | Isolation | Notes |
|---|---|---|---|
| Monty (`pydantic-monty` 1.1.0) | A Python subset, interpreted by a Rust VM | Worker subprocess pool | Async external functions, allow-listed host objects, REPL sessions, serializable snapshots, bundled `ty`. FastMCP's default. |
| CPython subprocess, tool calls over a pipe | Full Python and its installed packages | An OS process, the same trust as `bash` | No new dependency. |
| `exec` in-process with restricted globals | Full Python | None; escapable through the object graph | Rejected as a boundary; usable only as a guardrail. |
| Subinterpreter (`concurrent.interpreters`, 3.14) | Full Python | None that matters | Rejected; see below. |

Measured 2026-10-05 (probe scripts kept outside the repo):

- **`exec` with restricted globals escapes on every supported version.** On
  3.11–3.15rc3, a script that is handed any host function reads its
  `__globals__`, and any generator's `gi_frame.f_back` walks to the host's frames.
  An audit hook (PEP 578) is reachable the same way and was switched off from
  inside the script. It is useful for logging, not enforcement.
- **A subinterpreter is not an isolation unit for this.** It shares
  `os.environ` with the host. Its `Queue.get` polls with a 10 ms sleep, so a
  tool round trip took 10.2 ms median, against ~35 µs over a pipe to a child
  process. `pydantic_core`, and therefore τ's provider stack, cannot load in one.
  `interp.call(host_function)` re-imports the host's `__main__` inside it. The
  Python docs say not to use subinterpreters where isolation matters.
- **The CPython subprocess design** is `sys.executable -I -X disable-remote-debug`
  with an environment scrubbed of keys, a JSON-RPC channel on its own file
  descriptor as the only bridge, and limits applied per process (`RLIMIT_AS`,
  `RLIMIT_CPU`, a host-side `SIGKILL` at the deadline). `sys.settrace` missed a
  0.2 s deadline by 2.7 s inside C code, so a timeout must kill the process. On
  Linux, Landlock and seccomp both worked unprivileged as optional hardening; a
  head reports whether they are active, never assumes it.
- **Monty 1.1.0** costs 0.075 ms per warm checkout and ~92 µs per host call.
  Wheels exist for every platform and Python version τ supports, so it fits as an
  extra pinned `>=1.1,<1.2`. Its subset has no inheritance (so no custom
  exceptions), no generators, no `match`, no `del`, and no third-party packages.
  Three defaults must be overridden: `print` writes to the host's stdout unless a
  collector is passed; a memory-limit trip poisons the session, which must then be
  discarded; and `max_suspensions` caps a checkout at 1000 host calls in total.
  A probe dumped a paused script to 2623 bytes and resumed it in another process.

Python as the script language suits τ in three ways the JavaScript
implementations cannot copy. Tool schemas render as Python signatures, and τ's
extension tools are already Python. A script can be type-checked against those
signatures before any tool runs, so a misspelled tool fails with no side effects —
pi documents the opposite: "calls made before a failure are not undone". And a
paused script is something Monty can serialize, which raises the possibility of a
script that stops at a permission request and resumes from the tree.

### 6.3 What the script sees

- `tools.<name>(**args)` — async; resolves to the tool's structured value, or its
  text; raises with the tool's error text on failure. Built 2026-10-18: a tool
  whose name is not an identifier is reached by its stub name (`my-tool` as
  `tools.my_tool`, `from` as `tools.from_`), and its stub keeps every
  parameter name; `experiments/tool-exposure/RESULTS.md` §9. Built
  2026-10-19 (#55): a tool whose schema cannot be stubbed (a `$ref` that does
  not resolve, or is not local) is left out of `tools`, and every head shows
  "tool X is unavailable in code mode: <reason>"; the other tools are
  unaffected.
- `search_tools(query, limit=8, namespace=None)`, `describe_tool(name)`,
  `describe_namespace(name)` — discovery inside the runtime (level 4).
- `print` and `return` — the only output that reaches the model, with a cap and
  the full text written to a file when the cap cuts it.
- `store(key, value)` / `load(key)` — small JSON state across scripts, written as
  a custom entry only when the script succeeds, so each branch sees what was
  written on its own path. This is pi's design, and it is already the tree-as-truth
  shape.

### 6.4 Interpreter lifetime

**Default: one interpreter per script, state only through `store`.** This is the
only lifetime that satisfies tree-as-truth with no new mechanism, because state is
a fold over the custom entries on the path, and a branch sees only its own
writes. pi's version has two gaps to fix rather than copy: a script cannot list
the keys, and a failed script's writes are dropped without the result saying so.

A persistent interpreter is allowed only as a cache of something the tree can
rebuild: the replay of the path's scripts, with each nested tool call answered
from its recorded result rather than run again. Cloudflare's codemode runtime
replays this way and raises on a divergence in call order or arguments. A heap
that holds anything the tree cannot regenerate is a hidden channel. smolagents
1.26 shows the failure: `run(reset=True)` clears the transcript and keeps the
interpreter's variables.

The filesystem is a hidden channel under every lifetime. A script that writes a
file has created state outside the tree.

### 6.5 Calls still running when a script ends

pi cancels them. The alternative is to turn each into a handle in the harness,
delivered later the way `feat/background-tools` (unmerged, 2026-09-24) delivers a
background result: by steering into the running turn or starting one. That is the
choice between one-shot scripts and pipelined ones, and it needs model-facing
`await`, `poll` and `cancel`, which that branch does not have.

Neither choice needs new CPython. Plain asyncio and a subprocess cover every
design measured. Free-threading (3.14t) only helps CPU-bound pure Python inside a
script, and subinterpreters lose to a subprocess on isolation and latency (§6.2).

## 7. Discovery, JMFTS and MCP

**Capability sets.** A sandbox should hold a frozen set of tool handles, resolved
once from an expression over names, namespaces, tags and annotations. A child's
set is the parent's set narrowed, with caveats such as a call count or a path
root added; a request for anything outside the parent's set raises. The grant is
written to the tree on the child's cursor. Cloudflare, FastMCP and runline each
give an instance its own set; none found lets a script narrow a set and pass it to
a child, or records the grant. Annotations come from the tool's author, and MCP
says to treat them as untrusted unless the server is trusted, so a selector over
them should name who declared them.

**Ranking.** A `ToolRanker` protocol with in-process BM25 as the default. A JMFTS
ranker, chosen by config, indexes each tool's search text — name, description,
parameter names, namespace — under a tools root and queries it with
`method="hybrid"`. pi's ranker carries the same idea as a comment: "BM25 today; a
hybrid ranker with embeddings can replace it".

**Past scripts as skills.** A successful script stored as an entry and indexed
could be returned by `search_tools` alongside the tools: Anthropic's `./skills`,
with the tree as the store. Blocked today, because a `customEntry` projects to
`""` in `tau-jmfts`.

**MCP.** A client extension registers each server's tools as
`mcp__<server>__<tool>`, in that server's namespace, with exposure `codemode`.
Build it after §5.2: an MCP client with no exposure modes is level 0, which is
the problem every source describes.

## 8. What is absent from this record

- **The interpreter decision.** §6.2 prices both live candidates; nothing here
  picks one.
- **A measured comparison of script-writing against tool calling on τ's default
  local model.** Every quality claim in the sources is about frontier models.
  Whether a small local model writes reliable scripts is unmeasured, and that
  measurement would decide whether code mode is a default or an opt-in.
- **Behaviour on interruption.** Esc during a script, a sequential tool such as
  `bash` called from parallel script code, and a `request_user_action` raised
  from a nested call each need a defined outcome before anything is built.
- **Rendering.** The reading that turns nested calls into display data belongs in
  `tau_agent_core`, or only one head shows them.
