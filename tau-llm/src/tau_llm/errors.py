"""Typed provider failures, so a caller can branch on a status instead of a message.

See docs/PROVIDER-HTTP-ERRORS.md.
"""

from __future__ import annotations

import json
import sys
from typing import Any

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
        message: The server's own error text, or ``""`` when the body had none.
        body: The raw response body, bounded to ``MAX_ERROR_BODY_CHARS``.
        model_id: The requested model id.
        base_url: The endpoint the request went to.
    """

    def __init__(
        self,
        status_code: int,
        *,
        message: str,
        body: str,
        model_id: str,
        base_url: str,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.body = truncate_error_text(body)
        self.model_id = model_id
        self.base_url = base_url
        super().__init__(
            f"HTTP {status_code} from model {model_id!r} at {base_url!r}: "
            f"{message or '(empty response body)'}"
        )


def http_error_from_anthropic(
    exc: BaseException, *, model_id: str, base_url: str
) -> ProviderHttpError | None:
    """The ProviderHttpError for an ``anthropic.APIStatusError``; None for anything else.

    The SDK module is looked up, not imported: an exception from an SDK that was
    never loaded cannot be one of its types.
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
    )


def http_error_from_google(
    exc: BaseException, *, model_id: str, base_url: str
) -> ProviderHttpError | None:
    """The ProviderHttpError for a ``google.genai.errors.APIError``; None for anything else."""
    sdk = sys.modules.get("google.genai.errors")
    if sdk is None or not isinstance(exc, sdk.APIError) or not isinstance(exc.code, int):
        return None
    return ProviderHttpError(
        exc.code,
        message=str(exc.message or ""),
        body=_body_text(exc.response, exc.details),
        model_id=model_id,
        base_url=base_url,
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
