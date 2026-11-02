"""The wall clock."""

import time


class SystemClock:
    """The `Clock` that reads the wall clock. Pass it to `Driver`, which reads time only through
    a clock; a test passes its own."""

    def now(self) -> int:
        """The wall-clock time in milliseconds since the epoch."""
        return time.time_ns() // 1_000_000
