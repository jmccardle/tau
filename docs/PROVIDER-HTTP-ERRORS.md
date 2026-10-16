# A refusal carries its status

Built 2026-10-12. A non-2xx from any provider now reaches every caller as
`tau_llm.ProviderHttpError`, with `status_code` as a field, not only in the message text.
This is requirement R1 of JMFTS's tokenization requirements (JMFTS
`.archive/TOKENIZATION_REQUIREMENTS.md` Part 4), and it is the τ half of JMFTS
issues #8 and #32.

## 1. What was wrong

The OpenAI provider turned a non-200 into
`ErrorEvent(message=f"HTTP {status} from model …")`. `AssistantMessageEventStream.result()`
then raised `Exception(message)`, and the agent loop raised `RuntimeError(message)`.
The status existed only inside a string.

JMFTS calls `complete_simple`. Its `classify_exception` branches on exception type,
so every LLM refusal took its default, `RETRYABLE`. A context-length 400 is
permanent, but it used up three retries before it failed (JMFTS #8). A 503
"model is loading" was indistinguishable from "no LLM configured" (JMFTS #32).
JMFTS 0.6.2 worked around this by matching `^HTTP (\d{3}) from model ` against τ's
message text. Its own `task_errors` docstring forbids deciding retry policy that way.

The Anthropic and Google providers were worse. They wrapped the SDK's status error in
`"Streaming error from model …: BadRequestError: …"`, which left the status in
no fixed position at all.

## 2. Raised, not yielded

The non-200 branch of both OpenAI transports now `raise`s, and the generator's
catch-all re-raises `ProviderHttpError` next to `ConstraintViolation`. The
mechanism was already in place: `AssistantMessageEventStream._collect` stores a
raised exception as `_error_exc`, and `result()` re-raises it with its type intact.

The alternative was to add a `status` field to `ErrorEvent` and keep yielding. It
loses for one reason. `result()` and `complete_simple` would still raise a bare
`Exception`, and every caller would need to know to go and look at the event.

**Cost, stated:** a caller that iterates `provider.stream_chat()` directly, rather
than through `AssistantMessageEventStream`, used to receive an `ErrorEvent` for a
non-200. It now gets a raised `ProviderHttpError`. Nine τ tests did exactly this
and were rewritten.

## 3. `ErrorEvent.exception`, so iterating consumers keep the type

The agent loop does not call `result()`. It iterates events and raised
`RuntimeError(event.message)`. `_collect` now puts the original exception on the
`ErrorEvent` it enqueues (`exception=e`), and the agent loop raises that object
when it is present. An in-band error, such as an SSE `error` frame after a 200 or a
dropped connection, still has `exception=None` and still raises `RuntimeError`.
That changed on 2026-10-15: it is now a `ProviderStreamError`
(`RESPONSE-HEADERS.md` §5.5).

`ProviderHttpError` subclasses `RuntimeError`, as `ConstraintViolation` does. Any
`except RuntimeError` around a turn therefore behaves exactly as before.

## 4. The message text did not change

`str(ProviderHttpError)` is byte-identical to 0.12.0's `ErrorEvent.message` for
the OpenAI provider: `HTTP <status> from model '<id>' at '<base_url>': <message>`.
`test_the_message_keeps_the_0_12_prefix_that_callers_already_parse` pins the
prefix that JMFTS's regex reads. So JMFTS keeps working before it switches to the
field, and switches by deleting its regex.

The Anthropic and Google texts did change, from `Streaming error … BadRequestError`
to the same `HTTP …` form. One shape for all three providers is the point of the
change, so this break is deliberate.

## 5. SDK errors are recognised by class, looked up rather than imported

`http_error_from_anthropic` and `http_error_from_google` (`tau_llm/errors.py`)
check `isinstance` against `sys.modules["anthropic"].APIStatusError` and
`sys.modules["google.genai.errors"].APIError`. If the SDK was never loaded, no
exception can be one of its types, so the check costs no import and needs no
`try`. Neither SDK is a τ dependency, so an import-based check would have needed
one of those.

The body is the raw response text when it is readable. Otherwise it is the SDK's
parse of that text, serialised back to JSON. A streaming `anthropic` response's
body may not have been read when the error is raised.

## 6. What is absent

- **No retry policy.** τ reports the status. Deciding that a 503 is worth a
  retry belongs to the caller (JMFTS #32 does it), because τ cannot know the
  caller's budget. **Superseded the same day by `PROVIDER-RETRY.md`:** τ now
  retries a transient status by default, and a caller with its own budget passes
  `retry=False`.
- **No classification of in-band errors.** An SSE `{"error": …}` frame after a 200
  has no HTTP status to carry. It stays an `ErrorEvent` with `exception=None`.
  Built later: `ProviderStreamError` (`RESPONSE-HEADERS.md` §5.5) types it,
  without claiming a status the response did not have.
- **No "context too long" subtype.** Servers phrase it differently, and a
  subtype keyed on that phrasing would be the string match this change removes.
  `status_code == 400` plus `message` is what callers get.
- **R2, R5, R6** of the same requirements document are tracked in `ROADMAP.md`.
