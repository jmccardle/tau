"""Building test text of a stated TOKEN size rather than a stated character size.

A compaction fixture needs a conversation big enough to cross a threshold. Every
one of them used to say so in characters — ``"x" * 40_000``, with a comment
working out that this is 10000 tokens at four characters each. When the counter
changed (docs/TOKEN-ACCOUNTING.md), eleven such fixtures silently stopped
crossing the threshold they were built to cross, and eleven tests failed for a
reason none of them was about.

The fix is to say the size in the unit the threshold is in. These helpers take a
token count and return text that costs at least that much, measured with the same
counter the harness will use.
"""

from __future__ import annotations

from tau_agent_core.compaction import count_message
from tau_llm.tokens import TextCounter


def text_costing_at_least(
    tokens: int,
    *,
    role: str = "user",
    counter: TextCounter | None = None,
    word: str = "lorem",
) -> str:
    """Repeated ``word`` whose counted cost under ``role`` is at least ``tokens``.

    Grows geometrically and then trims back, so the result is close to the target
    rather than wildly over it — a fixture that overshoots by 10x makes every test
    using it slower for no added coverage.

    Args:
        tokens: The minimum token cost of the returned text.
        role: Message role to price it under; roles have separate weights.
        counter: Counter to measure with. Defaults to the harness's own.
        word: The repeated unit. Change it to shift the character mix.

    Returns:
        A string of space-separated ``word`` repetitions.

    Raises:
        ValueError: on a non-positive token count.
    """
    if tokens <= 0:
        raise ValueError(f"tokens must be > 0, got {tokens}")

    def cost(n: int) -> int:
        return count_message(
            {"role": role, "content": [{"type": "text", "text": " ".join([word] * n)}]},
            counter,
        ).tokens

    n = 1
    while cost(n) < tokens:
        n *= 2
    lo, hi = n // 2, n
    while lo < hi:
        mid = (lo + hi) // 2
        if cost(mid) >= tokens:
            hi = mid
        else:
            lo = mid + 1
    return " ".join([word] * lo)
