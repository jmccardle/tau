# Response headers reach the caller

Built 2026-10-13. Every provider now hands back the HTTP response headers: on
`DoneEvent.response_headers` (and `AssistantMessageEventStream.response_headers`)
for a success, and on `ProviderHttpError.headers` for a refusal.

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

## 5. Not built

- **No parsing beyond `Retry-After`.** τ parses that one header, for its own
  retry (`PROVIDER-RETRY.md`), and does not interpret any vendor's rate-limit
  counters. Their formats differ per vendor and change without notice; the
  consumer that acts on them owns that table. A retried refusal's headers reach
  the caller on `RetryEvent.error`.
- **No headers on an in-band `ErrorEvent`.** An SSE `error` frame arrives after
  a 200, whose headers are already on the success path's state but are not
  reported, because no consumer has asked.
- **No request id on `AssistantMessage`.** `request-id` is among the headers; to
  promote it to a persisted field would be §2's trade made the other way, and it
  needs its own reason.
