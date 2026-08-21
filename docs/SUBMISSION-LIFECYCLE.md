# Spec: the submission lifecycle — one door for every input source

**Status:** shipped 2026-07-31 → 08-05. **Date:** 2026-07-29 (original
proposal). `AgentSession.submit()` (`agent_session.py:1754`) is now the one
door: TUI (`app.py:2438` `on_input_submitted`), headless (`headless.py:583`
`run_print`), and SDK (`AgentSession.prompt()`) all funnel through it.
`multitask_strategy` covers `reject`/`enqueue`/`steer`/`rollback`/`fork`.
Provenance (`submission_id`/`source`/`submitter`/`correlation`) is on every
event (`5eff135`); `submit_threadsafe` (`agent_session.py:1431`) covers
cross-loop callers. The phasing table below is the record of what was built,
not a plan — see `ROADMAP.md`'s "Submission lifecycle" entry for the
compressed shipped-state summary.

## The problem

Today only a human typing into the TUI can start a turn, and *what submitting text means* is
defined inside a Textual event handler (`app.py:1827`, `on_input_submitted`): input history,
clearing the widget, intercepting `/compact` / `/tree` / `/fork` / `/extensions`, dispatching
extension slash commands, ensuring a session exists, appending to the working message list,
rendering the user turn, setting `is_generating`, then spawning a worker to call
`backend.stream_chat`.

Headless (`headless.py:382`, `run_print`) reaches the model by a different path. The SDK reaches it
by a third (`session.prompt`). So:

- A bus event, webhook, timer, or voice utterance has no way to originate a turn at all.
  `ctx.send_user_message(..., deliver_as="nextTurn")` parks content that runs *only* if a human
  later types.
- `ctx.prompt()` (added 2026-07-29 for `nats_bus`) originates a turn but bypasses every submission
  semantic above. Under the TUI it would run a turn the TUI never initiated and is not rendering.
- **`AgentSession.prompt()` has no concurrency guard at all.** It sets `_is_streaming = True` and
  proceeds. Two concurrent callers interleave and corrupt history. pi raises here.
- Concurrency policy is therefore reinvented per input source. `nats_bus.py` hand-rolls
  `state["turn_in_flight"]` and drops; the next extension will pick a different answer.

The requirement is **one data model that holds for interactive, headless, and embedded** (τ behind a
web server) uses.

## Prior art

### pi has most of this, and τ ported half of it

pi's seam is `AgentSession.prompt(text, options?)` (`agent-session.ts:988`) and **every** frontend
funnels through it: interactive (`interactive-mode.ts:844`), print mode (`print-mode.ts:122`), RPC
(`rpc-mode.ts:395`), SDK, and extensions (`agent-session.ts:1370`). Four things τ dropped:

| pi | τ |
|---|---|
| `sendMessage(msg, {triggerTurn: true})` — starts a turn when idle (`agent-session.ts:1303`) | absent; only `followUp`/`nextTurn` queueing |
| `InputSource = "interactive" \| "rpc" \| "extension"`, carried on `PromptOptions.source` and the `input` event (`types.ts:781`) | absent entirely |
| `prompt()` **throws** if streaming unless `streamingBehavior` given (`agent-session.ts:1033`) | no guard |
| `streamingBehavior: "steer"` — deliver after the current turn's tools, before the next LLM call | absent (`agent_session.py:1535` anticipates it) |

pi ships `examples/extensions/file-trigger.ts` — an `fs.watch` in `session_start` calling
`sendMessage(..., {triggerTurn: true})`, header comment *"Useful for external systems to send
messages to the agent."* That is the `nats_bus` pattern, sanctioned.

pi deliberately has **no** `registerInputSource()` abstraction. The capability is the injection API;
the driver is whatever code the extension writes. We keep that position.

### Five mechanisms everything else converged on

Editors, kernels, and workflow engines solved this; the agent-framework space mostly has not.

1. **One door.** VS Code: a command *"can be invoked via a keyboard shortcut, a menu item, an
   action, **or directly**"* — a handler cannot tell which. The UI is a caller of the same API
   extensions call.
2. **Provenance + correlation on every event.** Jupyter's `parent_header` is a *full copy* of the
   causing message's header, so *"clients know which messages come from their own interaction …
   and which ones are from other clients, so they can display each type appropriately."* Critically,
   IOPub re-broadcasts `execute_input` — **the submission itself** — which is exactly "an extension
   submitted a turn and the TUI must show it."
3. **Capability declared per-request, not per-host.** Jupyter `allow_stdin`: *"Some frontends do not
   support stdin requests. If this is true, code running in the kernel can prompt the user for
   input."* One process can serve an interactive client and a batch job at once; a global
   `is_interactive` flag cannot express that.
4. **Concurrency policy is a named parameter on the submission.** LangGraph Platform's
   `multitask_strategy`: `enqueue` (default) / `reject` / `interrupt` / `rollback` — and the *same*
   parameter appears on the timer-driven `crons.create`, which proves it belongs on the submission
   rather than the submitter.
5. **Typed in-band refusal.** LSP `ApplyWorkspaceEditResult{applied, failureReason, failedChange}`
   is a *successful response* that says no. Refusal needs a type, not just an exception.

Temporal adds **signal-with-start**: *"if there is a running Workflow Execution with the given
Workflow Id, it will be Signaled. Otherwise, a new Workflow Execution starts and is immediately sent
the Signal."* That is the primitive τ most obviously lacks — one call that means "wake it, or join it."

## Design

### The dataclasses

```python
# tau_agent_core/submission.py

SubmissionSource = Literal[
    "interactive",  # a human at a frontend
    "rpc",          # a programmatic client over a transport
    "extension",    # an extension's own logic
    "bus",          # NATS / message bus
    "timer",        # schedule / cron
    "webhook",      # inbound HTTP
    "voice",        # speech front end
    "agent",        # τ driving itself (sub-agent, self-continuation)
]

MultitaskStrategy = Literal["reject", "enqueue", "steer", "rollback", "fork"]
#   reject   — refuse if a turn is in flight; returns accepted=False
#   enqueue  — run after the current turn finishes (τ's existing followUp/nextTurn)
#   steer    — deliver after the current turn's tool calls, before the next LLM call (pi's steer)
#   rollback — abort the in-flight turn and discard its progress, then run
#   fork     — branch at a node and run a second agent concurrently; the in-flight
#              turn is untouched. Requires NODE-ADDRESSABLE-AGENTS.md I1.
#
# rollback and fork both select a parent and proceed in another direction. They differ in what
# happens to the children already there: rollback makes them stop being the path, fork leaves
# them as a second live pointer. On an append-only log that asymmetry is the whole cost —
# fork is purely additive and rollback is not (see open question 2).

@dataclass(frozen=True)
class Submission:
    text: str
    source: SubmissionSource
    submitter: str                  # extension name, "human", channel id — WHO, not what kind
    submission_id: str              # uuid4; the parent_header analogue
    images: list[dict[str, Any]] | None = None

    multitask_strategy: MultitaskStrategy = "reject"
    # Fail-Early default. pi throws when streaming and no behaviour is named; "reject" is that,
    # as a value instead of an exception. A submitter that wants queueing says so.

    expand_commands: bool = False
    # Whether leading "/" is command-dispatched and templates/skills expanded. FALSE by default:
    # pi's sendUserMessage sets expandPromptTemplates: false, so injected text can never smuggle
    # a "/compact" through a bus payload. Interactive frontends pass True. An extension that wants
    # to compact calls ctx.compact() — the typed API — not a string.

    allow_user_input: bool = False
    # Jupyter allow_stdin. Whether code running under THIS submission may prompt a human.
    # Per-submission, not per-process: an embedded τ can serve an interactive session and a
    # cron-triggered submission simultaneously. Enforcement stays HeadlessDialogError.

    store_history: bool = True      # does this enter the durable session log
    silent: bool = False            # suppress renderer-visible output; forces store_history=False

    correlation: dict[str, Any] = field(default_factory=dict)
    # Free-form origin detail: bus subject + binding_id, cron id, HTTP request id. Carried onto
    # emitted events so a renderer can fan out to the right consumer. Free-form, but NOT
    # unchecked: __post_init__ raises on a value that is not a JSON scalar/list/dict
    # (decision 4). The failure mode being prevented is a live NATS message object riding
    # in `correlation` and detonating in the JSON renderer three hops downstream.

    depth: int = 0
    # Self-submission depth. Incremented when a submission is made from inside a turn that
    # is itself driven by a submission; submit() raises past MAX_SUBMISSION_DEPTH = 10
    # (decision 3). Neovim's shape: non-reentrant by default, opt-in nesting, hard cap.


@dataclass(frozen=True)
class SubmissionResult:
    accepted: bool                          # LSP ApplyWorkspaceEditResult.applied
    submission_id: str
    rejection_reason: str | None = None     # LSP failureReason — a RESULT, not an exception
    messages: list[dict[str, Any]] = field(default_factory=list)
```

### The one door

```python
class AgentSession:
    async def submit(self, sub: Submission) -> SubmissionResult:
        """The single admission point. TUI, headless, RPC, and every extension call this."""
```

`submit` owns, in order:

1. **Admission / concurrency.** Apply `sub.multitask_strategy` against `_is_streaming`. This is the
   guard `prompt()` never had. `reject` returns `accepted=False` with a reason — never an exception,
   never a silent drop.

   **There are two unguarded doors, not one.** `continue_conversation()` sets
   `_is_streaming = True` with no check (`agent_session.py:1284`) exactly as `prompt()` does at
   `:931`. Both must route through admission or the guard is decorative.

   Admission also **records the pre-turn leaf** — the log's cursor immediately before the user
   node is appended. It is needed for provenance regardless, and capturing it is what makes
   `rollback` cheap (decision 2).
2. **The `input` hook chain.** τ already has this (`agent_session.py:956`, S42) but it fires only
   from `prompt()`. Moving the call site here is the point of the whole design: **every input source
   gets the same parsing/transform pipeline.** The event gains `source` and `submitter` so a handler
   can branch on provenance, as pi's does. `handled` still consumes without a turn.
3. **Command dispatch**, if `expand_commands`. This is the logic that must move out of
   `on_input_submitted`.
4. **Session materialisation** — create one if absent.
5. **Persistence and rendering**, honouring `store_history` / `silent`, with the interaction
   resolved once here (`silent ⇒ store_history=False`) rather than in each renderer.
6. **Run or enqueue**, then emit lifecycle events stamped with `submission_id`, `source`, and
   `correlation`.

`prompt(text)` becomes a compatibility wrapper:

```python
async def prompt(self, text, images=None, context=None) -> list[dict[str, Any]]:
    result = await self.submit(Submission(
        text=text, source="interactive", submitter="human",
        submission_id=uuid4().hex, images=images,
        multitask_strategy="enqueue", expand_commands=True, allow_user_input=True,
    ))
    return result.messages
```

### Provenance on events

Add to `AgentEvent` (`events.py:58`): `submission_id`, `source`, `submitter`, `correlation`.

This is the highest-value single change and the cheapest. It is Jupyter's `parent_header`, and it is
what lets the TUI render a bus-initiated turn *distinctly*, lets `--mode json` attribute events,
and lets an embedded webserver fan out to the right SSE stream — **without the core knowing any
renderer exists.**

Follow Jupyter's rule, not the obvious one: a frontend filters on "is this mine?" to decide *how* to
render, and still renders the rest. Dropping other sources' events is how a multi-client session
becomes incoherent.

### The extension API

```python
# On ExtensionAPI — the injection surface.
async def submit(self, text: str, *, multitask_strategy="reject", images=None,
                 correlation=None, allow_user_input=False) -> SubmissionResult: ...
```

`source="extension"` and `submitter=<extension name>` are filled in by the binding from the caller's
own bucket — **not** accepted as arguments, the same unforgeability `ExtensionAPI.emit` already has
for `ext_channel` names. An extension cannot claim to be a human.

`ctx.prompt()` (added this session) becomes a deprecated alias for `submit(..., source="extension")`.
`nats_bus.py` deletes its `turn_in_flight` flag and passes `multitask_strategy="reject"` —
same behaviour, now one implementation instead of one per extension.

No `register_input_source()`. Following pi: the driver is whatever the extension writes in
`session_start`. Adding a registry buys a lifecycle we would have to maintain and does not buy the
extension anything it cannot do with a `session_start` handler and a coroutine.

### Task marshalling

A NATS callback, a timer, and a webhook handler all fire in a context the session does not own.
Neovim names this exactly (`:h api-fast`): most API functions are *deferred* — queued on the main
loop — and calling an editor-state function from a fast context is a **hard error** (`E5560`), fixed
with `vim.schedule_wrap`. Textual has the same split (`post_message` / `call_from_thread`).

So: `submit()` is safe only from the session's own loop. Add `submit_threadsafe(sub)` which enqueues
onto a queue the session drains, and make `submit()` **raise** when called from a foreign
loop/thread rather than working by accident. A silent fallback here produces exactly the
"bus disconnected randomly" class of bug that is investigated as a network problem for hours.

### Hook vocabulary: decision vs observation

τ's `ExtensionRunner` already splits `HOOK_EVENTS` (mutating, arbitrated) from `LIFECYCLE_EVENTS`
(notify). Formalise the consequence, following pluggy's `historic` trade — *"a historic call allows
for all newly registered functions to receive all hook calls that happened before their
registration"* but *"you can not receive results back"*:

- **Decision hooks** (`input`, `tool_call`, `context`) return values, are arbitrated, and are
  **not replayable**.
- **Observation hooks** (the lifecycle events) ignore return values and **are replayable to a
  late-attaching subscriber.**

That partition is what makes a client attaching mid-turn coherent instead of silently missing state
— the "works in the TUI, no-ops for the web frontend" failure class.

### Parity enforcement

Two mechanical checks, both stolen:

- **Round-trip property test** (pytest's `pytest_report_to_serializable`): every `AgentEvent` and
  `Submission` survives `model_dump()` → reconstruct. If it cannot, it is carrying a live object and
  will break the JSON and webserver renderers.
- **Naming convention as capability declaration** (Sphinx's `html-*` prefix): TUI-shaped surfaces
  (`ExtensionUI.panel`, `set_status`) get a prefix so a grep finds every parity violation. Sphinx
  needs no capability query because the name *is* the query.

### Deliberately not adopted

**No blocking wait-for-input inside a turn.** AutoGen shipped it as
`UserProxyAgent(input_func=...)` and then documented that mid-run use *"put[s] the team in an
unstable state that cannot be saved or resumed."* External input enters at the turn edge — τ's
existing `_end_of_prompt_drain` boundary (`agent_session.py:981`) — via `enqueue`/`steer`, never by
parking the loop.

**No advisory reentrancy flag.** Claude Code's `Stop` hook can block stopping, which re-fires
`Stop`; the guard is a `stop_hook_active` payload flag that *"does not prevent loops
automatically."* Prefer Neovim's shape: non-reentrant by default, opt-in nesting, hard depth cap.
A submission made from within a turn driven by the same submitter needs a depth bound in the core.

## Phasing

| Phase | Content | Why first |
|---|---|---|
| 1 | `Submission`/`SubmissionResult`, `submit()`, concurrency policy, `prompt()` as wrapper | Fixes a live defect: `prompt()` has no guard today |
| 2 | Provenance on `AgentEvent` + round-trip parity test | Cheapest, highest leverage; unblocks any second renderer |
| 3 | Move command dispatch + history out of `on_input_submitted` into `submit()`; TUI becomes renderer + one source | The actual "usage lifecycle" unification |
| 4 | `steer` mode; `submit_threadsafe` + foreign-loop raise | Needed for voice and for correctness under real drivers |
| 5 | `nats_bus` drops `turn_in_flight`; `ctx.prompt` → `ctx.submit` | Proves the seam by deleting the workaround |

Phases 1–2 are independently valuable and do not touch the TUI.

**`fork` belongs in phase 2, ahead of `steer` and `rollback`.** It is the only strategy that
cannot corrupt an in-flight turn, because it mutates no active path — the worst it can do is
start from a bad prefix, and that is checkable at admission. Its data model is also already
built and shipped (`BranchView`, `open_branch`, `LANE_KEY`, `ctx.spawn_branch`), so the cost is
not tree work; it is lifecycle (a supervised task registry — `spawn_branch` is awaited by its
caller and a fork submission has none), the non-tree session state a fork does not inherit
(extensions, abort signal, usage), and the renderer contract (`backends.py:200` `stream_chat`
is single-stream by construction, and nothing yet subscribes to the `branch_event` channel, so
a fork today is unobservable). The first two are core work; the third is separable TUI work.

The concrete admission check: a fork point must be a turn-complete entry. Forking at an
assistant message whose `toolCall` blocks have no matching `toolResult` entries yields a prefix
most providers reject outright.

## Decisions taken

1. **`interactive` defaults to `enqueue`.** pi's TUI binds Enter→steer and Alt+Enter→followUp, so
   the TUI eventually needs both — but `steer` is phase 4, and the strategy is a per-submission
   parameter, so the second keybinding is a one-line change at the call site when it lands. Not a
   design question, a phasing one.

2. **`rollback` is suffix-drop, and erases nothing.** Abort the in-flight turn, move the cursor to
   the pre-turn leaf recorded at admission, append there. The abandoned nodes stay in the log and
   fall off the `parentId` walk — which is what `append_branch_summary` already does
   (`session_log.py:262`), minus the summary. Of the three shapes in
   `NODE-ADDRESSABLE-AGENTS.md` §3 this is the one that needs no surgery and mints no ids, so it
   is the one `rollback` promises. Document the consequence plainly: a tree browser still shows the
   abandoned branch, because append-only means nothing was un-said — only un-pathed.

   Because the only new state it needs (`pre_turn_leaf`) is captured in phase 1 anyway, `rollback`
   lands in **phase 2** alongside `fork` rather than waiting for phase 4.

3. **Self-submission depth is a hard cap that raises.** `Submission.depth`, module constant
   `MAX_SUBMISSION_DEPTH = 10`, and `submit()` raises past it — not a silent drop, and not an
   advisory flag (see *No advisory reentrancy flag* above). Not a constructor parameter until
   something actually needs a different bound; `AgentSession.__init__` already takes fifteen.

4. **`correlation` stays free-form, and is validated at construction.** Typing it would mean
   enumerating origins we have not built (webhook, voice), and a tree browser can render
   `dict[str, str]` generically. What makes free-form safe is the round-trip parity test above,
   whose real content is "no live objects" — so enforce that at `__post_init__`, where the
   traceback names the culprit, rather than at the renderer that inherits the corpse.
