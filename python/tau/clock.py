"""The wall clock."""

import time


class SystemClock:
    def now(self) -> int:
        return time.time_ns() // 1_000_000
