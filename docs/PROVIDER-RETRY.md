# A request that failed before producing anything is sent again

Built 2026-10-12. When a request fails before the provider yields anything, τ sends
it again, byte-identical. That covers a 408, 429, 502, 503, 504 or 529, and a
connection that was refused or reset. It retries up to three times, honours
`Retry-After`, stops on an abort, and every retry is an event all three heads show.
This closes PLAN-0.9.3 §4.3's first bullet, which had been open since 2026-08-21.

## 1. What was wrong

- **No retry anywhere in `tau-llm`.** A 503 "loading model" from a llama.cpp
  server that was still starting failed the turn on the first attempt. A 429 from
  UnoRouter's free tier also failed at once, although that tier allows one request
  per minute per model (`experiments/backend-probe/README.md`). Each was a typed
  `ProviderHttpError` from e8a9bd9 on, so a policy could branch on the status
  rather than match a string (`PROVIDER-HTTP-ERRORS.md` §6 left the policy out).
- **One wire did retry, silently.** `AsyncAnthropic` was built with the SDK's
  default `max_retries`, which is 2 in the installed `anthropic` 1.0.0
  (`anthropic._constants.DEFAULT_MAX_RETRIES`). So an `anthropic-messages` model
  made up to three attempts and reported none of them, and the other two wires
  made one. `google-genai` 2.19.0 makes one attempt when `retry_options` is unset
  (`_api_client.retry_args(None)`), and τ never sets it.
- **A refused connection carried no type.** The OpenAI provider turned
  `httpx.ConnectError` into an in-band `ErrorEvent` with `exception=None`. A
  caller could not tell it from an SSE `error` frame after a 200.

## 2. Where the policy lives: `stream_simple`

`tau_llm.retry.retrying_stream` wraps the provider stream inside
`stream_simple`. That puts it under every caller: the agent loop,
`complete_simple` (compaction, branch summaries, an extension's
`ctx.complete()`), and JMFTS.

**Alternatives rejected:**

- *In each provider.* Three copies of one policy, and the Anthropic one would sit
  on top of the SDK's own.
- *In the agent loop.* Compaction and every `complete_simple` caller would get
  nothing, and those are the calls most likely to be long and to hit a limit.

The retry option is `options["retry"]`. It is either a `RetryPolicy` or `False`,
and `stream_simple` pops it before the provider sees the options, so it never
reaches a request body. Absent means `DEFAULT_RETRY_POLICY`. Anything else is a
`TypeError`, because a typo that silently fell back to the default would hide
the typo.

**The default is on.** A caller with its own retry loop has to opt out. JMFTS
is one: it retries at the task-queue level, and until its `llm_client.complete`
passes `retry=False` the two layers multiply (4 × its own attempts). That change
belongs to the JMFTS repo and is not made here (§6). The opposite default would leave every
TUI user, who has no retry loop, with the failure this record exists to fix.

## 3. What is retried, and the one rule that keeps it safe

**A failure is retried only if the attempt yielded nothing.** `retrying_stream`
tracks whether the provider stream produced any event. Once one has gone to the
caller, any failure is raised as before. It becomes `TURN-FAILURES.md` §2's
partial message with `stop_reason: "error"`.

**Why a mid-stream failure is not retried.** By then the caller has rendered,
and may have persisted, part of an answer. A second attempt would not resume it.
The model would sample a different completion, so the output would be duplicated
or contradicted, and nothing could tell which text came from which attempt. pi
does not retry a mid-stream drop either (PLAN-0.9.3 §4.3).

What counts as retryable (`RetryPolicy.retryable`):

| Failure | Retried | Why |
|---|---|---|
| `ProviderHttpError` 408, 429, 502, 503, 504, 529 | yes | Overload, a rate limit, a gateway or a model still loading. Each tends to clear on its own. |
| 400, 401, 403, 404, 422 | no | Permanent. Another attempt gets the same answer. |
| 500 | no | Ambiguous. llama.cpp and gateways send it for requests they cannot handle as often as for passing faults. |
| 524 | no | Cloudflare already waited 100s for this answer, and the server may still be computing it. |
| `ProviderConnectionError` | yes | Refused, reset, or closed before any response. |
| `httpx.ReadTimeout` / `anthropic.APITimeoutError` | no | The server may still be working. A retry would double its load and wait the full timeout again. |
| `ProviderStreamError` (an SSE `error` frame after a 200) | no | It has no status to judge by, as `PROVIDER-HTTP-ERRORS.md` §6 says. Named since 2026-10-15 (`RESPONSE-HEADERS.md` §5.5). |

`ProviderConnectionError(RuntimeError)` is new (`tau_llm/errors.py`). Each provider
raises it in place of the in-band `ErrorEvent`, and only while it has yielded
nothing. Its message text is unchanged, so a head that printed the old error
prints the same one now. `connection_error_from` recognises `httpx.ConnectError`,
`ConnectTimeout`, `RemoteProtocolError`, `ReadError` and `WriteError`, plus
`anthropic.APIConnectionError` less its `APITimeoutError` subclass. The SDK class
is looked up in `sys.modules`, as `PROVIDER-HTTP-ERRORS.md` §5 does for status errors.

**An SDK connection error is a connection error only when its cause chain says
so** (2026-10-21, 0.14.1). The SDK raises `APIConnectionError("Connection
error.")` for any exception from the send, not just a transport failure. A
daemon that lost `-I` imported a stale `typing_extensions` from the user site,
and the `ImportError` arrived as "Connection error." and was retried as one.
`connection_error_from` now declines an `APIConnectionError` whose chain has
causes but no `httpx.TransportError` or `OSError`. The anthropic provider's
message names the whole chain (`describe_causes`), so the failure is a
`ProviderStreamError` that reads `… APIConnectionError: Connection error. <-
ImportError: cannot import name 'sentinel' …`.

**The request is identical on every attempt.** Each attempt calls
`provider.stream_chat` with the same `model`, `messages`, `tools` and `options`
objects, which build the same body. Nothing is re-derived and nothing is dropped,
so a prompt cache keyed on the prefix still matches.
`test_every_attempt_sends_the_identical_request` compares the bytes on the wire.
This respects the rule that τ never busts the prompt cache on its own.

## 4. How long it waits

`RetryPolicy()` defaults to `max_retries=3`, `base_delay=1.0`, `max_delay=30.0`,
`max_retry_after=60.0` and `jitter=0.2`.

- **Backoff** is `base_delay · 2^(n-1)`, capped at `max_delay`, then stretched by
  up to `jitter` (×1.0–1.2). So the default waits about 1, 2 and 4 seconds. The
  jitter is there for JMFTS-style concurrency: many tasks that failed against one
  loading server should not all come back in the same millisecond.
- **`Retry-After`** replaces the backoff and is honoured exactly. `retry-after-ms`
  is read first, then `retry-after` as seconds or as an HTTP-date
  (`retry_after_seconds`). A zoneless date is GMT; a value that is not a finite
  number or a date counts as absent; a past date or negative value is 0. It reaches the policy as
  `ProviderHttpError.retry_after`, a new field that each provider fills from the
  response headers.
- **A server that asks for longer than `max_retry_after` gets no retry.** The error
  is raised at once, with a log line naming the cap. Waiting an hour on a quota
  reset inside a turn is not recovery. This is pi's `maxRetryDelayMs` rule.
- **Worst case** for the default is 3 × 60s, when the server keeps asking for a
  minute. It is visible the whole time and can be aborted.
- **The abort signal** is checked when an attempt fails and polled every 100 ms
  during a wait. An abort ends the stream with an empty `stop_reason: "aborted"`
  message, and an abort already set when the attempt failed announces no retry. That is what an abort
  before any output already produced, so no head needs a new case. The request
  is not sent again.
- **The Anthropic SDK's own retries are off** (`max_retries=0`), so τ's retry is
  the only one and every attempt is reported.

## 5. Every head sees it

Each retry produces a `tau_llm.RetryEvent` (`attempt`, `max_retries`,
`delay_seconds`, `error`). It goes out before the wait, so a head can show the wait
while it happens. It is also logged as a warning on `tau_llm.retry`. That log is
the only signal a `complete_simple` caller gets.

The agent loop turns the event into an `AgentEvent` of the new type
`provider_retry`. That event carries `attempt`, `max_retries`, `delay_seconds`,
`error` and `error_detail`, with `is_error=False` because the turn goes on. The
status being retried is `error_detail.status`, None for a connection failure. A
`status_code` field carried it until 2026-10-15, when `error_detail`
(`RESPONSE-HEADERS.md` §5.2) made it a second copy; it never shipped in a
release, so it was removed rather than kept. `WireEvent` carries the same fields, so it reaches
`tau --mode rpc` and `tau serve`, and `--mode json` writes it as a JSONL line.

The sentence comes from `tau_agent_core.provider_retry.retry_notice`, a pure function
over the event or its wire payload. For example: `HTTP 503 from model 'm' at 'u':
Loading model — retry 1 of 3 in 1.2s`. `TurnStream.feed_wire` renders it into a
`provider_retry` render event, so the local TUI and `tau --connect` take one path:

| Head | Shows |
|---|---|
| TUI | A warning toast that lasts at least the wait. No transcript box: a retry that succeeds leaves nothing to scroll back to, and one that fails ends in the existing error box. |
| Compare screen | The column status reads `retrying in Ns`. |
| REPL | One system line. |
| `tau -p` | `[τ] <notice>` on stderr in both modes. |
| `--mode json` / RPC / serve | The `provider_retry` event itself. |

## 6. What is absent

- **No retry after output has streamed.** §3 gives the reason.
- **No retry for side completions on the bus.** Compaction and branch summaries
  call `complete_simple`. That call retries, but its `RetryEvent` reaches no
  `AgentEvent`, so those retries show only in the log. Surfacing them would mean
  a `side_completion_*` retry field.
- **No per-model or config-file policy.** `options["retry"]` is the only control.
  A `retry` key in `~/.tau/config.json` would be a separate decision.
- **No retry of an in-band error frame**, including Anthropic's `overloaded_error`
  sent after a 200. Neither has an HTTP status for the policy to judge.
- **JMFTS is not updated.** Its `llm_client.complete` should pass `retry=False`
  (§2), and it maps only a bare `Exception` from `complete_simple` to its
  `LlmCallError`. A refused connection used to arrive that way, as an in-band
  error; it is now a `ProviderConnectionError` and propagates past that mapping,
  as a `ProviderHttpError` already has since e8a9bd9.
- **No `aiohttp` errors for `google-genai`.** `google-genai` uses `aiohttp` instead
  of `httpx` when `aiohttp` is installed, and its connection errors are not
  recognised.
- **No `x-should-retry` header.** pi reads it. The probe's 2026-08-21 results
  predate its retry-header recording, so τ has no measurement of any endpoint
  sending it.
- **No protocol version bump.** `provider_retry` is a new event type with three new
  optional fields (a fourth, `status_code`, was removed before release; §5).
  `tau serve` stays at 0.8 and RPC at 2.1. Whether an added event type is a bump
  is a release decision: a serve client's hello refuses any version mismatch
  below 1.0.
