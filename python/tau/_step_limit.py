"""`StepLimit`: the extension that stops a cursor from stepping on forever."""

from dataclasses import dataclass
from typing import Annotated

from tau._handles import Cursor, Subject


@dataclass(frozen=True)
class Options:
    """`StepLimit`'s options (`extensions.step_limit`; `/config step_limit`)."""

    limit: Annotated[int, "unattended steps before auto-step turns off"] = 50

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"a step limit must be at least 1, not {self.limit}")


class StepLimit:
    """Turns a cursor's auto-step off after `limit` unattended steps in a row (D1).

    The limit is the extension's option (`Options.limit`, 50 unless configured), read at
    each hook, so `/config step_limit` changes it for a branch.

    It guards the loops where the cursor does move every step, such as a model
    that calls tools forever or a hook that writes "continue" after every reply.
    A cursor that does not move is stopped by core, not by this limit.

    The count is this object's own memory, per cursor, kept at hook points:

    - `pre_input` sets it to 1: the step that takes an input is the first. There
      is no `post_input` hook; `pre_input` fires once per input item offered.
    - `pre_step` adds one, when the cursor's auto-step is on. A step run with
      auto-step off (a frontend's `step`, a load) is attended and is not counted.
    - `post_step` turns auto-step off when the count has reached `limit`, and
      starts the count over, so whoever resumes the cursor (`set_auto`) gets
      another `limit` steps. It also starts over whenever it sees auto-step off:
      a halt, an abort, or a go-to ended the run.

    When core turns auto-step off because a step did not move the cursor, that
    happens after `post_step`: the count stays as it was until the next input.

    A hook that leaves an input pending offers it again every step, so
    `pre_input` restarts the count every time: this limit does not bound that.
    List it after any extension that turns auto-step back on from `post_step`,
    or that hook's `set_auto(True)` wins.
    """

    name = "step_limit"
    options = Options

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}

    async def pre_input(self, cursor: Cursor, subject: Subject) -> str | None:
        """Start the cursor's count at 1: the step that takes an input is the first."""
        self._counts[cursor.id] = 1
        return None

    async def pre_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Count this step if auto-step is on. A bad `limit` option fails the step here."""
        cursor.options()  # a bad limit fails the step before it samples
        if cursor.cursor(cursor.id).auto:
            self._counts[cursor.id] = self._counts.get(cursor.id, 0) + 1
        return None

    async def post_step(self, cursor: Cursor, subject: Subject) -> str | None:
        """Turn auto-step off once the count reaches `limit`, then start the count over."""
        auto = cursor.cursor(cursor.id).auto
        if auto and self._counts.get(cursor.id, 0) >= cursor.options().limit:
            cursor.set_auto(False)
            auto = False
        if not auto:
            self._counts[cursor.id] = 0
        return None
