# `api.request`: the emitter owns the outcome

Built 2026-10-24 (#98). An extension that asks its subscribers for an answer gets
one reply per subscriber, under its own deadline. A subscriber's own policy
cannot turn a failure into a pass.

## 1. What was wrong

An extension that needed a decision from another (tau-router asking a decider
which model to route to, or tau-arecibo asking a classifier) had only
`await api.emit(topic, payload)`, which gave it four problems.

- **It returns `None`.** A subscriber's return value was discarded. The only
  way to answer was to mutate the payload, which cannot say who answered, and
  where two subscribers answer, the last write wins.
- **The subscriber picks the failure semantics.** The policy is chosen at
  `api.on(..., policy=)` (STEP-DEBUGGER.md §4). A decider subscribed under
  `advisory` turns its own timeout into `Proceeded`, so `emit` returns normally
  and the router routes by default. That breaks John's rule for deciders: when
  no decider is configured, route as today; when a configured decider times out
  or crashes, that is an error, never hidden behind default routing. The
  emitter had no way to enforce that rule.
- **An unpoliced subscriber's error never reaches the emitter.** The bus
  isolates it (`EventBus.emit_channel`, S44).
- **The emitter cannot ask whether anyone is listening.**
  `EventBus.has_listeners` is on the bus, which an extension does not hold.

## 2. The call

```python
replies = await api.request("decide", payload, timeout_s=5)
# [RequestReply(extension="tau_decider", outcome=Returned("route-b"), status="returned", abandoned=False)]
n = api.subscriber_count("decide")
```

`request` publishes on the requester's own `ext:<name>:<topic>`, as `emit`
does, and returns one `policies.RequestReply` per subscriber, in subscription
order:

| `status` | `outcome` | when |
|---|---|---|
| `"returned"` | `Returned(value)` | the handler returned; `value` is its **return value**, not the payload |
| `"expired"` | `Failed(DeadlineExpired)` | the request's deadline fired; `abandoned` is true when a sync handler's thread was left running (#97) |
| `"raised"` | `Failed(error)` | the handler raised; `error` is the exception itself, type and message intact, and a `DeadlineExpired` from a deadline of the handler's own counts here |

`extension` names the subscriber, so a caller can say which decider failed.
With no subscribers the result is `[]`, and whether that is an error is the
caller's decision. `subscriber_count` is complete by `session_start`, because
every extension subscribes at load.

## 3. The emitter's terms replace the subscriber's policy

For this call, each subscriber runs its bare handler (`ChannelSubscriber.handler`)
under a fresh `PausableDeadline(timeout_s)`, and a timeout and an error each
give `Failed`. The subscriber's registered policy is not consulted, so an
`advisory` subscriber cannot produce `Proceeded`. A custom policy's retry is
skipped too: the requester has already said what it will wait for. `emit` keeps
running each subscriber through its own policy, unchanged.

The alternative was to keep the subscriber's policy and add a `strict=True` to
`emit` that refused `Proceeded`. That still lets a subscriber set a 15 s budget
for an emitter that can wait 2 s, and it still returns nothing.

To read `handler` and `extension` back, `api.on` now stores an `ext:`
subscription as an `events.ChannelSubscriber`. The bus calls it, which runs
`dispatch`, the handler or its policy wrapper, so `emit` is unchanged.

## 4. Concurrent: the total is bounded by `timeout_s`

Every subscriber starts at once, each in its own task under its own deadline,
so the whole call takes at most `timeout_s` of the subscribers' own work.
Running them in turn would make N slow subscribers cost N × `timeout_s`, and
tau-router and tau-arecibo both ruled that out. A concurrent request also
cannot let one subscriber see another's answer. That is deliberate:
subscribers are peers here, not a chain.

The deadline is the M1d one, so the §4 rules hold without new code:

- **An enclosing deadline caps the request.** A `gating` hook that requests
  with a 30 s timeout is still cut off at the hook's own deadline.
- **A suspension pauses only the clock of the subscriber that suspends**
  (§4.3). A request that reaches a human can therefore exceed `timeout_s`.
- **A sync subscriber runs on a worker thread** and is abandoned at the
  deadline (#97). Its late finish sets its own `policy:<channel>` warning.
  Once it is abandoned, every ctx or api call that acts on the session raises,
  `api.set_warning`, `ctx.shutdown` and `api.submit_threadsafe` included. Only
  the harmless reads (`read` in STEP-DEBUGGER.md §4, "Built (#97)") still
  work. Review round 2 found the first three still working after abandonment;
  the third started a real turn.
- **A sync subscriber's `StopIteration` is `"raised"` at once**, as a
  `RuntimeError` caused by it, matching an async subscriber. Before review
  round 2 it was reported `"expired"` after the full `timeout_s`.
- **Each subscriber task counts as awaited by the requester**, so
  `runs_on_behalf_of` follows it, and a subscriber that calls `ctx.prompt()`
  hits the reentrant guard at once.

## 5. Cancellation and `TurnSuspended`

If the requester is cancelled (an aborted turn, an enclosing deadline), every
subscriber task is cancelled and awaited before the cancellation propagates.
No task is left behind, and a sync subscriber's thread is abandoned per #97. A
`BaseException` from any subscriber does the same and then leaves unchanged,
`TurnSuspended` included. `request_replies` uses `gather` without
`return_exceptions`, which the M1c lint (`test_turn_suspended_lint.py`) accepts.

## 6. Scope: `ext:` channels only

`request` takes a topic and builds the requester's own channel name, so it
reaches only `ext:` channels, and only the requester's own. The other bus
channels carry `AgentEvent`s, which have notify-only subscribers with nothing
to return. `api.on` already refuses a policy on them for the same reason.

## 7. What is absent

- **No partial results on cancel.** A cancelled request raises; replies that
  arrived before the cancel are not returned, because a caller being torn down
  has nowhere to use them.
- **No per-subscriber budgets.** One `timeout_s` applies to every subscriber.
  A requester that wants different budgets makes separate requests.
- **No quorum or first-answer mode.** `request` waits for every subscriber,
  up to `timeout_s`. "First good answer wins" is a policy over the replies,
  and the caller can write it.
- **No request from a policed sync handler's worker thread.** `request` is a
  coroutine method, so it is refused there, as `emit` is (STEP-DEBUGGER.md §4,
  "Built (#97)").
