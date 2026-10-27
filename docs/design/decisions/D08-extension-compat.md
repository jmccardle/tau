# D8: No compatibility with 0.15 extensions

Status: accepted (Phase 1, 2026-10-26)

## Context

0.15 extensions registered handlers through a large API object, ran in a
runner with its own state, and relied on hooks such as `input`,
`before_agent_start`, `tool_call`, and `tool_result`. Some paused a turn and
waited for a person, which left state that a reload could not rebuild
(glossary, "Retired 0.15 terms").

The hook set here differs in meaning, not only in name. A hook now sits around
a write (model.md §7), steers by returning a value, and receives `api` and
`cursor` arguments.

## Decision

- **No compatibility layer.** An extension is a Python module that registers
  hooks and tools through the public API (glossary, extension). Extensions
  have no special standing in core.
- **A documented mapping** replaces a shim. The glossary's retirement table
  is that mapping:

  | 0.15 hook | here |
  |---|---|
  | `input` | `pre_input` |
  | `before_agent_start` | `pre_sample`, which appends what it adds (I1) |
  | `tool_call` | `pre_tool` |
  | `tool_result` | `pre_result` |
  | `turn_end`, `user_turn_end` | `post_reply` (Q20) |
  | the node-written and annotate hooks | event observers (`node_appended`) |

- Lifecycle hooks (`session_start` and the rest) do not exist until a
  Phase 5 extension needs one (Q23). Hook deadlines are not core's; a hook
  that needs one uses `asyncio.timeout` (Q9).
- The exact `api` surface is a Phase 4 decision.

## Alternatives

| option | why not |
|---|---|
| A shim that runs 0.15 extensions | The shim would have to fake 0.15's state: turns, per-turn state, a pausing broker. Each is something this design removed on purpose. It would also carry the 42-method API surface. |
| Port the 0.15 extensions in the repo | Not a compatibility choice; each port is its own work, and rule 1 forbids copying code from 0.15. |

## Consequences

- The hook model stays small and its guarantees (I1, I2) stay true for every
  extension.
- Extension authors rewrite once, with a table to follow.
- Cost: no 0.15 extension runs unchanged. τ 0.16 is incompatible on purpose
  (plan amendments).
- Cost: the table is only as good as its rows. Some 0.15 behaviors map to
  nothing (handler policies), and the docs say so.
- Enforced by: the Phase 5 exit criterion (each extension has behavior tests
  and needs no core change) and rule 3's lint. No invariant tests
  non-compatibility itself.
