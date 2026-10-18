# A sub-agent continues an agent turn instead of answering a prompt

Built 2026-10-15. `ctx.spawn_branch` and `AgentSession.spawn` take a `seed`: messages
written on the branch before it runs. With no prompt, the sub-agent answers the seed
directly, with no user turn added. A planner's tool can hang a sub-agent beside its
own pending call and hand it a note plus an injected call and result. Before
anything is written, the seed and the context it extends are checked for a tool
call with no result, and the check names the call. The same change makes a
branch's model run with its own API key.

## 1. What was wrong

The tau-router probe (`experiments/ext/branch_probe.py` in that repo) found two
defects. Each one fails the first request.

- **A sub-agent spawned from inside a tool got a context with an unanswered call.**
  The planner model calls `delegate`, and the tool's execute calls
  `ctx.spawn_branch(ctx.cursor.leaf, prompt, …)`. That leaf is the planner's
  assistant message, and its call has no result yet. So the child's context was
  system → user → *call (no result)* → user prompt, and Mistral returned HTTP
  400 "Not the same number of function calls and responses". The probe got
  around it by anchoring at the leaf's parent. Even then, the child could only
  start from a new *user* message. There was no way to give it the planner's
  instruction as the answer to a call, which is how the router's design frames a
  sub-agent: it continues the agent turn.
- **A branch on another provider ran with its spawner's key.** An Anthropic parent
  spawning `model="mistral-small"` got HTTP 401 "Invalid API Key" from Mistral.
  `spawn` built `TurnFrame(model=model, api_key=self._api_key)`, and
  `ctx.resolve_model` dropped the key that the config held for the name (§7).

The admission check had the first defect's blind spot too. `admission_reason`
(tree surgery) reset its pending set at every assistant message, so the sequence
call → note → call → result passed: the first call was silently forgotten. That
is exactly the shape of a seeded branch anchored at the pending call (§6).

## 2. Where the branch hangs

John's model: a conversation is a tree, and a context is a walk of it. Given
0 system → 1 user → 2 planner call → (3 result, pending), the sub-agent's branch
is a second child of 1: 4 note → 5 injected call → 6 injected result. The child
sees 0 → 1 → 4 → 5 → 6 and continues from there. The planner's walk 0 → 1 → 2 → 3
never contains the branch.

From inside a tool the anchor is the **parent** of the calling assistant message,
`ctx.cursor.leaf`. That message can be preceded by a config entry recorded mid-loop
(docs/TURN-SETTINGS.md §3). The context fold skips config entries, so the parent
works as an anchor either way. Two alternatives lose:

- *A child of 2.* Its context ends on a call with no result, which is §1's
  defect. It is now refused (§6).
- *A child of 3, after the planner's result.* Result 3 does not exist until the
  child finishes, and writing a provisional one would change the planner's own
  walk.

No helper computes the anchor. One line does it
(`next(e["parentId"] for e in ctx.entries() if e["id"] == ctx.cursor.leaf)`), and
a helper would have to guess whether its caller is inside a tool.

## 3. The seed's shape

A seed is a list of message dicts **in the shapes the tree already stores**, so
nothing reads a second format. `tau_agent_core.seed.normalise_seed` accepts:

- `user`, `assistant` and `toolResult` messages, each validated by its `tau_llm`
  model (`UserMessage`, `AssistantMessage`, `ToolResultMessage`) and stored as
  that model's dump;
- a note `{customType, content, display?, details?}`, with or without
  `role: "custom"`, built by `create_custom_message`, the same builder that
  `before_agent_start` and `user_turn_end` returns go through. On the wire it is
  a user message (`convert_to_llm`).

A missing `timestamp` is filled with the time of the call. Anything else is a
`ValueError` that names the element's index.

`seed_tool_call(name, arguments, result, *, is_error=False)` returns the injected
`[assistant, toolResult]` pair with a fresh id (§5), so no caller builds ids or
the `AssistantMessage` boilerplate by hand. The assistant message's `api`,
`provider` and `model` are all `"tau-seed"` (`SEED_ORIGIN`), so no reader
mistakes it for a completion some model produced. It has no usage, which is true:
nothing was billed. The tool named need not be one the branch may call. The
point is the shape of the context, not a callable tool.

## 4. No user turn

With `seed` and no `prompt`, the branch's turn adds no user message. The loop runs
with the seed as its prompts (`AgentLoop.run(prompts=seed, context=…)`), and the
model's first request is the seed's answer. `run` rather than `run_continue`
because both behave the same once the seed is in the message list, and `run` is
the path `_run_one_turn` already takes. One write path then covers the seeded
and unseeded cases.

The turn is otherwise the one `submit` runs. Its tail was split out as
`_drive_turn`, and the seeded path `_submit_seeded` enters it holding the fresh
cursor's lock after the depth cap. So the frame's config entry is written first,
then the seed, then the loop's messages. The events carry the branch's
`cursor_id` and `source="agent"`, and `submission_start`/`submission_end` bracket
the turn. The end-of-prompt drain and `user_turn_end` run as for any turn, and
`BranchResult.messages` begins with the seed, as a prompted branch's begins with
its user message.

Seed-only skips two things a prompt gets, both because they are about prompt
text: the `input` hook chain and command expansion, and `before_agent_start`,
whose `prompt` argument would be empty. `submission_start` carries `text=""`.

**Prompt and seed together are allowed.** The seed is written first, then the
prompt as a user message, which then runs the ordinary input pipeline. Refusing
the combination would have cost a caller who wants "here is what was done; now
summarise it" a second spawn. The check (§6) covers the seed followed by a user
message, so a seed ending on an unanswered call is refused there too.

## 5. Injected call ids

`new_tool_call_id()` returns nine characters drawn from `[a-zA-Z0-9]` by
`secrets`. Nine alphanumerics is what Mistral accepts (exactly that), and
Anthropic and OpenAI accept it too. A recognisable prefix such as `seed_…` would
be rejected by Mistral, the provider the router runs on.

An id only has to be unique **within one context**, because a provider sees one
walk at a time. That is checked, not assumed. `check_seed` refuses a seed id that
repeats within the seed or is already declared on the path at `at`, so an
injected id cannot collide with a provider-issued one in any context it reaches.
Across concurrent branches and the rest of the tree, uniqueness is probabilistic:
62⁹ ≈ 1.35·10¹⁶ ids, so a collision between two of 10⁶ ids has probability under
10⁻⁴. Even then, two calls with one id only matter when they share a path, and
that is the case the check refuses.

## 6. Validation, before anything is written

`spawn` resolves the model, checks the tool allowlist, and then calls
`_checked_seed`. That normalises the seed and runs `check_seed` against
`ConversationTree(entries, at).context_for()`. All of this happens before
`open_cursor`, any write, or any provider call. A refusal is a `ValueError`:

| Case | Message contains |
|---|---|
| neither `prompt` nor `seed` | `give a prompt, a seed, or both` |
| `seed=[]` | `seed is empty` |
| an element that is not a dict / has another role / fails its model | `seed[i] …` |
| `at` names no entry | `branch point 'x' not found` |
| an id repeats, or is already on the path | `tool call id 'x' in the seed is already used on this path` |
| `at` is the pending call message, or a seed call is followed by a note or user message before its result | `tool call(s) name(id) are interrupted by a 'user' message before their results` |
| a seed result with no call | `a tool result for call id has no tool call before it` |
| the seed ends on an unanswered call | `unanswered tool call(s) name(id)` |
| no prompt and the seed ends on an assistant message | `the seed ends on an assistant message` |

The pairing check is **reused, not written again**: `tree_surgery.admission_reason`,
the plan-level check the tree browser's branch composer runs. It gained a third
fault, the *interrupted call*: a message the model sees, other than a
`toolResult`, follows an assistant message whose calls are not all answered. It
reads the sequence after `convert_to_llm`, which is what the provider receives.
So a display-only note (`visibleToModel: False`) between a call and its result is
not a fault, and a model-visible one is. The browser's composer gets the stricter
check too, which is right: a composed branch with an interrupted call is the same
400.

## 7. The key goes with the model

`spawn` takes `model` as a config name or a `Model`, and resolves both with
`AgentSession.model_and_key`:

- a **name** goes through `resolve_model`, the one way a credential enters a
  session. That gives `ConfigModelResolver.api_key(name)`: the config entry's
  key, or `None` for the provider's environment variable. The frame records
  the model, that key and `model_name=name`, as `set_model` does, so the
  branch's config entry names the model by the name `--continue` resumes on;
- a **`Model` object** keeps the session's key only when it *equals* the
  session's model, and otherwise gets `None`. Equality rather than identity: an
  equal model has the same provider and base URL, so the key is right for it, and
  a resolver that builds a fresh object for the same name still matches;
- **`None`** leaves the frame's model unset, so the branch runs on the session's
  model and key.

`ctx.resolve_model` and `ctx.complete` take the same path. `ctx.complete` had
the same leak: an explicit `api_key` aside, it sent the session's key to any
model it was handed. `compare` already resolved names through `resolve_model`;
it now also records `model_name`. A plain `name -> Model` resolver, one with no
`api_key(name)`, used to give every model the session's key. It now gives `None`
for any model not equal to the session's. All three heads bind
`ConfigModelResolver`, so this changes only SDK callers that bind a plain
resolver.

Audited, and left alone because each one sends a model with its own key:
`AgentSession.summarizer` and `summarize_branch`; the headless navigate path
(`backends.py`, both through `summarizer()`); `set_model`; `_spawn_fork`, whose
frame has no model; and `ctx.summarize_branch`, which sends the session's own
model with its own key. That last one also ignores a configured summarizer,
unlike `AgentSession.summarize_branch`. That is a separate inconsistency, noted
in §9.

## 8. `tau -p` and a command's branch

The probe also found that after `tau -p "/probe"` the session file held only its
header, the config entry and the system message. Cause: the `tau` the probe ran
imports the main checkout, which was on a branch without 3a74ed7 ("tau -p binds
its cursor", on master since 2026-10-06). Without that fix, headless builds its
`AgentSession` on an `InMemorySessionLog`, and copies only the head turn's
messages to the file, after a prompt. A command returns before the copy, so
every entry a command writes, a spawned branch included, stays in memory. On
master, `run_print` binds the file-backed cursor (`headless.py`, `bind_cursor`),
`spawn` opens its cursor on that log, and every entry is written as it is made.
`tau-coding-agent/tests/test_headless_binds_cursor.py` pins it for a command that
spawns a branch.

## 9. Absent

- **`fork_admission_reason` is still the lax check.** It resets its pending set
  at each assistant message, as `admission_reason` used to. A tree path with an
  interrupted call is already broken, so the case is rare, but the two checks
  now disagree on it.
- **No anchor helper** (§2).
- **No `before_agent_start` or `input` hook on a seed-only branch** (§4). An
  extension that wants to inject into a sub-agent can put the message in the
  seed.
- **`ctx.summarize_branch` ignores a configured summarizer** (§7). It sends the
  session's model rather than `AgentSession.summarizer()`'s, and is not a key
  leak.
- **No wire verb.** `spawn` is reached through `ctx.spawn_branch` and the `fork`
  strategy only, as before (`test_rpc_capability_audit.py`).
