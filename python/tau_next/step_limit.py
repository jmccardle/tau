"""`StepLimit`: the extension that stops a cursor from stepping on forever."""

from tau_next._core import ItemData
from tau_next.handles import Cursor, Subject


class StepLimit:
    """Turns a cursor's auto-step off after `limit` steps that moved it, counted
    since a frontend last sent it input (D1).

    A step counts if its cursor moved, which every commit does. The count starts
    over at a new input item (seen in `pre_step`, `pre_input`, or `post_step`) and
    when this extension stops the cursor, so whoever resumes it with `set_auto`
    or `step` gets another `limit`. It cannot see those two calls, nor
    `go_to`; core keeps no count for it (the count is this object's memory).
    Counts are per cursor. List it after any extension that turns auto-step back
    on from `post_step`, or that hook's `set_auto(True)` wins.
    """

    name = "step_limit"

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"a step limit must be at least 1, not {limit}")
        self.limit = limit
        self._counts: dict[int, int] = {}
        self._inputs: dict[int, int] = {}

    def _count_new_input(self, cursor: Cursor) -> None:
        """Start over if an input item arrived that no hook has seen."""
        newest = self._inputs.get(cursor.id, -1)
        for item in cursor.items():
            if isinstance(item.data, ItemData.Input) and item.id > newest:
                self._inputs[cursor.id] = newest = item.id
                self._counts[cursor.id] = 0

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self._count_new_input(cursor)
        return None

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        self._count_new_input(cursor)
        return None

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        self._count_new_input(cursor)
        if not cursor.moves(since_step=True):
            return None
        taken = self._counts.get(cursor.id, 0) + 1
        if taken >= self.limit:
            cursor.set_auto(False)
            taken = 0
        self._counts[cursor.id] = taken
        return None
