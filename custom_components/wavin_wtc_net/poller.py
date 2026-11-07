"""Non-overlapping poll cycle driver for one endpoint.

The Home Assistant coordinator drives cycles through this class. A cycle is: an optional
``before_reads`` hook (the command layer sends what the user explicitly queued) -> serialized
reads -> snapshot -> ``on_snapshot`` (read-back verification). Without hooks a cycle performs
reads only. Cycles never overlap: a tick that arrives while a cycle is running is skipped and
counted.
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

from .client import WtcReadClient
from .models import GatewaySnapshot
from .transport import AsyncioClock, Clock


class GatewayPoller:
    def __init__(
        self,
        client: WtcReadClient,
        *,
        interval: float,
        clock: Clock | None = None,
        on_snapshot: Callable[[GatewaySnapshot], None] | None = None,
        before_reads: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not (math.isfinite(interval) and interval > 0):
            raise ValueError("interval must be positive")
        self._client = client
        self._interval = interval
        self._clock: Clock = clock or AsyncioClock()
        self._on_snapshot = on_snapshot
        self._before_reads = before_reads
        self._cycle_lock = asyncio.Lock()
        self.completed_cycles = 0
        self.skipped_cycles = 0
        self.last_snapshot: GatewaySnapshot | None = None

    @property
    def cycle_running(self) -> bool:
        return self._cycle_lock.locked()

    async def run_cycle(self) -> GatewaySnapshot | None:
        """Run one cycle, or return ``None`` if another cycle is already running."""
        if self._cycle_lock.locked():
            self.skipped_cycles += 1
            return None
        async with self._cycle_lock:
            if self._before_reads is not None:
                await self._before_reads()  # explicit queued user commands only, then the reads
            snapshot = await self._client.read_snapshot()
            self.last_snapshot = snapshot
            self.completed_cycles += 1
        if self._on_snapshot is not None:
            self._on_snapshot(snapshot)
        return snapshot

    async def run(
        self, *, max_cycles: int | None = None, stop: asyncio.Event | None = None
    ) -> None:
        """Poll on a fixed grid; ticks missed by a long cycle are skipped, not queued."""
        next_tick = self._clock.monotonic()
        cycles = 0
        while (max_cycles is None or cycles < max_cycles) and not (stop and stop.is_set()):
            await self.run_cycle()
            cycles += 1
            next_tick += self._interval
            now = self._clock.monotonic()
            if now > next_tick:
                missed = math.floor((now - next_tick) / self._interval) + 1
                self.skipped_cycles += missed
                next_tick += missed * self._interval
            if max_cycles is not None and cycles >= max_cycles:
                break
            await self._clock.sleep(next_tick - now)
