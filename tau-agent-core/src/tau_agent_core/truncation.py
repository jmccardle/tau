"""Whether a completion stopped at the output cap, and what to say when it did.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3.

``stop_reason == "length"`` means the server stopped because generation reached
the ``max_tokens`` τ sent, so the content is a PREFIX rather than an answer. No
tool call in such a completion runs: :func:`without_tool_calls_if_truncated`
removes them all and records how many in
``usage.extra["dropped_partial_tool_calls"]``, and the turn ends on the partial
text (§3.2). Both halves are the report.

The reading is head-agnostic and lives here so the TUI, print mode and an RPC
host reach the same verdict from the same evidence; a head owns only the delivery
and the cap it quotes. Pure — no clock and no session state, unlike
:mod:`tau_agent_core.prompt_cache`, because one completion's stop reason is
already the whole fact.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from tau_llm.types import AssistantMessage, ToolCall

#: The ``stop_reason`` that means the output cap, not the model, ended the text.
TRUNCATED_STOP_REASON = "length"

#: Where the provider records tool calls it refused to run on a cut-off payload.
DROPPED_TOOL_CALLS_KEY = "dropped_partial_tool_calls"


class Truncation(NamedTuple):
    """How much of a completion, or of a whole turn, the output cap cut off.

    ``completions`` counts calls that stopped at the cap; ``dropped_tool_calls``
    counts the tool calls those completions lost mid-argument. A drop with no
    truncated completion is possible (an aborted stream drops calls too), which
    is why the two are counted separately rather than one implying the other.
    """

    completions: int
    dropped_tool_calls: int

    @property
    def happened(self) -> bool:
        """Whether any completion stopped at the cap."""
        return self.completions > 0


def dropped_tool_calls(usage: dict[str, Any]) -> int:
    """How many tool calls one completion lost, off its ``usage`` dict.

    0 for a completion that dropped none: the provider writes the key only when
    something was lost, so absent and zero say the same thing here even though
    the persisted transcript keeps them distinguishable.
    """
    extra = usage.get("extra")
    if not isinstance(extra, dict):
        return 0
    count = extra.get(DROPPED_TOOL_CALLS_KEY, 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def without_tool_calls_if_truncated(message: AssistantMessage) -> AssistantMessage:
    """``message`` with every tool call removed when it stopped at the output cap.

    A provider cannot say which calls the cut spared: the Anthropic SDK hands
    back a partially parsed ``input``, and JSON repair can close an open string,
    so a cut-off ``write`` arrives looking complete (docs/TRUNCATED-TOOL-CALLS.md
    §3.2). The count is added to any the provider already dropped. Any other
    stop reason, or a truncated message with no calls, is returned unchanged.
    """
    if message.stop_reason != TRUNCATED_STOP_REASON:
        return message
    kept = [block for block in message.content if not isinstance(block, ToolCall)]
    removed = len(message.content) - len(kept)
    if not removed:
        return message
    extra = message.usage.extra
    prior = extra.get(DROPPED_TOOL_CALLS_KEY, 0)
    usage = message.usage.model_copy(
        update={"extra": {**extra, DROPPED_TOOL_CALLS_KEY: prior + removed}}
    )
    return message.model_copy(update={"content": kept, "usage": usage})


def completion_truncation(message: dict[str, Any]) -> Truncation:
    """One assistant message's truncation, as a 0-or-1-completion reading.

    A message with any other ``stop_reason`` reports ``Truncation(0, 0)`` even
    when it dropped calls, because the drop is then an abort's and this says what
    the cap did.
    """
    if message.get("stop_reason") != TRUNCATED_STOP_REASON:
        return Truncation(0, 0)
    usage = message.get("usage")
    return Truncation(1, dropped_tool_calls(usage) if isinstance(usage, dict) else 0)


def truncation_from_messages(messages: list[dict[str, Any]]) -> Truncation:
    """A whole turn's truncation, summed over its assistant messages.

    For a caller holding finished messages rather than a live event stream (print
    mode, a reloaded session). Summing is right here where it is wrong for
    :func:`~tau_agent_core.prompt_cache.prompt_tokens`: two truncated completions
    are two separate losses, not one conversation counted twice.
    """
    total = Truncation(0, 0)
    for message in messages:
        if message.get("role") != "assistant":
            continue
        one = completion_truncation(message)
        total = Truncation(
            total.completions + one.completions,
            total.dropped_tool_calls + one.dropped_tool_calls,
        )
    return total


def truncation_notice(truncation: Truncation, *, max_tokens: int | None) -> str | None:
    """What to say about ``truncation``, or None when nothing was cut off.

    Args:
        truncation: The reading, from either of the two functions above.
        max_tokens: The cap actually sent on the wire, quoted so a reader knows
            which number to raise. None reports it as ``unknown`` rather than
            substituting a default — quoting 4096 at an operator whose real cap
            is something else sends them to change a number that was right.

    Returns:
        One or two sentences naming what was lost, or None.
    """
    if not truncation.happened:
        return None
    cap = "unknown" if max_tokens is None else str(max_tokens)
    subject = (
        "This completion"
        if truncation.completions == 1
        else f"{truncation.completions} of this turn's completions"
    )
    sentence = (
        f"{subject} stopped at the output cap (stop_reason: length, "
        f"max_tokens = {cap}), not at the end of an answer."
    )
    dropped = truncation.dropped_tool_calls
    if dropped:
        plural = "" if dropped == 1 else "s"
        verb = "was" if dropped == 1 else "were"
        sentence += (
            f" {dropped} tool call{plural} {verb} dropped rather than run on a "
            "truncated argument list."
        )
    return sentence
