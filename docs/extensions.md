# Extension System Design

## Overview

τ's extension system is Python modules, loaded at runtime, that register
tools, intercept/veto/modify events, add commands and shortcuts, persist
state, and talk to the user. `importlib`, no compile step, no manifest
language — this is the one design goal from day one that held up
unchanged.

This is the canonical doc for the `ExtensionAPI` surface. Earlier drafts
described overlapping, slowly-diverging versions of this same API in three
different places (`ARCHITECTURE.md`, this file, `tau-api-reference.md`);
`tau-agent-core.md` and the README now just point here instead.

## Registration convention

```python
def register(api):
    api.register_tool(my_tool_def)
    api.on("tool_call", my_handler)
```

The parameter is named `api`, not `pi` — every real extension in
`examples/` and `tau_agent_core/extensions_builtin/` uses this convention.

## Discovery

Two sources, in this order, deduped by resolved path (first occurrence
wins): the global directory `~/.tau/extensions/` (bare `*.py` files or
`<dir>/__init__.py` packages, loaded alphabetically), then every explicit
`-e`/`--extension PATH` (repeatable; still loads under `--no-extensions`,
which suppresses discovery only). **There is no project-local
`<cwd>/.tau/extensions/` discovery** — deliberately deferred pending the
Tier-8 trust gate, not an oversight. There is also no dependency-manifest
convention (`requirements.txt`, `package.json`) inside an extension
directory — dependency management is the operator's own venv; a missing
import surfaces as a load error (raises for an explicit `-e` path,
collected into an errors list for a discovered one).

Collision handling differs by what's colliding, and is not "later wins"
uniformly:

- **Tools** — a duplicate name **raises** `ValueError` at load time
  (a deliberate divergence: silent first-wins was pi's answer, τ's is not).
- **Commands** — silent last-write-wins.
- **Shortcuts** — last-write-wins, with a logged warning.
- **Hook chains** — first `block: True` wins for `tool_call`; `before_agent_start`'s
  `system_prompt` contribution is last-wins.

## Hook and event vocabulary

**Mutating hooks** (subscribe via `api.on(name, handler)`; handler return
value is applied) — `tool_call` (veto/patch args before execution),
`tool_result` (modify content/details/terminate after execution),
`before_agent_start` (contribute to the system prompt), `input` (transform
the user prompt pre-node), `turn_end`, `user_turn_end`,
`session_before_switch` (veto a session new/fork/switch operation).

**Lifecycle hooks** — `session_start`, `session_shutdown` (each carries a
`reason`, e.g. `"reload"`).

**Notify-only events** — `agent_start`, `agent_end`, `turn_start`,
`message_start`/`message_update`/`message_end`,
`tool_execution_start`/`tool_execution_update`/`tool_execution_end`, plus
the wildcard `"all"`. These are observer-only, reached through the same
`api.on(...)` call.

**The two vocabularies take different handlers**, and `api.on` routes by
name, so the name you subscribe to decides which contract you owe:

| Kind | Handler | `event` is |
|---|---|---|
| mutating hook + lifecycle hook | `handler(event, ctx)` | a plain **dict** |
| notify-only event | `handler(event)` | an `AgentEvent` **object** |

A one-argument handler on a hook raises `TypeError` the first time that hook
fires, which for `session_shutdown` is at the very end of a session and for
`tool_call` is in the fail-closed path, where a raising handler blocks the
call. `tau-agent-core/tests/test_examples_contract.py` holds every shipped
example to this.

**Choose the turn-boundary hook by cadence.** `turn_end` fires once per
*agent-loop* turn, so one user request resolved in six tool round-trips fires
it six times; `user_turn_end` fires once per `prompt()`. Anything that should
happen once per thing-the-user-asked-for wants the latter.

**The one sharp edge:** `api.on("turn_end", ...)` always resolves to the
*mutating* hook, never the notify event of the same name — there is no way
to observe the plain notify-grade `turn_end` via `api.on`. Use `"all"` or
`AgentSession.subscribe()` for pure observation instead. `context` was a
hook in an earlier design and is now retired — calling `api.on("context",
...)` raises.

## The `ExtensionAPI` surface

Grouped by purpose, with real signatures from `extension_types.py`:

```python
# Events
api.on(event: str, handler: Callable) -> Callable[[], None]

# Tools
api.register_tool(definition: dict | ExtensionToolDefinition) -> None
api.get_all_tools() -> list[Any]
api.set_active_tools(names: list[str]) -> None

# Commands and shortcuts
api.register_command(name: str, command: dict) -> None
api.register_shortcut(...)                         # guarded ctrl+e namespace

# Session state
api.append_entry(custom_type: str, data: dict) -> None   # durable customEntry (not RAM-only)
api.set_session_name(name: str) -> None
api.get_session_name() -> str | None

# Messaging
api.send_user_message(content: str, deliver_as: str = "followUp") -> None
api.send_message(message: dict, options: dict | None = None) -> None

# Turn origination (attributed submissions — the "one door" for extensions)
await api.submit(...)
api.submit_threadsafe(...)

# Inter-extension pub/sub
await api.emit(topic: str, payload: Any) -> None    # ext:<name>:<topic> channels

# Per-extension config — replaces the deleted register_flag/get_flag
api.config -> dict[str, Any]     # from ~/.tau/config.json "extensions.<name>", or --ext-config

api.ui -> ExtensionUI
api.context -> ExtensionContext
```

`register_flag`/`get_flag` (an earlier CLI-flag registration idea) were
deleted, not deprecated — they never populated a value. `api.config` is
the real replacement: sourced from `~/.tau/config.json`'s
`extensions.<name>` block (keyed by file stem), overridable per-run with
`--ext-config NAME.KEY=VALUE` (CLI wins over config.json).
`send_user_message`'s default is `"followUp"`, not `"steer"` — the other
valid values are `"nextTurn"` and `"steer"`. `send_message` used to be
inert (called a method that didn't exist); it's wired now.
`register_tool` takes a **plain dict**, matching every real example below —
not a `ToolDefinition`. `tau_llm.define_tool()` exists and is validated, but
it builds the *other* tool shape: its `execute` takes the tool's own
parameters, while an extension tool's `execute` is
`execute(tool_call_id, params, signal, on_update, ctx)` and receives the
bound `ExtensionContext`. `register_tool` will not accept a `ToolDefinition`,
and that is deliberate; the two contracts are not interchangeable.

## `ExtensionContext`

```python
ctx.cwd -> str
ctx.signal -> AbortSignal | None
ctx.is_idle -> bool
ctx.abort() -> None
ctx.shutdown() -> None
ctx.shutdown_requested -> bool
ctx.get_context_usage() -> dict | None
ctx.get_model() -> dict
ctx.set_model(name: str) -> dict
ctx.get_usage() -> dict | None
await ctx.compact(custom_instructions: str | None = None, defer: bool = False)
ctx.entries() -> list[dict]
ctx.resolve_model(model: Any = None) -> Any
await ctx.complete(...)          # constrained-decoding completion
await ctx.spawn_branch(...)
await ctx.summarize_branch(...)
await ctx.navigate(...)
await ctx.fork(...)
```

## `ExtensionUI`

```python
await ctx.ui.confirm(title: str, message: str) -> bool
await ctx.ui.select(title: str, items: list[str]) -> str | None
await ctx.ui.input(title: str, default: str = "") -> str
await ctx.ui.form(spec: dict) -> dict | None
ctx.ui.notify(message: str, level: str = "info", *, source: str | None = None) -> None
ctx.ui.set_status(key: str, text: str | None, *, source: str | None = None) -> None
ctx.ui.panel(key: str, spec: dict | None, *, source: str | None = None) -> None
```

All four dialog methods are genuinely implemented in the TUI today (an
earlier version of the TUI delegates raised `NotImplementedError`). In
headless mode, a dialog call **raises `HeadlessDialogError`** unless a
policy is configured (`--ui-defaults METHOD=ANSWER,...` or `config.json`'s
`ui_defaults`) — this replaced an earlier silent auto-resolve (confirm→
True, select→first) with a Fail-Early default, deliberately the stricter
direction, not the convenient one.

## Hot reload

`/extensions reload <target>` (one named extension, not a bulk
"reload all") fires `session_shutdown(reason="reload")` for that
extension's bucket, unregisters its tools/commands/shortcuts, does a full
**from-scratch re-import** (not stdlib `importlib.reload()` — a fresh
module object via `importlib.util.spec_from_file_location`), then fires
`session_start(reason="reload")`. `enable`/`disable` (also per-target) are
siblings of the same command.

## Real example extensions

Prefer reading these over hand-sketched pseudocode — they're real, and every
one of them is loaded, registered and signature-checked by
`tau-agent-core/tests/test_examples_contract.py`, so they cannot drift the way
embedded examples in a design doc do. (That guard is new. Before it existed,
examples 02–05 had rotted: two registered a tool dict with no `execute`, one
used a one-argument hook handler, and four had no module-level `register`, so
`tau -e` could not load them at all.)

- `examples/01_permission_gate.py` — blocks dangerous bash patterns via a
  genuine `tool_call` veto. Its own docstring documents a real bug it once
  had (subscribing to a notify-only event that can't block anything) and
  how it was fixed — worth reading as a cautionary tale about the
  mutating-vs-notify distinction above.
- `examples/30_permission_gate.py` — the human-in-the-loop variant:
  `ctx.ui.confirm`-gated instead of a hard block.
- `examples/31_protected_paths.py` — vetoes writes/edits to paths sourced
  from `api.config`, not a hardcoded list.
- `examples/05_custom_tool.py` — the smallest complete `register_tool`: one
  schema, one five-argument `execute`, one result dict. Start here before any
  of the tool-registering demos below.
- `examples/02_git_checkpoint.py` — commits the working tree on
  `user_turn_end`, and its docstring explains why not `turn_end` (six
  round-trips would mean six commits for one request).
- `examples/03_dynamic_env_tool.py` — registers a tool (`env_vars`) that
  reads `os.environ`, masking values whose name looks like a credential and
  bounding its own output so a large environment cannot flood the context.
- `examples/04_session_logger.py` — the notify side of the same coin:
  `api.on("all", ...)` with a one-argument handler, writing JSONL.
- `tau_agent_core/extensions_builtin/nats_bus.py` — the largest real
  extension (843 lines): a NATS bus bridge, `TOUCHES_BUS`-gated, iterated
  against a live sibling project. See the README's "Where τ diverges from
  pi" and `docs/REMOTE-CONTROL.md`.
