# τ-agent-core Design

## Scope

τ-agent-core is the agent runtime: the loop that drives conversations,
executes tools, manages sessions, and exposes the extension system. It is
**TUI-agnostic** — no Textual, no stdout/stdin assumptions. It runs
headlessly (SDK), as a subprocess driven over stdio (RPC), or under
`tau-coding-agent`'s TUI. `tau_agent_core` never imports `tau_coding_agent`.

## Package layout

```
src/tau_agent_core/
├── agent_loop.py, agent_loop_types.py    # The turn loop + its config type
├── agent_session.py, agent_session_runtime.py   # submit()/prompt(), the one door
├── session.py, session_log.py, session_catalog.py, conversation_tree.py
│                                          # the live session/tree architecture
├── session_manager.py                    # retired — see "Sessions" below
├── compaction.py, compaction_policy.py, compaction_utils.py
├── extension_types.py, extensions/, extensions_builtin/   # extension system
├── rpc/                                  # RPC server package (8 files)
├── sdk.py                                # create_agent_session() — SDK entry point
├── tools/                                # built-in tools: read/write/edit/bash/grep/find/ls
├── submission.py, commands.py, settings.py, usage.py, events.py, export.py
└── testing/                              # SessionCatalog/SessionLog contract-test helpers
```

## The agent loop

`AgentLoop.__init__(self, config: AgentLoopConfig, emit=None, tools=None,
model=None, abort_signal=None, hook_dispatcher: ExtensionRunner | None =
None, steer_queue: list[Any] | None = None)` — `config`, `tools`, `model`,
`abort_signal` are constructor arguments, not fields on one shared object.
`AgentLoopConfig` (`agent_loop_types.py`) is intentionally small: `model`,
`system_prompt`, `tool_execution_mode`, `max_retries`, `max_turns`,
`temperature`, `api_key`, `reasoning`. Everything an earlier design put on
the config as callbacks — `before_tool_call`/`after_tool_call`,
`get_steering_messages`, `get_follow_up_messages`, `transform_context` —
is now injected instead: extension hook dispatch goes through
`hook_dispatcher` (a real `ExtensionRunner`, gated on
`has_hook_handlers()` so the zero-extension path pays nothing), and
mid-turn steering goes through the shared `steer_queue` list.

`AgentLoop.run(prompts, context=None)` emits, per `tau_agent_core.events`:
`agent_start`, `agent_end`, `turn_start`, `turn_end`, `message_start`,
`message_update`, `message_end`, `tool_execution_start`,
`tool_execution_update`, `tool_execution_end` — plus, beyond the base set,
provenance (`submission_id`/`source`/`submitter`/`correlation`, stamped by
`submit()`), `blocked`/`blocked_by` on `tool_execution_end` for
extension-veto presentation, and `error` on `agent_end` when the loop
raised. For how a tool call's `arguments` gets transformed on its way from
the provider to a rendered widget, see CLAUDE.md's "the streaming event
pipeline" — that four-hop trace still holds.

## Submission lifecycle — the one door

`AgentSession.submit()` is the real entry point every input source funnels
through (TUI, headless, SDK, extensions, RPC); `AgentSession.prompt()` is
now a thin compatibility wrapper that builds a `Submission` and calls
`submit()`. Concurrency has a real policy — `multitask_strategy`:
`reject`/`enqueue`/`steer`/`rollback`/`fork` — rather than a different
answer improvised per caller. Full design: `docs/SUBMISSION-LIFECYCLE.md`.

## Sessions — a tree, not a chat log

`session_manager.py`'s `SessionManager` still exists as a file (kept for a
legacy `summarize_branch` re-export) but has zero real instantiations on
any live path — checked directly, not inferred. The live architecture is:

- `session_log.py`'s `SessionLog` Protocol — the minimal contract
  (`append_message`, `append_custom_message`, `append_custom_entry`,
  `append_compaction`, `append_elide`, `append_navigate`,
  `append_branch_summary`, `append_at`, `entries`, `cursor`) that both the
  SDK's `InMemorySessionLog` and `tau-coding-agent`'s file-backed `Session`
  satisfy, plus `BranchView`/`open_branch()` for a lightweight branch view
  that doesn't disturb the parent log's cursor (the closest thing to
  "clone" — there is no method literally named `clone` or `navigate`;
  the latter is `append_navigate`, which appends a marker entry rather than
  mutating a cursor in place).
- `conversation_tree.py`'s `ConversationTree` — walks `parent_id` chains to
  build model input; this is what makes fork/branch/rollback/a second agent
  at an earlier point in the conversation safe. Full invariants:
  `docs/NODE-ADDRESSABLE-AGENTS.md`.
- `session_catalog.py` — the catalog abstraction TUI/headless/RPC use to
  list and resolve sessions (file store or JMFTS-backed, via
  `tau-jmfts`, selected by `--store`/config).

## SDK entry point (`sdk.py`)

```python
def create_agent_session(
    model: str | Model = "gpt-4o", provider: str = "openai",
    base_url: str | None = None, api_key: str | None = None,
    tools: list[str] | None = None, session_log: SessionLog | None = None,
    extensions: list[Callable] | None = None, system_prompt: str | None = None,
    no_context_files: bool = False,
    thinking_level: str = "off", cwd: str | None = None,
    tool_execution_mode: Literal["sequential", "parallel"] = "parallel",
    compaction_policy: CompactionPolicy | None = None,
    bus_available: bool = False,
    no_tools: Literal["all", "builtin"] | None = None,
) -> AgentSession
```

This is the genuine SDK entry point — used by 20+ scripts under
`examples/`, `scripts/tectum_responder.py`, and real unit tests. There is
deliberately **no** `settings=` parameter: an earlier version had one that
was silently ignored, so it was removed rather than kept as a no-op —
passing `settings=` today raises `TypeError` (Fail-Early). `tools` takes
built-in name strings only (`read`, `write`, `edit`, `bash`, `grep`,
`find`, `ls`); an `AgentTool` instance needs the `AgentSession` constructor
directly.

`no_tools` is the SDK half of the CLI's two flags: `"all"` offers the model
nothing, `"builtin"` drops the built-in set and keeps extension-registered
tools. Passing `tools=` and `no_tools=` together **raises** — they ask for
opposite things and neither outranks the other at a call site. `tools=None`
and `tools=[]` stay legal alongside `"all"`.

**`create_agent_session` is still not on the live TUI/headless path.**
`tau-coding-agent/src/tau_coding_agent/backends.py` constructs `AgentSession`
directly. What used to follow from that no longer does: the backend now calls
`_build_system_prompt` itself (`backends.py:1133`), so τ's base prompt and its
project context files reach the model on every path. Before `db98524` neither
had ever reached a model on the TUI or headless path at all — the backend
passed `config.get("system_prompt", "")` straight through, and after the
config key was removed that was the empty string.

What remains is that the factory's OTHER defaults are exercised only by SDK
callers. Worth deciding deliberately (wire it in, or accept the split) rather
than leaving unstated.

## Compaction (`compaction.py`)

`should_compact(context_tokens: int, context_window: int, settings:
CompactionSettings) -> bool`; `prepare_compaction(path_entries: list[dict],
settings: CompactionSettings) -> CompactionPreparation | None`; `async def
compact(preparation, model, api_key, *, custom_instructions=None,
thinking_level=None) -> CompactionResult`. LLM-backed, no
fabricated-summary fallback on failure — a compaction error raises rather
than silently truncating.

## Extension system

See `docs/extensions.md` — the canonical doc for the `ExtensionAPI`
surface, hook vocabulary, discovery, and collision semantics. This package
hosts it (`extension_types.py` at the package root, `extensions/registry.py`
+ `extensions/runner.py`, `extensions_builtin/` for the built-in NATS bus
extension) but the API itself is documented once, there.

## RPC

`rpc/` is a full package (commands, handler, transport, dialect,
capabilities, wire_events), not the single `rpc.py` file an earlier plan
sketched. Design and scope: `docs/REMOTE-CONTROL.md`. Generated wire
reference, test-locked against the real command table:
`docs/RPC-PROTOCOL.md`.
