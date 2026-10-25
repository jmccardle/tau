# A failing hook ends the turn it could have changed

Built 2026-10-24 (#29). An exception in a handler at a hook whose result can
change what the turn does now ends the turn with `end_reason: "error"` and an
error naming the extension and the hook. A handler at a hook that only observes
still gets reported, and the turn goes on.

## 1. What was wrong

`ExtensionRunner` caught every handler exception at the mutating hooks, passed
it to the `on_error` listeners (or printed it to stderr), and carried on with
the next handler. The turn continued as if the handler had returned nothing. An
extension that enforces something then stopped enforcing it without anyone
deciding that. tau-router's budget extension hit this: its `input` hook raised
`ModelUnavailableError` while stepping to a rung it could not build, τ printed
`[τ] warning: extension error in tau_router.router (input): …`, and the turn ran
on a model that was already over its allowance. The ledger shows a usage row
past the cap.

Measured with one unpoliced handler raising `RuntimeError` at each hook, and a
model script of a tool call followed by a text answer:

| hook | before: `end_reason` | before: model calls | after: `end_reason` | after: model calls |
|---|---|---|---|---|
| `input` | `done` | 2 | none (raised before admission) | 0 |
| `before_agent_start` | `done` | 2 | `error` | 0 |
| `tool_call` | `done` (call blocked, turn continued) | 2 | `error` | 1 |
| `tool_result` | `done` | 2 | `error` | 1 |
| `turn_end` | `done` | 2 | `error` | 1 |
| `user_turn_end` | `done` | 2 | `done`, then `submit` raises | 2 |

Before, `prompt()` returned normally in all six rows. After, it raises
`HookHandlerError` in all six.

Policies (docs/STEP-DEBUGGER.md §4) had already built the turn-ending path: a
policy's `Failed` at an isolating hook raised `HandlerPolicyError`, and the loop
ended the turn on it. An unpoliced handler, which is every handler in every
extension written before 0.15, never reached that path. This change sends it
there.

## 2. Which hooks end the turn

The rule: **a hook whose result can change what the turn does ends the turn when
its handler fails.** Losing that handler means losing whatever it enforces. A
hook whose return is ignored has nothing to enforce, so its failure is reported
and the turn goes on.

The table lives in code as two frozensets in `extensions/runner.py`,
`TURN_ENDING_HOOKS` and `OBSERVING_HOOKS`. `ExtensionRunner._dispatch` reads
them, so the table decides the behaviour and cannot drift from it.
`test_hook_errors.py::test_every_hook_is_classified_exactly_once` fails when a
hook is added to `HOOK_EVENTS` or `LIFECYCLE_EVENTS` without being classified.

| hook | class | what its result can change |
|---|---|---|
| `input` | ends the turn | the prompt; consume or refuse the submission |
| `before_agent_start` | ends the turn | the system prompt; injected messages |
| `tool_call` | ends the turn | veto the call; rewrite its arguments |
| `tool_result` | ends the turn | rewrite the result, e.g. a redactor |
| `turn_end` | ends the turn | append a durable message before the next request |
| `user_turn_end` | ends the turn | append a durable message |
| `session_before_switch` | ends the turn | veto a new session, fork or switch |
| `annotate` | ends the turn | the `ext` stored on the entry |
| `entry_written` | observes | nothing; the entry is already on disk |
| `turn_setting_changed` | observes | nothing; the change is already recorded |
| `session_start` | observes | nothing; lifecycle |
| `session_shutdown` | observes | nothing; lifecycle |

Notes on the edges:

- **`annotate` already ended the turn** (docs/ENTRY-ANNOTATIONS.md §4.3): it
  records the error on the entry and raises `AnnotationError`. It is in the set
  for completeness, and its path is unchanged.
- **`turn_end` ends the turn even for a handler that only observes.** The hook
  cannot tell an observer from a handler that would have returned a message,
  because that is decided by what the handler returns, and it raised instead.
  Use `"all"` or `AgentSession.subscribe()` for pure observation, which the
  notify bus isolates (docs/extensions.md, "The one sharp edge").
- **`session_before_switch` has no turn.** Its failure leaves the veto unknown,
  so the swap does not happen and `new_session`/`fork`/`switch_session` raises.
  Before, the swap went ahead.
- **`ext:` channel subscribers are unchanged.** An unpoliced subscriber's
  exception is surfaced and `emit` does not raise (STEP-DEBUGGER.md §4.1). A
  policed one's `Failed` raises into the emitter, and if the emitter is a
  turn-ending hook's handler that does not catch it, that ends the turn, as it
  did before.
- **`HookHandlerError` is the new exception, and `HandlerPolicyError` is now its
  subclass.** Every site that ended a turn on a policy failure catches the base
  class, so an exception and a policy failure take one path. A `HookHandlerError`
  a handler raised itself, from a nested hook, is raised unchanged and still names
  the hook that failed first.

## 3. `tool_call`: the call is answered, then the turn ends

Before, a raising `tool_call` handler blocked the call. The tool did not run,
the model got `Extension failed, blocking execution: …` as an error result, and
the turn went on. That failed closed for the one call, but the model could retry
or route around it, and the extension that failed was never told twice.

Now `prepare_tool_call` still returns the `BlockedCall`, and it carries the
failure as `BlockedCall.failure`. `AgentLoop._prepare_tool_call` notes it as the
turn's failure, and from there the path is the one `tool_result` and `annotate`
failures already take:

- **Sequential.** The failing call is answered with its block text. Every later
  call in the batch is answered `ABORTED_TOOL_RESULT` and none runs.
- **Parallel.** Preparation stops at the failing call, before anything executes.
  `_abort_prepared` answers each call prepared so far with its own reason, and
  every other call with `ABORTED_TOOL_RESULT`. That is the path an abort during
  a `tool_call` dispatch already used (STEP-DEBUGGER.md §3.6).
- The checkpoint after the batch raises the failure, so `agent_end` carries
  `end_reason: "error"`, and `_append_turn_error` writes the `turn_error` node
  (docs/TURN-FAILURES.md §4).

So the tree holds the assistant message, one result per call, and the
`turn_error` node, and the next request is valid.
`test_tool_call_answers_every_call_and_ends_the_turn` checks this in both modes.

**This amends STEP-DEBUGGER.md §4.1**, where a policy's `Failed` at `tool_call`
"blocks the call". It still blocks, and it now also ends the turn, because §4.1's
own rule is that `Failed` means what failure means at that hook, and §2 above
redefines failure at `tool_call`. Keeping `Failed` as block-only would make a
policed timeout weaker than an unpoliced exception.

A nested call (`ctx.execute_tool`) has no loop checkpoint. `run_nested_call`
raises the `HookHandlerError` into the calling tool, which is the rule a nested
`tool_result` failure already followed (STEP-DEBUGGER.md §4, "Built (M1d)").

## 4. What each head shows

Most of this needed no head code. The loop ends with `agent_end` for every
in-loop failure, and #93's `_turn_failure_on_the_wire` sends `agent_start` and
`agent_end(error)` for a failure before the loop exists, which covers
`before_agent_start`. Two hooks fall outside both, and both are listed here.

| hook | `agent_end` | TUI | `tau -p` | `--mode rpc` / `tau serve` |
|---|---|---|---|---|
| `before_agent_start`, `tool_call`, `tool_result`, `turn_end` | `end_reason: "error"`, `error: "HookHandlerError: extension 'x' 'tool_call' handler raised …"` | warning notice, then `Error: …` and an error block in the transcript | exit 1, `tau: error: extension 'x' 'tool_call' handler raised …` | `agent_end` on the wire with that error, `error_detail.type: "HookHandlerError"` |
| `input` | none: it runs before admission, so no turn started and nothing is written | as above | as above | the `prompt`/`submit` request gets an error response (`INTERNAL_ERROR`) carrying the message |
| `user_turn_end` | already sent, `end_reason: "done"`: the hook runs after the loop | as above | as above | logged by the host after admission, see §7 |
| `session_before_switch` | none | the verb's error | not reachable from `-p` | the verb's error response |

`tau -p`'s one change is in `cli.py`: `HookHandlerError` joins the provider
errors that become one `tau: error:` line and exit 1 instead of a traceback. The
`[τ] warning: extension error in …` line from the error listener comes before
it, so stderr carries the warning and then the error.

## 5. Deadline timeouts

A handler has a deadline only under a policy, so a timeout is already a policy
outcome and needs nothing new:

- Under a policy that fails on timeout (`gating`, a custom `Bounded(…, "fail", …)`),
  the timeout is `Failed(DeadlineExpired)` and raises `HandlerPolicyError`. That
  is now a `HookHandlerError`, so it ends the turn by the same path as an
  exception, `tool_call` included (§3).
- Under `advisory` or any policy that proceeds, the timeout is `Proceeded`: the
  turn goes on and the extension's `policy:<hook>` warning is set. The extension
  chose that degradation by naming the policy, and it is recorded, so it is not
  the silent loss §1 describes.
- An unpoliced handler has no deadline. It runs inline until it returns or
  raises.

## 6. The error listeners

`_dispatch` calls `_emit_error` before it raises, so every `on_error` listener
still receives every failure, and the session's listener still paints its
warning notice. One listener sees more than before: a raising `tool_call`
handler used to bypass the listeners entirely, because the runner let the
exception through and the call-site turned it into a block. It is now surfaced
like every other hook.

## 7. Tests

**Flipped**, because each asserted that a raise at a turn-ending hook let the
dispatch or the turn continue:

- `test_extension_runner.py`: `tool_call` propagated a bare `RuntimeError`, and
  `tool_result` recovered through a later handler. Both now raise
  `HookHandlerError`, and the later handler does not run.
- `test_tool_call_hook.py::test_throwing_tool_call_handler_ends_the_turn`: was
  "blocks"; the tool still does not run, and `prompt` now raises.
- `test_input_hook.py`, `test_turn_end_hook.py`: the "surfaced and continues"
  cases now raise, and later handlers do not run.
- `test_handler_policies.py`: `test_tool_call`, `test_ext_channel` and the nested
  timeout now raise for `Failed` (§3). The unpoliced `tool_result` reentrant case
  now raises `HookHandlerError`. `test_no_policy_keeps_todays_isolation` moved
  its observer from `turn_end` to `entry_written`, since the isolation it guards
  is now an observing hook's.
- `test_extension_error_visibility.py`: the three listener-routing tests are
  kept as they were and now also expect the raise. They are §6's evidence.

**Kept**: every observing-hook, lifecycle-hook, notify-bus and `ext:` channel
isolation test, unchanged.

**Added**: `test_hook_errors.py` (one case per turn-ending hook, the observing
hooks, the classification, and tree validity for `tool_call` in both modes),
`test_agent_session_runtime.py::test_a_raising_veto_check_stops_the_swap`, two
in `test_rpc_terminal_event.py`, one in `test_serve.py`, and
`test_print_hook_error.py`, which runs a real `tau -p` child.

## 8. What is absent

- **A terminal wire event for `user_turn_end`.** The hook runs after the loop
  has sent `agent_end(done)`, so an RPC or serve host gets no event saying the
  user turn then failed. The failure reaches the host's own log and the
  listener's warning. Running the hook before `agent_end` would mean moving it
  into the loop, and that is a larger change than this one.
- **A dedicated RPC error code for an `input` failure.** It answers as
  `INTERNAL_ERROR` with the message. A host that wants to branch on "an
  extension refused this" has the message text and nothing structured.
- **A way for a `turn_end` handler to declare itself an observer** and so keep
  the old isolation. See §2.
- **A strict/lenient switch.** #29 rejected a development-only strict mode,
  because it would leave production enforcement silently broken, which is the
  case that was hit.
- **Any change to `ext:` channels or the lifecycle hooks.** See §2 and §5.
