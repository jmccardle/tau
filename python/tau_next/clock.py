"""The wall clock."""

import asyncio
import time


class SystemClock:
    def now(self) -> int:
        return time.time_ns() // 1_000_000

    async def sleep_until(self, t: int) -> None:
        await asyncio.sleep(max(0, t - self.now()) / 1000)
