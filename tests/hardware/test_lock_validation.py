"""Opt-in HARDWARE LOCK test (decision 13). Never runs in CI or by default.

Needs a separate maintainer approval for ONE zone (AGENTS.md section 10). Run with that zone only:

    WTC_HARDWARE_HOST=192.168.1.210 WTC_HARDWARE_UNITS="1:6,2:4" WTC_LOCK_TEST="2:2" \\
        python -m pytest tests/hardware/test_lock_validation.py -m hardware_write -s

Sequence, all through the integration's own writable transport, queue and poller:

1. read-only precondition cycle; abort without writing unless the zone is live and unlocked;
2. queue "lock", then observe up to ``WTC_LOCK_CYCLES`` cycles (default 4) until the status coil
   (candidate 4186+) shows locked: this measures the status lag in poll-interval resolution;
3. queue "unlock", observe until the status shows unlocked;
4. always end unlocked: if still locked, one more unlock is queued.

Optional ``WTC_LOCK_HOLD_CYCLES=N`` keeps the zone locked for N more read-only cycles (prints
``LOCKED_NOW``) so the maintainer can observe the thermostat.
Cycles run on the poll grid (start to start = the poll interval, not interval after finish).
At most 3 writes (lock, unlock, one emergency unlock). The human watches the thermostat.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_socket

from custom_components.wavin_wtc_net.commands import (
    CommandExecutor,
    CommandKind,
    CommandPermissions,
    CommandQueue,
    CommandStatus,
    records_summary,
    validate_lock_target,
)
from custom_components.wavin_wtc_net.models import GatewaySnapshot
from custom_components.wavin_wtc_net.poller import GatewayPoller
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.transport import AsyncioClock
from custom_components.wavin_wtc_net.unit_spec import parse_units
from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

HOST = os.environ.get("WTC_HARDWARE_HOST")
APPROVAL = os.environ.get("WTC_LOCK_TEST")

pytestmark = [
    pytest.mark.hardware_write,
    pytest.mark.skipif(
        not (HOST and APPROVAL), reason="needs WTC_HARDWARE_HOST and the approved WTC_LOCK_TEST"
    ),
    pytest.mark.usefixtures("socket_enabled"),
]


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = Path(os.environ.get("WTC_REPORT_DIR", Path(__file__).parent / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"lock-validation-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def test_lock_one_zone_and_unlock() -> None:
    assert HOST is not None and APPROVAL is not None
    pytest_socket.socket_allow_hosts([HOST], allow_unix_socket=True)
    unit, zone = (int(x) for x in APPROVAL.split(":"))
    units = parse_units(os.environ.get("WTC_HARDWARE_UNITS", "1:6,2:4"))
    port = int(os.environ.get("WTC_HARDWARE_PORT", "502"))
    max_cycles = int(os.environ.get("WTC_LOCK_CYCLES", "4"))

    transport, client = build_client(HOST, port, units, writable=True)
    assert isinstance(transport, WritableGatewayTransport)
    interval = client.minimum_poll_interval
    queue = CommandQueue(AsyncioClock(), ttl=interval * 3)
    permissions = CommandPermissions(True, frozenset(), frozenset({(unit, zone)}))
    executor = CommandExecutor(transport, queue, permissions)
    poller = GatewayPoller(client, interval=interval, on_snapshot=executor.verify)
    executor.cycle_index = lambda: poller.cycles_started
    log: list[dict[str, Any]] = []
    t0 = time.time()
    next_at = t0

    async def tick() -> None:
        """Sleep until the next poll-grid tick (start to start = the poll interval)."""
        nonlocal next_at
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - time.time()))

    def status_of(snapshot: GatewaySnapshot) -> bool | None:
        return snapshot.unit(unit).zone(zone).lock_status  # type: ignore[union-attr]

    async def cycle(label: str) -> GatewaySnapshot:
        snapshot = await poller.run_cycle()
        assert snapshot is not None
        log.append(
            {
                "label": label,
                "at": time.strftime("%H:%M:%S"),
                "elapsed_s": round(time.time() - t0, 1),
                "lock_status": status_of(snapshot),
                "requests_started": transport.stats.requests_started,
                "commands": records_summary(queue.history)[-2:],
            }
        )
        return snapshot

    async def queue_lock(snapshot: GatewaySnapshot, locked: bool) -> None:
        print(
            f"\nQUEUED_{'LOCK' if locked else 'UNLOCK'} at {time.strftime('%H:%M:%S')}", flush=True
        )
        validate_lock_target(unit_id=unit, zone=zone, snapshot=snapshot, permissions=permissions)
        queue.submit(
            queue.create(
                CommandKind.LOCK, unit, zone, locked, int(locked), transport.connection_generation
            )
        )
        await executor.flush()  # written now (after the debounce in production)

    async def wait_for(want: bool, label: str) -> tuple[GatewaySnapshot, int | None]:
        snapshot = await cycle(f"{label} write + read-back")
        for n in range(1, max_cycles + 1):
            if status_of(snapshot) is want:
                return snapshot, n - 1
            await tick()
            snapshot = await cycle(f"{label} observe {n}")
        return snapshot, None

    locked_after_cycles: int | None = None
    unlocked_after_cycles: int | None = None
    final_unlocked = False
    started = False
    try:
        snapshot = await cycle("0 read-only precondition")
        assert status_of(snapshot) is False, "zone is not unlocked, not approved"
        assert snapshot.unit(unit).zone(zone).live is True  # type: ignore[union-attr]
        await queue_lock(snapshot, True)
        started = True
        await tick()
        snapshot, locked_after_cycles = await wait_for(True, "1 lock")
        hold = int(os.environ.get("WTC_LOCK_HOLD_CYCLES", "0"))
        if hold:
            # Stay locked (no writes) so the human can look at the thermostat.
            print(f"\nLOCKED_NOW {time.strftime('%H:%M:%S')}: look at the thermostat", flush=True)
            for n in range(hold):
                await tick()
                snapshot = await cycle(f"1.{n + 1} hold locked")
        await queue_lock(snapshot, False)
        await tick()
        snapshot, unlocked_after_cycles = await wait_for(False, "2 unlock")
        final_unlocked = status_of(snapshot) is False
    finally:
        try:
            if started and not final_unlocked:
                await tick()
                snapshot = await cycle("3 pre-restore read")
                if status_of(snapshot) is not False:
                    await queue_lock(snapshot, False)
                    await tick()
                    snapshot, _ = await wait_for(False, "4 emergency unlock")
                final_unlocked = status_of(snapshot) is False
        finally:
            await transport.close()

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "approved": {"unit": unit, "zone": zone},
        "poll_interval_s": interval,
        "locked_visible_after_extra_cycles": locked_after_cycles,
        "unlocked_visible_after_extra_cycles": unlocked_after_cycles,
        "ended_unlocked": final_unlocked,
        "writes": len([r for r in queue.history if r.status is CommandStatus.SENT]),
        "transport": {
            "requests_started": transport.stats.requests_started,
            "failures": transport.stats.failures,
        },
        "log": log,
    }
    path = await asyncio.to_thread(_write_report, report)
    print(f"\nreport: {path}\nended unlocked: {final_unlocked}")
    assert final_unlocked, "ZONE LEFT LOCKED: unlock it by hand on the thermostat"
