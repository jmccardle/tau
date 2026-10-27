# D8: No compatibility with 0.15 extensions

Status: accepted (Phase 1)

## Context

0.15 extensions registered handlers through a 42-method API object, ran in a
runner with its own state, and some paused a turn to wait for a person. The
hook set here differs in meaning: a hook sits at a fixed point of the phase
loop, changes state through its handle, and returns `None` or a string.

## Decision

- **No compatibility layer.** An extension is a Python module that registers
  hooks and tools through the public API.
- **A documented mapping** replaces a shim (glossary, Retired terms):
  `input` → `pre_input`; `before_agent_start` → `pre_sample`, moving onto
  what it adds; `tool_call` → `pre_tool`; `tool_result` → `pre_result` per
  call, `post_result` per batch; `turn_end`, `user_turn_end` → `post_reply`
  or `post_step`; node-written and annotate hooks → event observers.
- No lifecycle hooks until a Phase 5 extension needs one (Q23). No hook
  deadlines in core (Q9). The handle surface is Phase 4's.

## Alternatives

| option | why not |
|---|---|
| A shim that runs 0.15 extensions | It would fake the turns, per-turn state, and pausing broker this design removed. |
| Port the 0.15 extensions | Separate work per extension; rule 1 forbids copying. |

## Consequences

- The hook model stays small, and I1 and I2 hold for every extension.
- Cost: no 0.15 extension runs unchanged.
- Enforced by the Phase 5 exit criterion and rule 3's lint.
