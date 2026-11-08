"""Debounced dispatch: one endpoint-wide quiet period, restart on every command, bounded wait."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.commands import (
    CommandExecutor,
    CommandKind,
    CommandPermissions,
    CommandQueue,
    CommandStatus,
)
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

from .mock_wtc import REAL_PROFILE, MockWtcBackend, VirtualClock

UNITS = {uid: len(zones) for uid, zones in REAL_PROFILE.items()}
ALL = CommandPermissions(True, frozenset((u, z) for u, n in UNITS.items() for z in range(1, n + 1)))
CODE_REG = p.zone_wheel_code_register


class ManualClock:
    """Time moves only when the test says so; sleepers wake in deadline order."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            self._sleepers = [s for s in self._sleepers if not s[1].done()]  # drop cancelled
            due = sorted((s for s in self._sleepers if s[0] <= target), key=lambda s: s[0])
            if not due:
                break
            deadline, future = due[0]
            self._sleepers.remove(due[0])
            self.now = max(self.now, deadline)
            future.set_result(None)
            await _settle()
        self.now = target
        await _settle()


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, *, debounce: float = 2.0, max_wait: float = 10.0) -> None:
        self.clock = ManualClock()
        wire_clock = VirtualClock()
        self.backend = MockWtcBackend.for_profile(wire_clock)
        transport, self.client = build_client(
            "192.0.2.1", 502, UNITS, backend=self.backend, clock=wire_clock, writable=True
        )
        assert isinstance(transport, WritableGatewayTransport)
        self.queue = CommandQueue(self.clock, ttl=120.0)
        self.executor = CommandExecutor(
            transport, self.queue, ALL, debounce=debounce, max_wait=max_wait
        )
        self.generation = transport.connection_generation

    async def prime(self) -> None:
        await self.client.read_snapshot()  # connects, so commands carry a live generation
        self.generation = self.executor.connection_generation

    def submit(self, unit: int, zone: int, code: int) -> None:
        command = self.queue.create(
            CommandKind.WHEEL_TARGET, unit, zone, 22.0 + (code - 6) * 0.5, code, self.generation
        )
        self.queue.submit(command)
        self.executor.notify_submitted()

    @property
    def writes(self) -> list[tuple[str, int, int, object]]:
        return list(self.backend.writes)


async def test_nothing_is_written_before_the_quiet_period_ends() -> None:
    rig = Rig()
    await rig.prime()
    rig.submit(2, 2, 5)
    await rig.clock.advance(1.9)
    assert rig.writes == []
    await rig.clock.advance(0.2)
    assert rig.writes == [("write_register", 2, CODE_REG(2), 5)]
    await rig.executor.close()


async def test_every_new_command_restarts_the_two_second_window_for_all_zones() -> None:
    rig = Rig()
    await rig.prime()
    rig.submit(2, 3, 4)
    await rig.clock.advance(1.9)
    rig.submit(1, 1, 8)  # another thermostat: the single endpoint-wide window restarts
    await rig.clock.advance(1.9)
    assert rig.writes == []  # a per-command timer would have fired at 2.0 s
    rig.submit(1, 2, 9)
    await rig.clock.advance(1.9)
    assert rig.writes == []
    await rig.clock.advance(0.2)
    assert rig.writes == [  # one burst, fixed order (unit, zone)
        ("write_register", 1, CODE_REG(1), 8),
        ("write_register", 1, CODE_REG(2), 9),
        ("write_register", 2, CODE_REG(3), 4),
    ]
    await rig.executor.close()


async def test_latest_value_wins_inside_the_window() -> None:
    rig = Rig()
    await rig.prime()
    for code in (3, 4, 5, 6, 7):
        rig.submit(2, 2, code)
        await rig.clock.advance(0.5)
    await rig.clock.advance(2.0)
    assert rig.writes == [("write_register", 2, CODE_REG(2), 7)]
    await rig.executor.close()


async def test_a_steady_stream_cannot_starve_the_write_beyond_max_wait() -> None:
    rig = Rig()
    await rig.prime()
    start = rig.clock.now
    first_write_at: float | None = None
    code = 0
    while rig.clock.now - start < 14:
        rig.submit(2, 2, code % 13)
        code += 1
        await rig.clock.advance(1.5)  # always inside the 2 s window
        if rig.writes and first_write_at is None:
            first_write_at = rig.clock.now - start
    assert first_write_at is not None and first_write_at <= 10 + 1.5
    await rig.executor.close()


async def test_a_command_submitted_during_the_flush_gets_its_own_window() -> None:
    rig = Rig()
    await rig.prime()
    submitted: list[int] = []

    def on_change() -> None:
        if not submitted:
            submitted.append(1)
            rig.submit(2, 4, 3)

    rig.executor.on_change = on_change
    rig.submit(2, 2, 5)
    await rig.clock.advance(2.1)
    assert rig.writes == [("write_register", 2, CODE_REG(2), 5)]
    await rig.clock.advance(1.0)
    assert len(rig.writes) == 1  # still inside its own quiet period
    await rig.clock.advance(1.2)
    assert rig.writes[1:] == [("write_register", 2, CODE_REG(4), 3)]
    await rig.executor.close()


async def test_writes_in_a_burst_keep_the_one_request_per_second_limit() -> None:
    rig = Rig()
    await rig.prime()
    for zone in range(1, 5):
        rig.submit(2, zone, 5)
    await rig.clock.advance(2.1)
    starts = [r.start for r in rig.backend.records if r.op == "write_register"]
    assert len(starts) == 4
    assert all(b - a >= 1.0 - 1e-9 for a, b in itertools.pairwise(starts))
    await rig.executor.close()


async def test_close_drops_waiting_commands_without_any_write() -> None:
    rig = Rig()
    await rig.prime()
    rig.submit(2, 2, 5)
    await rig.clock.advance(1.0)
    await rig.executor.close()
    await rig.clock.advance(30)
    assert rig.writes == []  # no replay, nothing written after the unload
    record = rig.queue.latest(2, 2)
    assert record is not None and record.status is CommandStatus.TIMEOUT
    assert record.detail == "dropped on unload"
    assert rig.queue.optimistic(2, 2) is None


async def test_no_task_and_no_write_without_a_submitted_command() -> None:
    rig = Rig()
    await rig.prime()
    await rig.clock.advance(120)
    assert rig.writes == []
    assert rig.executor._worker is None


@pytest.mark.parametrize(("debounce", "max_wait"), [(0.0, 10.0), (-1.0, 10.0), (5.0, 2.0)])
def test_invalid_timing_is_refused(debounce: float, max_wait: float) -> None:
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    transport, _ = build_client(
        "192.0.2.1", 502, UNITS, backend=backend, clock=clock, writable=True
    )
    assert isinstance(transport, WritableGatewayTransport)
    with pytest.raises(ValueError):
        CommandExecutor(
            transport,
            CommandQueue(clock, 60.0),
            ALL,
            debounce=debounce,
            max_wait=max_wait,
        )
