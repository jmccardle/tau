"""The walk as turns: the rules every provider shares (`model.md` 10.5 to 10.7).

`turns(walk)` reads a walk once and returns provider-neutral turns. A provider maps each turn to
its own wire shape and never decides which nodes count, whether calls are runnable, how a partial
call reads, or when a stop is noted. Private to the package: the shapes may change with the next
provider. The text markup (`call_markup`, `outcome_markup`, `stop_note`) is shared so every provider
shows the model the same thing.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from tau_next._core import Body, Node, Outcome, Part, Stop
from tau_next.protocols import SampleError

# The stops a reply is noted for when its calls cannot run (model.md 10.6).
_STOP_NOTES: dict[Stop, str] = {
    Stop.Length: "length",
    Stop.Interrupted: "interrupted",
    Stop.Error: "error",
}


@dataclass(frozen=True)
class SystemTurn:
    """A `System` node."""

    text: str


@dataclass(frozen=True)
class UserTurn:
    """A `User` node, or a `Results` node that answers no batch (the conversion that keeps the
    walk legal, 10.5): its outcomes are in `text` as `outcome_markup` lines."""

    text: str


@dataclass(frozen=True)
class AssistantTurn:
    """An `Assistant` node. `parts` are in reply order, `Thinking` included (a provider decides
    whether to send one, with `replay_for`). `runnable` is true when the reply is a batch with a
    `Results` node right after it: only then do its `Call` parts go as calls; otherwise a
    provider writes each as text with `call_markup`. `note` is the stop note to append after
    such text (`stop_note`), set only when the reply has calls that are not runnable."""

    parts: tuple[Part, ...]
    runnable: bool
    note: str | None


@dataclass(frozen=True)
class CallResult:
    """One call's outcome. `text` is what the model reads; `error` is true unless the call
    returned a value."""

    id: str
    tool: str
    text: str
    error: bool


@dataclass(frozen=True)
class ToolResults:
    """A `Results` node under its batch: one `CallResult` per call, in the batch's call order."""

    results: tuple[CallResult, ...]


Turn = SystemTurn | UserTurn | AssistantTurn | ToolResults


def call_markup(call: Part.Call) -> str:
    """A call that cannot run, as the text the model reads."""
    return f"[tool call {call.id}: {call.tool} {call.args_raw}]"


def outcome_text(outcome: Outcome) -> str:
    match outcome:
        case Outcome.Ok(value=value):
            return value
        case Outcome.Err(error=error):
            return f"Error: {error}"
        case _:
            return "Interrupted"


def outcome_markup(call_id: str, outcome: Outcome) -> str:
    """A result with no batch to answer, as the text the model reads."""
    return f"[tool result {call_id}: {outcome_text(outcome)}]"


def stop_note(note: str) -> str:
    """The text for an `AssistantTurn.note`."""
    return f"[reply stopped: {note}]"


def _calls(body: Body) -> list[Part.Call]:
    if not isinstance(body, Body.Assistant):
        return []
    return [p for p in body.parts if isinstance(p, Part.Call)]


def _is_batch(body: Body | None) -> bool:
    return isinstance(body, Body.Assistant) and body.stop == Stop.Tools and bool(_calls(body))


def _assistant(body: Body.Assistant, answered: bool) -> AssistantTurn:
    runnable = answered and _is_batch(body)
    note = _STOP_NOTES.get(body.stop) if _calls(body) and not runnable else None
    return AssistantTurn(tuple(body.parts), runnable, note)


def _results(body: Body.Results, batch: Body | None) -> Turn:
    if isinstance(batch, Body.Assistant) and _is_batch(batch):
        calls = _calls(batch)
        got = [call for call, _, _ in body.outcomes]
        if [c.id for c in calls] != got:
            raise SampleError(
                f"a Results answers calls {got}, but its batch made {[c.id for c in calls]}"
            )
        return ToolResults(
            tuple(
                CallResult(c.id, c.tool, outcome_text(o), not isinstance(o, Outcome.Ok))
                for c, (_, o, _) in zip(calls, body.outcomes, strict=True)
            )
        )
    return UserTurn("\n".join(outcome_markup(call, o) for call, o, _ in body.outcomes))


def turns(walk: Sequence[Node]) -> list[Turn]:
    """The turns of a walk. `Config` and `Custom` nodes never reach the model. Raises
    `SampleError` for a `Results` that does not answer its batch call for call."""
    bearing = [n.body for n in walk if not isinstance(n.body, Body.Config | Body.Custom)]
    out: list[Turn] = []
    for i, body in enumerate(bearing):
        following = bearing[i + 1] if i + 1 < len(bearing) else None
        previous = bearing[i - 1] if i > 0 else None
        match body:
            case Body.System(text=text):
                out.append(SystemTurn(text))
            case Body.User(text=text):
                out.append(UserTurn(text))
            case Body.Assistant():
                out.append(_assistant(body, isinstance(following, Body.Results)))
            case Body.Results():
                out.append(_results(body, previous))
            case _:
                raise SampleError(f"a body this provider cannot send: {body}")
    return out
