# Backgrounding is a property conferred on a tool

Status: **built 2026-09-25, revised 2026-09-29.** A built-in extension,
`tau-agent-core/src/tau_agent_core/extensions_builtin/background.py`, tests in
`tau-agent-core/tests/test_background_extension.py`. One core addition:
`ExtensionContext.session_id`. The session-boundary question is answered (§6):
deliver, labelled. On `feat/background-tools`, unmerged; John merges it and writes
the release note.

Requested as: "build backgrounding as a generic τ extension that can be applied
to any tool. Not per-tool plumbing added to bash, then to web search, then to
recall. A wrapper any tool can be handed to, so backgrounding is a property that
can be conferred rather than a feature each tool implements separately."

Load it by path like any file extension, and name the tools in config:

```json
"extensions": {"background": {"tools": ["bash", "grep"]}}
```

## 1. Why an extension, and not a core tool

Backgrounding is a general τ faculty, not something built for one agent. It
lives in an extension because of what it does at a session boundary.

A backgrounded call can finish after the conversation that started it has ended.
When that happens its result is delivered into whatever conversation is current,
labelled with where it came from (§6). That is non-deterministic, cross-session
behaviour: whether the result lands in the same conversation depends on timing
the model never sees. Retrieval already works this way. A memory extension brings
material from other sessions into this one, and the extension is what carries
the attribution.

A native core tool doing the same would break the harness's semantics. The agent
loop is a pure function of the tree path it is given (the tree-as-truth
invariant). A core tool whose results can arrive in a different conversation
would make that loop reason about cross-session timing. The extension justifies
that behaviour and contains it. It enters the conversation through the one door,
`submit`, as an attributed extension submission, and the loop sees an ordinary
user message.

## 2. Why this is not per-tool plumbing

A tool that implements its own backgrounding owns four things that have nothing
to do with what the tool does: a handle namespace, a task lifetime, a delivery
route back into the conversation, and a cancellation story. So the unit is a
wrapper, and the wrapped tool never learns it was wrapped. `backgroundable` strips
the flag before delegating, so a call without it is indistinguishable from the
original.

Wrapping a built-in works because `AgentSession._build_turn_tools` resolves
extension tools last. Registering a tool of the same name confers the property. A
built-in named in config is constructed with `create_all_tools(ctx.cwd)`, so it
works in the session's directory rather than the process's.

## 3. The model elects it; nothing promotes

There is no time threshold. A threshold decides for the model using evidence the
model did not supply. The judgement it would replace, "can I keep going without
this answer?", belongs to the caller of the tool. So the judgement is written
where the model reads it, as the added parameter's description, and
`BACKGROUND_PARAM` is the entire policy:

> Background it when you can keep going without the answer; block on it when your
> next step depends on it.

The schema gains exactly one boolean. `_schema_with_background` deep-copies the
original, and **raises** if the tool already declares that name.

## 4. Delivery is one call for both cases

A completed call is delivered with
`api.submit(..., multitask_strategy="steer", correlation=...)`. τ's `steer`
already splits on whether a turn is running:

| State when the result lands | What `steer` does |
|---|---|
| A turn is in flight | Queued. The loop drains it immediately before its next provider call, after the current tool results. A queued steer also keeps a text-only turn going (`agent_loop.py:264-266`). |
| No turn in flight | It takes the turn slot and runs an ordinary turn. This is the wake-up. |

`api.submit` stamps `source="extension"` and `submitter=<the extension's name>`,
and neither is a parameter, so the attribution cannot be forged. `correlation`
carries `background_handle`, `tool`, `args`, `started_at_ms`, `origin_session_id`
and `crossed_session`. All three are on the RPC event schema, so **a host may
receive an `agent_end` for a turn it never prompted.** That is the one thing an
integrator has to be told.

## 5. Submission depth, which is a trap

`DRIVING_SUBMISSION_DEPTH` is a `ContextVar`. A task spawned inside a tool's
`execute` inherits it, so without intervention the depth climbs one per completion
that triggers another call, and the eleventh submission raises against
`MAX_SUBMISSION_DEPTH = 10` and kills the session. A backgrounded call is not
self-submission, so `BackgroundRegistry._supervise` sets the variable to `None` in
the spawned task. The chain test drives `MAX_SUBMISSION_DEPTH + 1` links and
asserts depth 0 inside each one.

## 6. A result outlives its session, labelled

Answered 2026-09-29 (Kevin Question Log, Q21): when the conversation ends while
work is outstanding, **deliver the result into the new one, labelled with where it
came from.** The two alternatives were waiting and cancelling. Waiting holds a
session swap hostage to a slow tool. Cancelling throws away work the model asked
for.

Each call records `ctx.session_id` when it starts. At delivery the extension reads
it again:

- **Same id:** `[background bg-recall-3] recall finished:` and the result.
- **Different id:** `[background bg-recall-3, from an earlier session] recall
  finished. It was started in session <id> at <UTC time> with arguments <json>,
  and that conversation has ended since:` and the result. `correlation` carries
  `crossed_session: true`.

The label is in the text as well as in `correlation`, because the model reads the
text and not the correlation.

Outstanding work is cancelled only on `session_shutdown`, which fires on quit,
disable and reload. A session swap fires none of those, so work survives it.

### 6.1 The first version's check never fired on a real swap

The first version kept an epoch counter, bumped on `session_start` and
`session_shutdown`, and refused delivery on a mismatch. `AgentSessionRuntime`'s
`new_session`, `fork` and `switch_session` fire **only** `session_before_switch`.
`rpc_mode.py:288,305` fires `session_start` once at startup and `session_shutdown`
once at quit. So on the RPC path the epoch never moved on a swap, and a
late completion was delivered into the new conversation **unlabelled**. The tests
passed because they modelled a swap by calling the shutdown hook.

Session identity replaces the counter because it is what the question is about.
It is read from the log that is actually in place, so no hook ordering can make it
wrong. `TestRealSessionBoundary` drives a real `AgentSession` through a real
`AgentSessionRuntime.new_session`. Under the epoch design that result
would arrive unlabelled, which the test's label assertion catches.

### 6.2 What is still lost: a result queued into a turn that is then aborted

If a result lands while a turn is in flight, `steer` queues it. Three things
clear that queue without reporting what they discarded:

- `AgentSession.abort()`, including the one `_apply_swap` sends before every swap;
- a `rollback` submission;
- `_apply_swap`'s reset.

Tectum also sends `abort` while idle to drop a stranded utterance, and that
clears a queued result too. `test_a_result_queued_into_a_turn_the_swap_aborts_is_lost`
pins this.

This extension cannot close the gap. The queue holds bare `UserMessage`s with no
submitter (`agent_session.py:2736`), so neither the clear nor the extension can
tell a background result from a human's stale utterance. The fix is in core. The
queue has to carry each item's provenance, and a clear has to either keep
extension-sourced items or report them. It is the ROADMAP item "A queued steer is
lost silently when a session is swapped", and it changes `abort` semantics that
tectum relies on, so it is John's decision.

## 7. What this does not do

- **Wrapping another extension's tool needs its definition.** `register_tool`
  raises on a duplicate name (H3), and `api.get_all_tools()` returns `ToolInfo`
  without the executable. `definitions=` takes the definition directly, which
  works when the two extensions are written together. A public way to look up
  another extension's tool is proposed separately. It would extend the command
  namespace (`docs/EXTENSION-NAMESPACE.md` §8 left tools open).
- **Two wrappers on one tool is not expressible**, for the same H3 reason.
- **No head renders the handle.** The provenance rides `details` and
  `correlation`, and nothing displays either yet.
- **Not measured against a live endpoint.** The session-boundary behaviour is
  proven against a real `AgentSession` and runtime with a stubbed provider. No
  probe ran a backgrounded call through a real model.
