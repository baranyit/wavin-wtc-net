"""24-hour virtual-time soak tests for the read-only milestone gate.

Gate (docs/06-implementation-plan.md step 2): 24-hour polling/reconnect/malformed
response suite with zero write calls, no overlapping cycles and no rate/cap violation.
"""

from __future__ import annotations

import asyncio
import itertools
import random

from custom_components.wavin_wtc_net.client import UnitConfig, WtcReadClient
from custom_components.wavin_wtc_net.models import GatewaySnapshot
from custom_components.wavin_wtc_net.poller import GatewayPoller
from custom_components.wavin_wtc_net.protocol import Table
from custom_components.wavin_wtc_net.transport import GatewayTransport, TransportSettings

from .mock_wtc import REAL_PROFILE, Fault, MockWtcBackend, VirtualClock

DAY = 24 * 3600


def build(
    interval: float, settings: TransportSettings | None = None
) -> tuple[VirtualClock, MockWtcBackend, WtcReadClient, GatewayPoller, list[GatewaySnapshot]]:
    clock = VirtualClock()
    backend = MockWtcBackend.for_profile(clock)
    transport = GatewayTransport(backend, settings or TransportSettings(), clock)
    units = [UnitConfig(uid, tuple(zones)) for uid, zones in REAL_PROFILE.items()]
    client = WtcReadClient(transport, units, clock=clock)
    snapshots: list[GatewaySnapshot] = []
    poller = GatewayPoller(client, interval=interval, clock=clock, on_snapshot=snapshots.append)
    return clock, backend, client, poller, snapshots


def assert_wire_invariants(backend: MockWtcBackend) -> None:
    assert backend.writes == []
    assert {r.op for r in backend.records} <= {"read_holding_registers", "read_coils"}
    assert backend.max_in_flight == 1
    starts = [r.start for r in backend.records]
    assert all(b - a >= 1.0 - 1e-9 for a, b in itertools.pairwise(starts))
    for record in backend.records:
        cap = 15 if record.op == "read_holding_registers" else 16
        assert 1 <= record.count <= cap


async def test_24h_clean_polling_makes_zero_writes() -> None:
    clock, backend, client, poller, snapshots = build(60.0)
    start = clock.now
    await poller.run(max_cycles=DAY // 60)
    assert clock.now - start <= DAY
    assert poller.completed_cycles == 1440 and poller.skipped_cycles == 0
    assert len(backend.records) == 1440 * client.requests_per_cycle
    assert all(unit.available for snap in snapshots for unit in snap.units)
    assert backend.connect_attempts == 1
    assert_wire_invariants(backend)


async def test_24h_with_outages_and_faults_recovers_without_stale_data() -> None:
    clock, backend, _client, poller, snapshots = build(60.0)
    rng = random.Random(42)
    start = clock.now
    # a 5-minute gateway outage every 2 hours
    backend.outages = [(start + h * 7200 + 1800, start + h * 7200 + 2100) for h in range(12)]
    # sporadic malformed/exception/disconnect/timeout faults on random units/addresses
    for _ in range(60):
        kind = rng.choice(["short", "garbage", "exception", "disconnect"])
        backend.faults.append(
            Fault(kind, unit_id=rng.choice([1, 2]), table=rng.choice(list(Table)), remaining=1)
        )
    poller_task = asyncio.create_task(poller.run(max_cycles=DAY // 60))
    await poller_task

    assert_wire_invariants(backend)
    assert poller.completed_cycles == 1440
    for snap in snapshots:
        for unit in snap.units:
            if not unit.available:
                assert unit.zones == () and unit.controller is None and unit.raw is None
    # every outage window is followed by a fully available snapshot within 10 minutes
    for _, end in backend.outages:
        assert any(
            end <= snap.cycle_started <= end + 600 and all(u.available for u in snap.units)
            for snap in snapshots
        )
    # snapshots that started inside an outage never report live data
    for snap in snapshots:
        if any(s <= snap.cycle_started < e for s, e in backend.outages):
            assert not any(u.available for u in snap.units)
    assert sum(all(u.available for u in s.units) for s in snapshots) > 1300


async def test_24h_timeouts_are_bounded() -> None:
    _clock, backend, _client, poller, snapshots = build(
        60.0, TransportSettings(request_timeout=0.02)
    )
    for _ in range(5):
        backend.faults.append(Fault("timeout", remaining=1))
    await poller.run(max_cycles=DAY // 60)
    assert_wire_invariants(backend)
    assert sum(r.outcome == "timeout" for r in backend.records) == 5
    assert snapshots[-1].units[0].available


async def test_long_cycles_never_overlap_and_ticks_are_skipped() -> None:
    _clock, backend, _client, poller, _ = build(10.0)  # cycle takes ~28 s
    await poller.run(max_cycles=50)
    assert poller.completed_cycles == 50
    assert poller.skipped_cycles > 0
    assert_wire_invariants(backend)


async def test_concurrent_manual_cycle_is_skipped() -> None:
    _clock, backend, client, poller, _ = build(60.0)
    results = await asyncio.gather(poller.run_cycle(), poller.run_cycle())
    assert sum(r is None for r in results) == 1
    assert poller.skipped_cycles == 1
    assert len(backend.records) == client.requests_per_cycle
    assert_wire_invariants(backend)
