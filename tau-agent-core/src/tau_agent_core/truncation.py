"""Whether a completion stopped at the output cap, and what to say when it did.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3.

``stop_reason == "length"`` means the server stopped because generation reached
the ``max_tokens`` τ sent, so the content is a PREFIX rather than an answer. No
tool call in such a completion runs: the agent loop keeps each with its partial
arguments, answers it with :func:`unrun_tool_result_text` and ends the turn,
and ``usage.extra["dropped_partial_tool_calls"]`` counts every call the cap cost
(§3.2). Both halves are the report.

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

#: How many tool calls a completion did not run: kept and refused, or dropped.
DROPPED_TOOL_CALLS_KEY = "dropped_partial_tool_calls"


class Truncation(NamedTuple):
    """How much of a completion, or of a whole turn, the output cap cut off.

    ``completions`` counts calls that stopped at the cap; ``dropped_tool_calls``
    counts the tool calls in them that did not run. A drop with no truncated
    completion is possible (an aborted stream drops calls too), which is why the
    two are counted separately rather than one implying the other.
    ``reasoning_tokens`` is how much of the cut completions' output was
    reasoning, ``None`` when no provider reported a split
    (docs/REASONING-WIRE.md §7).
    """

    completions: int
    dropped_tool_calls: int
    reasoning_tokens: int | None = None

    @property
    def happened(self) -> bool:
        """Whether any completion stopped at the cap."""
        return self.completions > 0


def dropped_tool_calls(usage: dict[str, Any]) -> int:
    """How many tool calls one completion did not run, off its ``usage`` dict.

    0 when the key is absent: it is written only when a call went unrun, so
    absent and zero say the same thing here even though the persisted
    transcript keeps them distinguishable.
    """
    extra = usage.get("extra")
    if not isinstance(extra, dict):
        return 0
    count = extra.get(DROPPED_TOOL_CALLS_KEY, 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


def with_unrun_tool_calls_counted(message: AssistantMessage) -> AssistantMessage:
    """``message`` with its tool calls counted as not run, when it stopped at the cap.

    The calls stay in the message with whatever arguments the provider
    recovered; the agent loop answers each with :func:`unrun_tool_result_text`
    and ends the turn (docs/TRUNCATED-TOOL-CALLS.md §3.2). The count is added to
    any the provider already dropped. Any other stop reason, or a truncated
    message with no calls, is returned unchanged.
    """
    if message.stop_reason != TRUNCATED_STOP_REASON:
        return message
    calls = len(message.get_tool_calls())
    if not calls:
        return message
    extra = message.usage.extra
    prior = extra.get(DROPPED_TOOL_CALLS_KEY, 0)
    usage = message.usage.model_copy(
        update={"extra": {**extra, DROPPED_TOOL_CALLS_KEY: prior + calls}}
    )
    return message.model_copy(update={"usage": usage})


def _last_string(value: Any, path: str) -> tuple[str, str] | None:
    """The path and text of ``value``'s last leaf in document order, if it is a string."""
    if isinstance(value, dict):
        if not value:
            return None
        key, last = list(value.items())[-1]
        return _last_string(last, f"{path}.{key}" if path else str(key))
    if isinstance(value, list):
        if not value:
            return None
        return _last_string(value[-1], f"{path}[{len(value) - 1}]")
    if isinstance(value, str):
        return path, value
    return None


def unrun_tool_result_text(
    message: AssistantMessage, call: ToolCall, *, max_tokens: int | None
) -> str:
    """The error result a tool call in a cut-off completion is answered with.

    The call that is the completion's last block is the one the cap cut; its
    last argument, when that is a string, is named with its length. Any other
    call finished its arguments before the cut.

    Args:
        message: The completion, whose ``stop_reason`` is ``"length"``.
        call: One of its tool calls.
        max_tokens: The cap the request was sent with; None reads ``unknown``.
    """
    cap = "unknown" if max_tokens is None else str(max_tokens)
    if not message.content or message.content[-1] is not call:
        return (
            f"Not run: your output reached max_tokens = {cap} later in this response, "
            "so none of its calls were executed. This call's arguments are complete."
        )
    last = _last_string(call.arguments, "")
    where = f" ({last[0]} holds {len(last[1]):,} characters so far)" if last else ""
    return (
        f"Not run: your output reached max_tokens = {cap} while writing this call's "
        f"arguments, so they are cut off{where} and nothing was executed. "
        "Write it in smaller pieces."
    )


def completion_truncation(message: dict[str, Any]) -> Truncation:
    """One assistant message's truncation, as a 0-or-1-completion reading.

    A message with any other ``stop_reason`` reports ``Truncation(0, 0)`` even
    when it dropped calls, because the drop is then an abort's and this says what
    the cap did.
    """
    if message.get("stop_reason") != TRUNCATED_STOP_REASON:
        return Truncation(0, 0)
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return Truncation(1, 0)
    return Truncation(1, dropped_tool_calls(usage), reasoning_tokens_of(usage))


def reasoning_tokens_of(usage: dict[str, Any]) -> int | None:
    """The reasoning share of one completion's output, off its ``usage`` dict, or ``None``."""
    count = usage.get("reasoning_tokens")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


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
        reasoning = (
            total.reasoning_tokens
            if one.reasoning_tokens is None
            else (total.reasoning_tokens or 0) + one.reasoning_tokens
        )
        total = Truncation(
            total.completions + one.completions,
            total.dropped_tool_calls + one.dropped_tool_calls,
            reasoning,
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
    if truncation.reasoning_tokens:
        sentence += f" {truncation.reasoning_tokens:,} of those output tokens were reasoning."
    dropped = truncation.dropped_tool_calls
    if dropped:
        plural = "" if dropped == 1 else "s"
        verb = "was" if dropped == 1 else "were"
        sentence += f" {dropped} tool call{plural} {verb} not run."
    return sentence
