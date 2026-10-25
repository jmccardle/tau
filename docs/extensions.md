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

Finding is not loading (docs/EXTENSION-PACKAGES.md). τ discovers two kinds of
source, and loads one only once the user has authorized it:

- **drop-ins** in `~/.tau/extensions/`: bare `*.py` files or `<dir>/__init__.py`
  packages, alphabetically. The identifier is the file's stem, verbatim.
- **installed packages** that declare an entry point in the `tau.extensions`
  group, naming a module that exposes `register(api)`:

  ```toml
  [project.entry-points."tau.extensions"]
  planner = "tau_router.planner"
  ```

  The identifier is the module name (`tau_router.planner`), and the module is
  imported under that real name.

An unauthorized source is offered in the TUI and named on stderr by the
headless modes, which never prompt: `tau ext authorize NAME` authorizes it, and
`tau ext list` shows every source and its state. A discovered extension that
fails to load gets one notice and the run goes on; `tau ext disable NAME` stops
loading and announcing it, and `tau ext enable NAME` undoes that. Drop-ins
present at the first 0.14.0 start are authorized once, automatically. Every explicit
`-e`/`--extension PATH|MODULE` (repeatable) is authorized by being named and
still loads under `--no-extensions`, which suppresses discovery only. A value
holding a `/` or ending in `.py` is a path; anything else names an installed
module. **There is no project-local
`<cwd>/.tau/extensions/` discovery** — deliberately deferred pending the
Tier-8 trust gate, not an oversight. There is also no dependency-manifest
convention (`requirements.txt`, `package.json`) inside an extension
directory — dependency management is the operator's own venv; a missing
import surfaces as a load error (stops startup for an explicit `-e` in `-p`
and `--mode rpc`, collected into an errors list for a discovered one).

An extension can import the modules sitting **beside** it: the loader puts
the extension's own directory on `sys.path` for the length of the import
and pops it afterwards, so `helpers.py` next to `myext.py` is reachable as
`import helpers`, and a package's `inner.py` as either `import inner` or
`from .inner import x`. Two limits follow from where that window opens and
closes. The window covers the module body only, so an import deferred into
`register(api)` or into a hook still raises `ModuleNotFoundError` — put
sibling imports at the top of the file. And the directory is *prepended*,
so a module beside the extension beats an installed distribution of the
same name; avoid naming a helper after something on PyPI.

Collision handling differs by what's colliding, and is not "later wins"
uniformly:

- **Tools** — a duplicate name **raises** `ValueError` at load time
  (a deliberate divergence: silent first-wins was pi's answer, τ's is not).
- **Commands** — first-wins on the typed name, and the loser keeps
  `ext:<extension>:<command>`; see `docs/EXTENSION-NAMESPACE.md`.
- **Shortcuts** — last-write-wins, with a logged warning.
- **Hook chains** — first `block: True` wins for `tool_call`; `before_agent_start`'s
  `system_prompt` contribution is last-wins.

## Hook and event vocabulary

**Mutating hooks** (subscribe via `api.on(name, handler)`; handler return
value is applied) — `tool_call` (veto/patch args before execution),
`tool_result` (modify content/details/terminate after execution),
`before_agent_start` (contribute to the system prompt), `input` (transform
the user prompt pre-node; return `{"handled": True}` to consume it without a
turn, or `{"refuse": "<reason>"}` to refuse the submission, which `submit`
reports as `accepted=False` with that reason and writes nothing), `turn_end`,
`user_turn_end`, `session_before_switch` (veto a session new/fork/switch
operation), `annotate` (return JSON stored on the entry about to be written as
`ext[<extension name>]`, never sent to the model; at most one handler per
extension; see `docs/ENTRY-ANNOTATIONS.md`), `entry_written` (observe
`{kind, entry_id, message, blocks}` for each message entry after it is in the
store; a deep copy, return ignored; dispatched per write group, so a parallel
tool batch's results fire only once every result is written; not dispatched
when `store_history` is off; see `docs/STEP-DEBUGGER.md` §3.2),
`turn_setting_changed` (observe `{cursor_id, setting, old, new, source}` when a
model or thinking change is recorded; return ignored; see
`docs/TURN-SETTINGS.md` §8).

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
`tool_call` ends the turn. `tau-agent-core/tests/test_examples_contract.py`
holds every shipped example to this.

**A raising handler ends the turn at a hook that can change it**: `input`,
`before_agent_start`, `tool_call`, `tool_result`, `turn_end`, `user_turn_end`,
`session_before_switch` and `annotate`. The error goes to the error listeners
first, and `agent_end` then carries `end_reason: "error"` naming your extension
and the hook. A raise at `entry_written`, `turn_setting_changed` or a lifecycle
hook is reported and the turn goes on. Catch what your handler can recover from;
anything else stops the turn rather than letting it run without your check
(docs/HOOK-ERRORS.md).

**Choose the turn-boundary hook by cadence.** `turn_end` fires once per
*agent-loop* turn, so one user request resolved in six tool round-trips fires
it six times; `user_turn_end` fires once per `prompt()`. Anything that should
happen once per thing-the-user-asked-for wants the latter.

**Handlers must not block.** A `sleep()` or a slow call holds the agent up from
getting back to work. With no timeout (no `policy=`, `"human"`, or `Unbounded`)
a handler runs inline on the event loop, sync or async, and an `async def`
that blocks inside its body stalls the loop, which is
[asyncio's own rule](https://docs.python.org/3/library/asyncio-dev.html#running-blocking-code),
not a τ limit. Give any reaction that could make the UI unresponsive, or that
waits on a remote service, a timeout policy: `policy="gating"`, `"advisory"`, or
your own `Bounded`. Under one, a plain `def` handler runs on a worker thread and
the hook returns at the deadline; ctx and api calls it makes are marshalled onto
the loop or refused (docs/STEP-DEBUGGER.md §4, "Built (#97)").

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
api.on(event: str, handler: Callable, *, policy: str | None = None) -> Callable[[], None]

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
await api.request(topic, payload, *, timeout_s) -> list[RequestReply]  # one reply per subscriber, docs/EXT-REQUEST.md
api.subscriber_count(topic: str) -> int

# Per-extension config — replaces the deleted register_flag/get_flag
api.config -> dict[str, Any]     # from ~/.tau/config.json "extensions.<name>", or --ext-config

api.ui -> ExtensionUI
api.context -> ExtensionContext
```

`register_flag`/`get_flag` (an earlier CLI-flag registration idea) were
deleted, not deprecated — they never populated a value. `api.config` is
the real replacement: sourced from `~/.tau/config.json`'s
`extensions.<name>` block (keyed by file stem), overridable per-run with
`--ext-config NAME:KEY=VALUE` (CLI wins over config.json).
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

### `CONFIG_SCHEMA` — saying what those keys are

`api.config` is an untyped dict, so on its own it tells a head nothing about
what may be in it. A module-level `CONFIG_SCHEMA` is the declaration:

```python
CONFIG_SCHEMA = {
    "title": "Session logger",
    "fields": [
        {"name": "log_file", "kind": "text", "label": "Log file"},
    ],
}
```

It is a `ui.form` spec — the same shape `validate_form_spec` accepts, so the
TUI's form renderer and the headless `form=defaults` answerer both already
understand it. It is read at import time and validated at load, beside
`TOUCHES_BUS`/`SUBJECTS` and for the same reason: an invalid one raises
`ExtensionCapabilityError` and `register` never runs. Declaring none is fine
and means "offer no settings screen", which is different from an empty one.

Two capabilities read and write it — `get_extension_config(path)` returns
`{path, schema, values}`, and `set_extension_config(path, values)` checks every
value against the schema, replaces the slice wholesale, and reloads the
extension so `api.config` is rebound. Both are on the RPC wire. An undeclared
key, a missing declared field, or a wrong type raises rather than being
dropped.

Two limits, both deliberate. The values are **not persisted**: the core does not
own `~/.tau/config.json`, so they last for the session and a head that wants
them durable writes the file. And there is **no slash command or palette entry**
for it, because a `Flow` declares a fixed tuple of `Argument`s and this
mutation's second one is a form whose fields are whatever the chosen extension
declared. Offering it needs a registration path into the capability registry,
not head code.

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
ctx.leaf_id() -> str | None      # the acting cursor's leaf; never infer it from entries()
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

## Warnings

Added 2026-10-19 (#60). A notification is gone once shown. A warning stays
until it is cleared, so a head opened later, or a serve client that attaches
after the first turn, still sees it.

```python
api.set_warning(key: str, message: str, severity: str = "warning") -> None
api.clear_warning(key: str) -> None
```

- A warning is keyed by `(extension identifier, key)`. Setting the same key
  replaces the old one, and clearing an absent key does nothing.
- `key` and `message` are non-blank strings, and `message` is one line.
  `severity` is `"warning"` or `"error"`. Anything else raises `ValueError`,
  naming the field.
- A new or changed warning is also shown once through `ui.notify`, as
  `"<extension>: <message>"`. Headless, that is one stderr line,
  `[τ] <severity>: <extension>: <message>`. Under `--mode json` it is one
  record instead,
  `{"type": "extension", "kind": "notify", "extension": "<extension>", "level": "<severity>", "message": "<extension>: <message>"}`.
  Setting an identical warning again shows nothing.
- **Lifecycle: warnings are extension state, not session state.** They
  persist across `new`, `fork` and `switch`/resume until the extension clears
  or replaces its own key. After each swap, every current warning is shown
  once more, so the new session's user sees it; the set itself does not
  change, so no listener or `ui` event fires. Disabling or reloading an
  extension clears that extension's warnings. Set a warning wherever its
  condition is found: `register()`, `session_start` or a tool. A later
  post-swap `session_start` (#60 decision B, open) may revisit this for
  warnings that belong to one session.
- The identifier `tau` is reserved for τ's own warnings, so an extension
  named `tau` that calls `set_warning` or `clear_warning` gets a
  `ValueError`.
- **Threads.** Both calls are safe from any thread, such as a tool's
  `asyncio.to_thread` or an extension's own loop thread. The warning is checked
  on the calling thread, so a bad one raises there. While the session's loop
  runs, the change is made on that loop, where every listener runs. From
  another thread the call returns before the warning lands, and a listener's
  exception goes to that loop's exception handler, not to the caller. Between
  turns of an SDK host that runs one `asyncio.run` per turn, the change is
  made at once, under the store's lock. A warning write never moves the
  session onto the caller's loop; only a turn and `session_start` bind one.
- τ writes its own warnings with source `"tau"` through
  `AgentSession.set_extension_warning`. `AgentSession.extension_warnings()` reads
  the current set.
- Each extension that failed to load is mirrored as a `"tau"` error keyed
  `load:<label>`, without a second notice, because every head already
  announced the failure.

```python
def register(api):
    async def search(tool_call_id, params, signal, on_update, ctx):
        try:
            hits = await asyncio.to_thread(backend.query, params["q"])
        except BackendDown as exc:
            api.set_warning("backend", f"search backend is down: {exc}", "error")
            raise
        api.clear_warning("backend")
        return {"content": [{"type": "text", "text": hits}]}
```

The TUI keeps a one-line marker above the status strip while any warning is
current, such as `1 extension error — /extensions`. The text comes from
`warnings_marker()`, so every head words it the same way. A toast fades after
about five seconds and the marker does not. The TUI and the REPL list each
warning under `/extensions`. Serve and RPC carry them as described in
docs/TAU-SERVE.md §7.5.

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
