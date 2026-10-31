"""The walk as turns: the rules every provider shares (`model.md` 10.5 to 10.7).

`turns(walk)` reads a walk once and returns provider-neutral turns. A provider maps each turn to
its own wire shape and never decides which nodes count, whether calls are runnable, how a partial
call reads, or when a stop is noted. Private to the package: the shapes may change with the next
provider. The text markup (`call_markup`, `outcome_markup`, `stop_note`) is shared so every provider
shows the model the same thing.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import cast

from tau_next._core import Body, Node, Outcome, Part, Stop
from tau_next.protocols import SampleError
from tau_next.providers._wire import Json, has_lone_surrogate, strict_loads

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
        case Outcome.Interrupted():
            return "Interrupted"
        case _:
            raise SampleError(f"an outcome this provider cannot send: {outcome}")


def result_line(call_id: str, text: str) -> str:
    """One result as the text the model reads; `text` is an `outcome_text`."""
    return f"[tool result {call_id}: {text}]"


def outcome_markup(call_id: str, outcome: Outcome) -> str:
    """A result with no batch to answer, as the text the model reads."""
    return result_line(call_id, outcome_text(outcome))


def stop_note(note: str) -> str:
    """The text for an `AssistantTurn.note`."""
    return f"[reply stopped: {note}]"


def system_note(text: str) -> str:
    """A `System` node after the walk's start, for a provider whose one system instruction
    cannot hold it: user text, since "from here on" is what the node means."""
    return f"[system note: {text}]"


def call_object(call: Part.Call) -> Json | None:
    """The arguments of a call as the object a wire `input` or `args` must be: `args_raw`
    parsed (no arguments is `{}`); `None` if it is not a JSON object (a call cut off, one with
    a NaN or a lone surrogate, or one another provider let through)."""
    if not call.args_raw.strip():
        return {}
    try:
        parsed: object = strict_loads(call.args_raw)
    except ValueError:  # a NaN or infinity is not JSON
        return None
    if has_lone_surrogate(call.args_raw, parsed):
        return None
    return cast("Json", parsed) if isinstance(parsed, dict) else None


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


def _base_turns(walk: Sequence[Node]) -> list[Turn]:
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


def _results_text(results: ToolResults) -> str:
    """A batch's results as `outcome_markup` lines."""
    return "\n".join(result_line(r.id, r.text) for r in results.results)


def turns(
    walk: Sequence[Node],
    *,
    structured: bool = True,
    text_from: Callable[[list[Turn]], int | None] | None = None,
) -> list[Turn]:
    """The turns of a walk. `Config` and `Custom` nodes never reach the model. Raises
    `SampleError` for a `Results` that does not answer its batch call for call.

    A runnable batch can still be one the provider's API would refuse, and a walk that makes
    the API refuse fails every later sample on the branch (`model.md` 10.7). So a batch goes as
    text, its `Results` becoming a `UserTurn` of result lines, when: `structured` is false (the
    request offers no tools, so the API takes no calls); a call's `args_raw` is not a JSON
    object (see `call_object`); or its index is at or past the one `text_from`
    names. `text_from` gets the turns as first read and returns an index or `None`: a rule
    only the provider can judge (Anthropic's manual thinking)."""
    read = _base_turns(walk)
    start = None if text_from is None else text_from(read)
    out: list[Turn] = []
    as_text = False
    for i, turn in enumerate(read):
        if isinstance(turn, AssistantTurn) and turn.runnable:
            as_text = (
                not structured or (start is not None and i >= start) or not _object_calls(turn)
            )
            out.append(replace(turn, runnable=False) if as_text else turn)
        elif isinstance(turn, ToolResults) and as_text:
            out.append(UserTurn(_results_text(turn)))
            as_text = False
        else:
            out.append(turn)
    return out


def _object_calls(turn: AssistantTurn) -> bool:
    return all(call_object(p) is not None for p in turn.parts if isinstance(p, Part.Call))


class Messages:
    """Wire messages built turn by turn. `items_key` is the key of a message's list (`content`,
    `parts`). Neighbouring turns of one role are one message (the APIs want roles to
    alternate), and a turn with nothing to send leaves none, so what it separated merges."""

    def __init__(self, items_key: str) -> None:
        self._key = items_key
        self._instruction: list[str] = []
        self.out: list[Json] = []

    def add(self, role: str, items: list[Json]) -> None:
        if not items:
            return
        if self.out and self.out[-1]["role"] == role:
            cast("list[Json]", self.out[-1][self._key]).extend(items)
        else:
            self.out.append({"role": role, self._key: items})

    def system(self, text: str, item: Callable[[str], Json]) -> None:
        """A `System` turn: the instruction while no message has been made, else a user `item`
        holding `system_note(text)`."""
        if self.out:
            self.add("user", [item(system_note(text))])
        else:
            self._instruction.append(text)

    @property
    def instruction(self) -> str | None:
        """The leading `System` texts, blank-line separated; `None` if there were none."""
        return "\n\n".join(self._instruction) if self._instruction else None
