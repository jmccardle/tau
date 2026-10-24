"""Where a turn stands, kept up to date so a suspension can record its continuation.

docs/STEP-DEBUGGER.md §3.7.3. A resume is written down at raise time, never
inferred from the leaf, because the tree cannot say which loop a turn was in,
how far a dispatch or a write group had got, or what the loop does next. The
agent loop and the session keep a :class:`LoopPosition` current in
:data:`LOOP_POSITION`; ``api.suspend`` snapshots it into the suspension's side
entry. Milestone 1 records it; milestone 2's resume reads it.
"""

from __future__ import annotations

import contextvars
import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

Then = Literal["tools", "turn_end", "sample", "break", "end_prompt", "nothing", "raise"]
"""What follows the dispatch a suspension was raised in (§3.7.3's ``then``).

``tools``: the batch's pending calls run. ``turn_end``: the batch is complete
and ``turn_end`` follows, then the next sample. ``sample``: the loop re-enters
at its top. ``break``: the loop ends, after a text-only turn's ``turn_end``.
``end_prompt``: the prompt's tail (follow-ups, compaction, ``user_turn_end``).
``nothing``: no turn follows. ``raise``: the turn ends in the stored error.
"""

LoopName = Literal["run", "run_continue"]


@dataclass
class LoopPosition:
    """A turn's position, as the loop and the session advance it.

    Attributes:
        loop: Which loop runs the turn.
        turn_index: The loop's own counter.
        prev_batch: The repeat-call limit's last batch signature.
        repeat_count: How many times in a row that signature has repeated.
        then: What follows the dispatch running now.
        calls: The batch's calls with no result written yet, in order, each
            ``{id, prepared, effective_args, blocked}``.
        group: The write group being dispatched, by entry id.
        index: The entry of ``group`` being dispatched.
        returned: Messages ``turn_end``/``user_turn_end`` handlers returned so
            far in the dispatch running now, not yet in the tree.
        pending_abort: A turn-ending failure already decided and not yet taken.
    """

    loop: LoopName = "run"
    turn_index: int = 0
    prev_batch: tuple[str, bool] | None = None
    repeat_count: int = 1
    then: Then = "sample"
    calls: list[dict[str, Any]] = field(default_factory=list)
    group: list[str] | None = None
    index: int | None = None
    returned: list[Any] | None = None
    pending_abort: str | None = None

    def start_batch(self, call_ids: list[str]) -> None:
        """Record a new batch: every call pending and none prepared."""
        self.calls = [
            {"id": call_id, "prepared": False, "effective_args": None, "blocked": None}
            for call_id in call_ids
        ]

    def prepared(
        self, call_id: str, effective_args: Any = None, blocked: str | None = None
    ) -> None:
        """Record that ``call_id``'s ``tool_call`` hooks all ran, and what they decided."""
        for call in self.calls:
            if call["id"] == call_id:
                call.update(
                    prepared=True, effective_args=copy.deepcopy(effective_args), blocked=blocked
                )

    def answered(self, call_id: str) -> None:
        """Record that ``call_id``'s result is written."""
        self.calls = [call for call in self.calls if call["id"] != call_id]

    def snapshot(self, phase: str, handler: int, gates: list[list[Any]]) -> dict[str, Any]:
        """The continuation a suspension records (§3.7.3's table).

        Args:
            phase: ``dispatch:<hook>``.
            handler: How far the suspending dispatch had got.
            gates: The loaded ``tool_call`` handlers' ``[extension, version]``.

        Returns:
            ``{phase, loop, turn_index, repeat, pending_abort, then,
            pending_calls, gates, handler}``, plus ``group``/``index`` for an
            ``entry_written`` phase and ``returned`` for ``turn_end`` and
            ``user_turn_end``.
        """
        continuation: dict[str, Any] = {
            "phase": phase,
            "loop": self.loop,
            "turn_index": self.turn_index,
            "repeat": {
                "prev_batch": None if self.prev_batch is None else list(self.prev_batch),
                "count": self.repeat_count,
            },
            "pending_abort": self.pending_abort,
            "then": self.then,
            "pending_calls": copy.deepcopy(self.calls),
            "gates": gates,
            "handler": handler,
        }
        if phase == "dispatch:entry_written":
            continuation["group"] = list(self.group or [])
            continuation["index"] = self.index
        if phase in ("dispatch:turn_end", "dispatch:user_turn_end"):
            continuation["returned"] = copy.deepcopy(self.returned or [])
        return continuation


LOOP_POSITION: contextvars.ContextVar[LoopPosition | None] = contextvars.ContextVar(
    "tau_loop_position", default=None
)
"""The running turn's :class:`LoopPosition`; hooks and the tasks they await see it."""


def current_position() -> LoopPosition | None:
    """The running turn's position, or ``None`` outside one."""
    return LOOP_POSITION.get()


@contextmanager
def positioned(position: LoopPosition) -> Iterator[LoopPosition]:
    """Publish ``position`` as the running turn's for the body.

    Args:
        position: The position to publish.

    Yields:
        ``position``.
    """
    token = LOOP_POSITION.set(position)
    try:
        yield position
    finally:
        LOOP_POSITION.reset(token)


def set_then(then: Then) -> None:
    """Set what follows the dispatch about to run, when a position is published."""
    position = LOOP_POSITION.get()
    if position is not None:
        position.then = then
