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


@agent_facing(topic="providers")
class ProviderHttpError(RuntimeError):
    """An endpoint answered a completion request with a non-2xx status.

    Every provider raises this for an HTTP refusal, and
    ``AssistantMessageEventStream.result()`` re-raises it with its type intact.
    ``str()`` is ``"HTTP <status> from model <id> at <base_url>: <message>"``.

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
    ) -> None:
        """Record the refusal; ``body`` is bounded here, so pass it whole.

        Args:
            status_code: The HTTP status.
            message: The server's error text.
            body: The raw response body, any length.
            model_id: The requested model id.
            base_url: The endpoint the request went to.
        """
        self.status_code = status_code
        self.message = message
        self.body = truncate_error_text(body)
        self.model_id = model_id
        self.base_url = base_url
        self.retry_after = retry_after
        super().__init__(
            f"HTTP {status_code} from model {model_id!r} at {base_url!r}: "
            f"{message or '(empty response body)'}"
        )


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
        self.model_id = model_id
        self.base_url = base_url
        super().__init__(message)


_HTTPX_CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


def connection_error_from(
    exc: BaseException, *, message: str, model_id: str, base_url: str
) -> ProviderConnectionError | None:
    """The ProviderConnectionError for a pre-response transport failure; None otherwise.

    The caller must only ask while nothing has been yielded. ``anthropic``'s
    ``APIConnectionError`` is looked up, not imported, as in
    :func:`http_error_from_anthropic`; its ``APITimeoutError`` subclass is excluded.
    """
    sdk = sys.modules.get("anthropic")
    is_sdk_connection = (
        sdk is not None
        and isinstance(exc, sdk.APIConnectionError)
        and not isinstance(exc, sdk.APITimeoutError)
    )
    if not (is_sdk_connection or isinstance(exc, _HTTPX_CONNECTION_ERRORS)):
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
    exc: BaseException, *, model_id: str, base_url: str
) -> ProviderHttpError | None:
    """Translate an ``anthropic.APIStatusError`` into a :class:`ProviderHttpError`.

    The SDK module is looked up, not imported: an exception from an SDK that was
    never loaded cannot be one of its types.

    Args:
        exc: Any exception the SDK call raised.
        model_id: The requested model id.
        base_url: The endpoint the request went to.

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
    )


def http_error_from_google(
    exc: BaseException, *, model_id: str, base_url: str
) -> ProviderHttpError | None:
    """Translate a ``google.genai.errors.APIError`` into a :class:`ProviderHttpError`.

    Args:
        exc: Any exception the SDK call raised.
        model_id: The requested model id.
        base_url: The endpoint the request went to.

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
