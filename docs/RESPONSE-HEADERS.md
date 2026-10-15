# Response headers reach the caller

Built 2026-10-13. Every provider now hands back the HTTP response headers: on
`DoneEvent.response_headers` (and `AssistantMessageEventStream.response_headers`)
for a success, and on `ProviderHttpError.headers` for a refusal. Built
2026-10-15 (§5): the agent loop passes both on, as `AgentEvent.response_headers`
on `message_end` and `AgentEvent.error_detail` on `agent_end` and
`provider_retry`, to extensions and to all three heads.

## 1. What was wrong

None of the three providers read a response header. `grep response.headers`
across `tau_llm/providers/` returned nothing on 2026-10-13. The per-request
quota counters vendors send on success were discarded: `x-ratelimit-*` on
OpenAI-compatible endpoints and `anthropic-ratelimit-*` on Anthropic. So was
every refusal header except one: `PROVIDER-RETRY.md`, merged the next day,
reads `Retry-After` into the parsed `ProviderHttpError.retry_after` and nothing
else.

A caller that has to respect quotas can do one of two things without these
headers. It can guess a cooldown, which is a fabricated number. Or it can
wrap the HTTP client underneath τ, which the Anthropic and Google paths make
awkward, because each SDK owns its own transport.

The first consumer is outside this repository: a cost-aware router built on
τ-llm that picks among several accounts by remaining quota. Nothing in τ reads
these fields yet, which is the "value written and read by nobody" shape
(`.claude/skills/feature-doc`). That is deliberate: the reader is downstream.

## 2. On the event, not the message

The headers ride on `DoneEvent`, not on `AssistantMessage`. An
`AssistantMessage` is persisted in every session log. A header map on it would
write about 1 KB of transport detail per turn into JSONL that is replayed for
the life of the session, and it would be stale the moment it was written. The
event is transient. `AssistantMessageEventStream` keeps a copy so a caller using
`result()` can still read it.

## 3. None is not empty

`None` means the transport did not capture headers. `{}` means the server sent
none. They are different facts, and folding one into the other is the
substitution Fail-Early forbids. Names are lowercased once, in
`tau_llm.errors.response_headers_of`, so a caller does not need a
case-insensitive mapping.

These events carry `None`:
- the `DoneEvent` that `AssistantMessageEventStream` synthesizes when a provider
  ends without one;
- a Google stream whose chunks carry no `sdk_http_response`.

## 4. Where each provider reads them

| Provider | Success | Refusal |
|---|---|---|
| OpenAI completions | `httpx.Response.headers`, in both the SSE and the buffered transport, onto `_TransportState` | the same response, in `_http_error_from_response` |
| Anthropic | `MessageStream.response`, read once on entry | `APIStatusError.response` |
| Google | the first chunk's `sdk_http_response` | `APIError.response` |

## 5. On the agent events

The 2026-10-13 build stopped at `tau-llm`. `AgentLoop._stream_response` built
`message_end` from `DoneEvent.final` and dropped `DoneEvent.response_headers`;
its message dict had five keys (`role`, `content`, `usage`, `model`,
`stop_reason`). A refusal reached `agent_end` as the string `error`, for example
`"ProviderHttpError: HTTP 429 from model 'm' at 'u': ..."`. A quota-governing
extension, the reader §1 names, had to parse the status out of that string and
could not see `Retry-After` or the counters at all. The same was true of an RPC
host and of `tau -p --mode json`.

### 5.1 `message_end.response_headers`

`AgentEvent.response_headers: dict[str, str] | None` is set on the
`message_end` that closes a completion, the one carrying `usage` and
`stop_reason`. The values are `DoneEvent.response_headers` unchanged, so §3
holds: `None` means the transport did not capture them, and `{}` means the
server sent none. Every other event has `None`.

**A top-level field, not a key in `message`.** That dict mirrors the persisted
`AssistantMessage` field for field, and readers treat it that way:
`_record_completion_usage`, `TurnStream` and the wire's `_truncation_fields` all
take message keys as message facts. A header map among them would be the one
key that must never be written, with nothing in its shape to say so, against
§2. As a separate field it has no path to the writer: `writer.finalize`
receives `DoneEvent.final`, which never had the headers.
`test_the_session_log_never_holds_the_headers` checks the log.

### 5.2 `error_detail`

`AgentEvent.error_detail: ErrorDetail | None` is set exactly when `error` is set:
on an `agent_end` that closed because the loop raised, and on every
`provider_retry`. `ErrorDetail` (`tau_agent_core.events`) has five fields:

| Field | Type | Set when |
|---|---|---|
| `type` | `str` | Always. The exception's class name, e.g. `"ProviderHttpError"`, `"ProviderConnectionError"`, `"ValueError"`. |
| `status` | `int \| None` | `ProviderHttpError`: its `status_code`. |
| `retry_after` | `float \| None` | `ProviderHttpError` whose server asked for a wait: its parsed `retry_after`. |
| `headers` | `dict[str, str] \| None` | `ProviderHttpError` whose transport read headers: its `headers`. |
| `retryable` | `bool \| None` | `ProviderHttpError` or `ProviderConnectionError`: `DEFAULT_RETRY_POLICY.retryable(exc)`. |

`ErrorDetail.of(exc)` builds it. `retryable` calls `RetryPolicy.retryable`
instead of copying its status list, so `PROVIDER-RETRY.md` §3's table stays the
only one. On `agent_end`, `retryable=True` means the failure was transient but
τ's retries ran out, or the server asked for longer than `max_retry_after`;
`retry_after` says which. For any other exception, `retryable` is `None`: τ makes
no judgement about a `ValueError` from a tool-call parser, and `False` would be
a judgement. A host still learns what failed without parsing `error`, because
`type` names the class.

`provider_retry` already had `status_code`. It gets `error_detail` too because a
retried 429's counters are exactly what a quota governor reads, and
`RetryEvent.error` had them with nothing passing them on.

**Rejected: separate top-level fields** (`error_status`, `error_headers`, …).
They would add five fields to every event type for two of them, and
`status_code` already means "the status being retried" on `provider_retry`.
One nested value keeps the error's facts together and leaves `status_code` as
it is.

### 5.3 Every head

| Head | How the fields arrive |
|---|---|
| An extension, or any bus subscriber, including the TUI | `api.on("message_end", …)` and `api.on("agent_end", …)` receive the `AgentEvent`, with headers unfiltered. |
| `tau --mode rpc`, `tau serve` | `WireEvent.response_headers` and `WireEvent.error_detail`, built by `rpc/wire_events.py`. Both names and types match `AgentEvent`'s, so `test_shared_scalar_fields_have_matching_types` covers them against drift. |
| `tau -p --mode json` | The JSON line is `AgentEvent.model_dump(exclude_none=True)`, so both fields appear, with `None` keys left out as on every other line. |

Both wire fields are additive and ride the unreleased RPC 2.2 / serve 0.9 bump
(`TAU-SERVE.md` §5); this build does not bump either version.

### 5.4 Cookies stop at the process boundary

Both machine-readable outputs drop `set-cookie` and `set-cookie2` from
`response_headers` and from `error_detail.headers`
(`rpc.wire_events.strip_cookies`, which `json_mode_payloads` calls too). Every
other header crosses.

A cookie's only legitimate reader is the HTTP client that received it, and that
client is inside τ. An RPC line or a JSONL line leaves the process: it lands in
a host's log, in a CI capture of `tau -p`, or with a serve client on another
machine. A Cloudflare bot cookie (`__cf_bm`) is the common case on vendor APIs.
No consumer reads it, so dropping it costs nothing.

**Not an allowlist.** The consumer owns the table of vendor headers (§6), and an
allowlist would make τ decide which headers it may see. **Not filtered
in-process.** An extension runs with the transport's own privileges and could
read the cookie from the client anyway, so filtering there would hide data
without protecting anything.

## 6. Not built

- **No parsing beyond `Retry-After`.** τ parses that one header, for its own
  retry (`PROVIDER-RETRY.md`), and does not interpret any vendor's rate-limit
  counters. Their formats differ per vendor and change without notice; the
  consumer that acts on them owns that table. A retried refusal's headers reach
  the caller on `RetryEvent.error`, and on `provider_retry`'s `error_detail`.
- **No headers on an in-band `ErrorEvent`.** An SSE `error` frame arrives after
  a 200, whose headers are already on the success path's state but are not
  reported, because no consumer has asked.
- **No request id on `AssistantMessage`.** `request-id` is among the headers; to
  promote it to a persisted field would be §2's trade made the other way, and it
  needs its own reason.
- **No `error_detail` on a `message_end` with `stop_reason: "error"`.** The
  partial message a mid-stream failure leaves (`TURN-FAILURES.md` §2) carries
  `error_message` as before; the same failure's fields arrive on the
  `agent_end` that follows it.
- **No headers on the content-only duplicate `message_end`.** The loop emits a
  second `message_end` for a tool-bearing completion, with content alone. It
  gets no `response_headers`, as it gets no `usage` or `stop_reason`.
- **`retryable` is the default policy's judgement.** The agent loop does not
  set `options["retry"]`, so `DEFAULT_RETRY_POLICY` is the policy that ran. A
  caller of `stream_simple` with its own `RetryPolicy` builds no `AgentEvent`
  and is not affected.
