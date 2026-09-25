# Backgrounding is a property conferred on a tool

Status: **built 2026-09-25** as `examples/ext_kit/background.py`, tests in
`tau-coding-agent/tests/test_ext_kit_background.py`. **No change to any `src`
tree.** One product question is open and the code refuses rather than guessing
it — §6.

Requested as: "build backgrounding as a generic τ extension that can be applied
to any tool. Not per-tool plumbing added to bash, then to web search, then to
recall. A wrapper any tool can be handed to, so backgrounding is a property that
can be conferred rather than a feature each tool implements separately."

## 1. Why this is not per-tool plumbing

A tool that implements its own backgrounding owns four things that have nothing
to do with what the tool does: a handle namespace, a task lifetime, a delivery
route back into the conversation, and a cancellation story. Three tools
implementing it separately is three of each, and the third one is where they stop
agreeing. So the unit is a wrapper, and the wrapped tool never learns it was
wrapped: `backgroundable` strips the flag before delegating, and a call with
`background` absent or false is indistinguishable from calling the original.

The mechanism that makes this possible already existed and is not new here.
`AgentSession._build_turn_tools` resolves extension tools last, which is
"a documented precedence with a fixed winner" whose docstring names
`ext_kit.steer.wrap_tool` as the reason it is preserved. Registering a tool of
the same name *is* conferring the property.

## 2. The model elects it; nothing promotes

There is no time threshold, and the absence is the design. A threshold decides
for the model using evidence the model did not supply — elapsed milliseconds —
in place of the judgement it is standing in for, which is "can I keep going
without this answer?" Only the caller of the tool holds that. So the judgement is
written where the model reads it, as the added parameter's description, and
`BACKGROUND_PARAM` is the entire policy:

> Background it when you can keep going without the answer; block on it when your
> next step depends on it.

The schema gains exactly one boolean. `_schema_with_background` deep-copies the
original (which in the built-in case is a shared instance's dict) and **raises**
if the tool already declares that name, because a flag meaning two things is
worse than a flag that is refused.

## 3. Delivery is one call for both cases

A completed call is delivered with
`api.submit(..., multitask_strategy="steer", correlation=...)`, and that single
call covers both of the behaviours asked for, because τ's `steer` already splits
on whether a turn is running:

| State when the result lands | What `steer` does |
|---|---|
| A turn is in flight | Appended to `_pending_steer_messages`; the loop's `_deliver_steer` drains it immediately before the next provider call, **after** the current tool results are appended |
| No turn in flight | "There is nothing to steer, and the next LLM call is the one this submission is about to make" — it takes the turn slot and runs an ordinary turn |

The second row is the wake-up, and it needed no new mechanism. Two behaviours
already correct and worth not re-implementing: a steer left undelivered because
the turn ended first (a `terminate`-ing tool, `max_turns`) **stays queued** for
the next turn rather than being dropped, and `abort`/`rollback` **do** drop it,
because the turn it was aimed at no longer exists.

## 4. Attribution is unforgeable, and already on the wire

`api.submit` stamps `source="extension"` and `submitter=<the extension's own
name>` from the caller's runner bucket. Both are deliberately **not parameters**
— an extension cannot claim to be a human or to be another extension. All three
of `source`, `submitter` and `correlation` are fields on the RPC event schema,
so an RPC host distinguishes a τ-originated turn from one it prompted with no new
event type and no new verb. `correlation` carries the handle, the tool, the
arguments as the model gave them, and the start time.

The consequence for a host: **it may receive an `agent_end` for a turn it never
prompted.** That is a contract statement, not a protocol change, and it is the
one thing an integrator has to be told.

## 5. Submission depth, which is a trap

`DRIVING_SUBMISSION_DEPTH` is a `ContextVar`. A task spawned inside a tool's
`execute` inherits it, so `next_submission_depth` climbs one per
completion-that-triggers-another-call and the eleventh submission **raises**
against `MAX_SUBMISSION_DEPTH = 10`, killing the session.

A backgrounded call is not self-submission: the work was already running when the
turn that reads its result began. So `BackgroundRegistry._supervise` sets the
variable to `None` inside the spawned task's own context. The test drives
`MAX_SUBMISSION_DEPTH + 1` links and asserts the depth observed inside every one
is 0; mutating the clear to inherit instead fails it at `{1} == {0}`, so the test
has teeth rather than passing either way.

## 6. The open question, and why the code refuses

**What should happen to backgrounded work at a session boundary — wait for it,
cancel it, or deliver it into the new session with attribution?** Undecided.

It matters because a backgrounded call can outlive its session, and delivering
into a session that did not ask for it attributes the work to a transcript that
never contained the originating call — which breaks attribution at exactly the
point attribution was required. There is no public way to ask an `ExtensionAPI`
whether its session is still the current one, so the registry keeps a counter it
owns: bumped on `session_start` and `session_shutdown`, recorded when a call
starts, compared on completion. A mismatch raises `BackgroundSessionGoneError`
through the registry's error sink and **does not deliver**.

The default sink re-raises. A dropped completion is the failure this module
exists to prevent, so silence is not an available default — an extension with a
notify surface passes its own sink.

### 6.1 The epoch covers one half of the boundary, not both

Measured while integrating with tectum's `responder3`, which calls `new_session`
at the start of each turn. `AgentSessionRuntime._apply_swap` calls
`session.abort()` **unconditionally**, and `AgentSession.abort()` clears
`_pending_steer_messages` — so a swap always empties the steering queue, whether
or not a turn was in flight.

That gives two orderings, and only one is caught:

| Ordering | Caught? |
|---|---|
| turn ends → `new_session` (epoch bumps) → completion submits | **Yes.** Epoch mismatch, `BackgroundSessionGoneError`, nothing delivered. |
| turn ends → completion submits (epoch still matches, message sits in the queue) → `new_session` clears the queue | **No.** Silent loss. |

The second row cannot be closed from inside an extension. The submit *succeeded*;
what happens afterwards is τ discarding a queue the extension cannot observe, and
there is no "your steer was cleared" hook to learn it from. Both orderings have a
test, including the uncovered one, so the limitation is a pinned fact rather than
something a reader has to rediscover. The fix is the persistent-session work — a
session that outlives the turn never reaches either ordering.

Anyone relying on the epoch to make a session boundary loud should read this
table first: it makes the *later* completion loud, not the *earlier* one.

## 7. What this does not do

- **Two wrappers on one tool is not expressible.** `ExtensionRegistry.register_tool`
  **raises** on a duplicate name (H3), so backgrounding and gating cannot both be
  conferred on the same tool. Conferring backgrounding on N different tools is
  fine. Composing two properties on one tool would need either one wrapper that
  does both or a registry that composes, and neither is built.
- **Wrapping another extension's tool needs that extension's cooperation.**
  `api.get_all_tools()` returns `ToolInfo` — name, description, parameters,
  source — and **not** the executable, so there is no route from a tool name to a
  callable for an extension-registered tool. `backgroundable(..., definition=...)`
  takes the definition directly instead, which works when the two extensions are
  written together and not otherwise. A public accessor returning the resolved
  definition would remove the restriction and is deliberately not added here: it
  is a change to a released package's surface, and this record's whole scope is
  `examples/`.
- **No head renders the handle.** The provenance rides `details`, which is the
  correct channel for facts that are not what the model reads, and which nothing
  currently displays.
- **Not measured against a live endpoint.** Every claim above is read from source
  or proven by the unit tests; no probe ran a backgrounded call through a real
  model.
