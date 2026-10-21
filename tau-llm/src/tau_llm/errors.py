"""Typed provider failures, so a caller can branch on a status instead of a message.

See docs/PROVIDER-HTTP-ERRORS.md, and docs/PROVIDER-RETRY.md for which of them
:mod:`tau_llm.retry` retries.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import math
import sys
import time
from typing import Any

import httpx

from tau_llm.docs import agent_facing

MAX_ERROR_BODY_CHARS = 4000


def truncate_error_text(text: str, max_chars: int = MAX_ERROR_BODY_CHARS) -> str:
    """Bound an error body, saying how much was dropped rather than eliding silently."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def response_headers_of(response: Any) -> dict[str, str] | None:
    """An HTTP response's headers with lowercased names; None when it has none to read.

    None means the headers were not captured, not that the server sent none. See
    docs/RESPONSE-HEADERS.md.
    """
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    return {str(k).lower(): str(v) for k, v in headers.items()}


@agent_facing(topic="providers")
class ProviderHttpError(RuntimeError):
    """An endpoint answered a completion request with a non-2xx status.

    Every provider raises this for an HTTP refusal, and
    ``AssistantMessageEventStream.result()`` re-raises it with its type intact.
    ``str()`` is ``"HTTP <status> from model <id> at <base_url>: <message>"``,
    followed by ``hint`` when there is one.

    Attributes:
        status_code: The HTTP status, e.g. 400 for an over-context prompt.
        message: The server's ``error.message``. Without one, the OpenAI wire
            uses the bounded body and the Anthropic wire the SDK's text, so it
            is ``""`` only when there was nothing to report.
        body: The raw response body, bounded to ``MAX_ERROR_BODY_CHARS``.
        model_id: The requested model id.
        base_url: The endpoint the request went to.
        retry_after: Seconds the server asked the client to wait, from a
            ``retry-after-ms`` or ``retry-after`` header; None when it sent neither.
        headers: The response headers, names lowercased, unparsed; None when the
            transport could not read them. Read the wait from ``retry_after``.
        key_sent: Whether the request carried a key; None when the raiser did
            not say. Tells "no API key" from "API key refused" on a 401.
        hint: For a 401 where the caller named the provider and whether a key
            was sent, what to set or where the refused key came from
            (``tau_llm.providers.credential_hint``); otherwise ``""``.
    """

    def __init__(
        self,
        status_code: int,
        *,
        message: str,
        body: str,
        model_id: str,
        base_url: str,
        retry_after: float | None = None,
        headers: dict[str, str] | None = None,
        provider_id: str | None = None,
        key_sent: bool | None = None,
    ) -> None:
        """Record the refusal; ``body`` is bounded here, so pass it whole.

        Args:
            status_code: The HTTP status.
            message: The server's error text.
            body: The raw response body, any length.
            model_id: The requested model id.
            base_url: The endpoint the request went to.
            retry_after: Seconds the server asked for, already parsed from its
                header; None when it named none.
            headers: The response headers, names lowercased; None when not captured.
            provider_id: The vendor id (``Model.provider``); names the hint on a 401.
            key_sent: Whether the request carried a key; None for unknown, which
                omits the hint.
        """
        from tau_llm.providers.base import credential_hint

        self.status_code = status_code
        self.message = message
        self.body = truncate_error_text(body)
        self.model_id = model_id
        self.base_url = base_url
        self.retry_after = retry_after
        self.headers = headers
        self.key_sent = key_sent
        self.hint = (
            credential_hint(provider_id, base_url, key_sent=key_sent)
            if status_code == 401 and provider_id and key_sent is not None
            else ""
        )
        text = (
            f"HTTP {status_code} from model {model_id!r} at {base_url!r}: "
            f"{message or '(empty response body)'}"
        )
        super().__init__(f"{text.rstrip('.')}. {self.hint}" if self.hint else text)


@agent_facing(topic="providers")
class ProviderConnectionError(RuntimeError):
    """The connection failed before the provider produced any event.

    Refused, reset, or closed by the server before a response. Raised only while
    nothing has streamed, so the caller has received nothing from this request
    (docs/PROVIDER-RETRY.md §3). A timeout waiting for the response is NOT one
    of these: the server may still be working on the request.

    Attributes:
        model_id: The requested model id.
        base_url: The endpoint the request went to.
    """

    def __init__(self, message: str, *, model_id: str, base_url: str) -> None:
        """Record the failure.

        Args:
            message: The provider's description of the failure; becomes ``str()``.
            model_id: The requested model id.
            base_url: The endpoint the request went to.
        """
        self.model_id = model_id
        self.base_url = base_url
        super().__init__(message)


@agent_facing(topic="providers")
class ProviderStreamError(RuntimeError):
    """The provider accepted the request, then reported a failure inside the stream.

    The usual case is an SSE ``error`` frame after an HTTP 200, such as
    Anthropic's ``overloaded_error`` or an OpenAI-compatible server's
    ``{"error": ...}`` chunk. It is also what the agent loop raises for an
    ``ErrorEvent`` that names no exception. τ never retries one
    (docs/PROVIDER-RETRY.md §3). ``str()`` is ``message``.

    Attributes:
        message: The description of the failure, naming the model and endpoint.
        model_id: The requested model id.
        base_url: The endpoint the request went to; None when the raiser did
            not know it.
        code: The frame's own error code or type, e.g. ``"overloaded_error"``
            or ``"500"``; None when the frame had none, or there was no frame.
        status: ``code`` as an HTTP status when it is an integer from 100 to
            599; None otherwise. The response itself was a 2xx.
        body: The error frame as JSON, bounded to ``MAX_ERROR_BODY_CHARS``;
            None when there was no frame.
    """

    def __init__(
        self,
        message: str,
        *,
        model_id: str,
        base_url: str | None,
        code: str | None = None,
        body: str | None = None,
    ) -> None:
        """Record the failure; ``status`` is derived from ``code``.

        Args:
            message: The description of the failure; becomes ``str()``.
            model_id: The requested model id.
            base_url: The endpoint the request went to, or None if unknown.
            code: The frame's error code or type, if it named one.
            body: The error frame, any length.
        """
        self.message = message
        self.model_id = model_id
        self.base_url = base_url
        self.code = code
        self.status = _http_like(code)
        self.body = truncate_error_text(body) if body is not None else None
        super().__init__(message)


def _http_like(code: str | None) -> int | None:
    """``code`` as an int when it reads as an HTTP status (100-599); None otherwise."""
    if code is None or not code.isdigit():
        return None
    number = int(code)
    return number if 100 <= number <= 599 else None


def stream_error_from_frame(
    error: Any, *, message: str, model_id: str, base_url: str
) -> ProviderStreamError:
    """The ProviderStreamError for the ``error`` value of an SSE ``{"error": ...}`` frame.

    ``code`` is the value's ``code``, else its ``type``; a non-object value has none.
    """
    code = None
    if isinstance(error, dict):
        raw = error.get("code") or error.get("type")
        code = str(raw) if raw is not None else None
    return ProviderStreamError(
        message,
        model_id=model_id,
        base_url=base_url,
        code=code,
        body=json.dumps(error, default=str),
    )


_HTTPX_CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


def _causes(exc: BaseException) -> list[BaseException]:
    """``exc``'s cause chain, outermost first, as a traceback would print it; ``exc`` excluded."""
    chain: list[BaseException] = []
    link = exc
    while True:
        nxt = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
        if nxt is None or nxt in chain:
            return chain
        chain.append(nxt)
        link = nxt


def describe_causes(exc: BaseException) -> str:
    """`` <- Type: text`` for each exception in ``exc``'s cause chain; ``""`` when it has none.

    A link that reads the same as the one before it is dropped: httpx2 wraps
    httpcore2's ``ConnectError`` in its own, with the same text.
    """
    links = [f" <- {type(cause).__name__}: {cause}" for cause in _causes(exc)]
    return "".join(link for i, link in enumerate(links) if i == 0 or link != links[i - 1])


def connection_error_from(
    exc: BaseException, *, message: str, model_id: str, base_url: str
) -> ProviderConnectionError | None:
    """The ProviderConnectionError for a pre-response transport failure; None otherwise.

    The caller must only ask while nothing has been yielded. ``anthropic``'s
    ``APIConnectionError`` is looked up, not imported, as in
    :func:`http_error_from_anthropic`; its ``APITimeoutError`` subclass is excluded,
    and so is one whose cause chain holds no transport or OS error, because the SDK
    wraps any exception from the send, a failed import included
    (docs/PROVIDER-RETRY.md §3). anthropic>=1.0 sends over ``httpx2``, whose
    ``TransportError`` is looked up the same way.
    """
    sdk = sys.modules.get("anthropic")
    is_sdk_connection = (
        sdk is not None
        and isinstance(exc, sdk.APIConnectionError)
        and not isinstance(exc, sdk.APITimeoutError)
    )
    if is_sdk_connection:
        transport: tuple[type[BaseException], ...] = (httpx.TransportError, OSError)
        httpx2 = sys.modules.get("httpx2")
        if httpx2 is not None:
            transport += (httpx2.TransportError,)
        causes = _causes(exc)
        if causes and not any(isinstance(c, transport) for c in causes):
            return None
    elif not isinstance(exc, _HTTPX_CONNECTION_ERRORS):
        return None
    return ProviderConnectionError(message, model_id=model_id, base_url=base_url)


def retry_after_seconds(headers: Any) -> float | None:
    """Seconds a response asked the client to wait, or None when it did not say.

    Reads ``retry-after-ms`` first, then ``retry-after`` as seconds or as an
    HTTP-date (a zoneless date is GMT, as RFC 9110 says). A value that parses to
    no finite number is None, as if absent; a past date or negative value is 0.
    """
    if headers is None:
        return None
    millis = _finite(headers.get("retry-after-ms"))
    if millis is not None:
        return max(0.0, millis / 1000.0)
    value = headers.get("retry-after")
    if value is None:
        return None
    seconds = _finite(value)
    if seconds is not None:
        return max(0.0, seconds)
    try:
        when = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, when.timestamp() - time.time())


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, or None when it is absent or not a number."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def http_error_from_anthropic(
    exc: BaseException,
    *,
    model_id: str,
    base_url: str,
    provider_id: str | None = None,
    key_sent: bool | None = None,
) -> ProviderHttpError | None:
    """Translate an ``anthropic.APIStatusError`` into a :class:`ProviderHttpError`.

    The SDK module is looked up, not imported: an exception from an SDK that was
    never loaded cannot be one of its types.

    Args:
        exc: Any exception the SDK call raised.
        model_id: The requested model id.
        base_url: The endpoint the request went to.
        provider_id: The vendor id, for the 401 hint.
        key_sent: Whether the request carried a key, for the 401 hint.

    Returns:
        The translated error, or None when ``exc`` is not an ``APIStatusError``.
    """
    sdk = sys.modules.get("anthropic")
    if sdk is None or not isinstance(exc, sdk.APIStatusError):
        return None
    payload = exc.body
    error = payload.get("error") if isinstance(payload, dict) else None
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    return ProviderHttpError(
        exc.status_code,
        message=message or str(exc),
        body=_body_text(exc.response, payload),
        model_id=model_id,
        base_url=base_url,
        retry_after=retry_after_seconds(getattr(exc.response, "headers", None)),
        headers=response_headers_of(exc.response),
        provider_id=provider_id,
        key_sent=key_sent,
    )


def stream_error_from_anthropic(
    exc: BaseException, *, model_id: str, base_url: str
) -> ProviderStreamError | None:
    """Translate an in-band ``error`` event the ``anthropic`` SDK raised.

    The SDK raises an SSE ``error`` event as an ``APIStatusError`` carrying the
    stream's own response, whose status is the 200 that opened it. That is not a
    refusal, so :func:`http_error_from_anthropic` must not see it.

    Args:
        exc: Any exception the SDK call raised.
        model_id: The requested model id.
        base_url: The endpoint the request went to.

    Returns:
        The translated error, or None when ``exc`` is not an ``APIStatusError``
        with a status below 400.
    """
    sdk = sys.modules.get("anthropic")
    if sdk is None or not isinstance(exc, sdk.APIStatusError) or exc.status_code >= 400:
        return None
    payload = exc.body
    error = payload.get("error") if isinstance(payload, dict) else payload
    text = str(error.get("message") or "") if isinstance(error, dict) else ""
    return stream_error_from_frame(
        error,
        message=f"model {model_id!r} at {base_url!r} failed mid-stream: {text or exc}",
        model_id=model_id,
        base_url=base_url,
    )


def http_error_from_google(
    exc: BaseException,
    *,
    model_id: str,
    base_url: str,
    provider_id: str | None = None,
    key_sent: bool | None = None,
) -> ProviderHttpError | None:
    """Translate a ``google.genai.errors.APIError`` into a :class:`ProviderHttpError`.

    Args:
        exc: Any exception the SDK call raised.
        model_id: The requested model id.
        base_url: The endpoint the request went to.
        provider_id: The vendor id, for the 401 hint.
        key_sent: Whether the request carried a key, for the 401 hint.

    Returns:
        The translated error, or None when ``exc`` is not an ``APIError`` or
        carries no integer status.
    """
    sdk = sys.modules.get("google.genai.errors")
    if sdk is None or not isinstance(exc, sdk.APIError) or not isinstance(exc.code, int):
        return None
    return ProviderHttpError(
        exc.code,
        message=str(exc.message or ""),
        body=_body_text(exc.response, exc.details),
        model_id=model_id,
        base_url=base_url,
        retry_after=retry_after_seconds(getattr(exc.response, "headers", None)),
        headers=response_headers_of(exc.response),
        provider_id=provider_id,
        key_sent=key_sent,
    )


def _body_text(response: Any, parsed: Any) -> str:
    """The response body as text: the raw bytes when readable, else the SDK's parse."""
    try:
        text = response.text if response is not None else None
    except Exception:  # pragma: no cover - an unread streaming body; the parse remains
        text = None
    if isinstance(text, str) and text:
        return text.strip()
    if parsed is None:
        return ""
    return parsed if isinstance(parsed, str) else json.dumps(parsed, default=str)
