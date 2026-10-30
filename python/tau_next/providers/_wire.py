"""HTTP and JSON helpers a provider over SSE needs: private to the package.

Written for the Anthropic provider. `openai.py` holds older copies of `retry_after`, `plain`,
`non_finite`, and `is_number`; it moves here when a third provider needs them too.
"""

import math
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

Json = dict[str, object]
HTTP_OK = 200


def retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header: whole seconds as sent, an HTTP-date as
    seconds from now (0 if past); `None` for no header or anything else."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def plain(value: object) -> object:
    """`value` as plain JSON: `ModelConfig` options are read-only mappings and tuples."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in cast("Mapping[str, object]", value).items()}
    if isinstance(value, tuple | list):
        return [plain(v) for v in cast("tuple[object, ...]", value)]
    return value


def non_finite(value: object) -> list[float]:
    """Every NaN or infinity inside `value`."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [value]
    if isinstance(value, Mapping):
        return [b for v in cast("Mapping[str, object]", value).values() for b in non_finite(v)]
    if isinstance(value, tuple | list):
        return [b for v in cast("tuple[object, ...]", value) for b in non_finite(v)]
    return []


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def is_count(value: object) -> bool:
    """A non-negative integer (not a boolean)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """The `data` of each SSE event: its `data:` lines joined by newlines. The body is read to
    its end, so the connection goes back to the pool."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            if line.startswith("data:"):
                lines.append(line.removeprefix("data:").removeprefix(" "))
            continue
        if lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)
