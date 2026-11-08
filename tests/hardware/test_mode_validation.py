"""Opt-in HARDWARE zone mode test (decision 16). Never runs in CI or by default.

Needs a separate maintainer approval for ONE zone and ONE kind (AGENTS.md section 10):

    WTC_HARDWARE_HOST=192.168.1.210 WTC_HARDWARE_UNITS="1:6,2:4" WTC_MODE_TEST="2:2:preset" \\
        python -m pytest tests/hardware/test_mode_validation.py -m hardware_write -s

``WTC_MODE_TEST`` is ``unit:zone:kind`` with kind ``power`` (zone on/off) or ``preset``
(comfort/economy). Sequence through the integration's own writable transport, queue and poller:

1. read-only precondition cycle; abort without writing unless the zone is live, status and
   control bits agree, and it is on / in comfort (the state the test starts from and restores);
2. queue the change (zone off, or economy), observe up to ``WTC_MODE_CYCLES`` cycles (default 4)
   until the status coil shows it;
3. queue the restore (on / comfort), observe until the status shows it;
4. if the restore is not confirmed, one more restore is queued.

At most 3 writes. Cycles run on the poll grid. The human watches the thermostat.
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
    validate_zone_mode,
)
from custom_components.wavin_wtc_net.models import GatewaySnapshot, ZoneSnapshot
from custom_components.wavin_wtc_net.poller import GatewayPoller
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.transport import AsyncioClock
from custom_components.wavin_wtc_net.unit_spec import parse_units
from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

HOST = os.environ.get("WTC_HARDWARE_HOST")
APPROVAL = os.environ.get("WTC_MODE_TEST")

pytestmark = [
    pytest.mark.hardware_write,
    pytest.mark.skipif(
        not (HOST and APPROVAL), reason="needs WTC_HARDWARE_HOST and the approved WTC_MODE_TEST"
    ),
    pytest.mark.usefixtures("socket_enabled"),
]


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = Path(os.environ.get("WTC_REPORT_DIR", Path(__file__).parent / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"mode-validation-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def test_one_zone_mode_change_and_restore() -> None:
    assert HOST is not None and APPROVAL is not None
    pytest_socket.socket_allow_hosts([HOST], allow_unix_socket=True)
    unit_text, zone_text, kind_text = APPROVAL.split(":")
    unit, zone = int(unit_text), int(zone_text)
    kind = {"power": CommandKind.ZONE_POWER, "preset": CommandKind.ZONE_PRESET}[kind_text]
    units = parse_units(os.environ.get("WTC_HARDWARE_UNITS", "1:6,2:4"))
    port = int(os.environ.get("WTC_HARDWARE_PORT", "502"))
    max_cycles = int(os.environ.get("WTC_MODE_CYCLES", "4"))

    transport, client = build_client(HOST, port, units, writable=True)
    assert isinstance(transport, WritableGatewayTransport)
    interval = client.minimum_poll_interval
    queue = CommandQueue(AsyncioClock(), ttl=interval * 3)
    permissions = CommandPermissions(True, frozenset(), frozenset(), frozenset({(unit, zone)}))
    executor = CommandExecutor(transport, queue, permissions)
    poller = GatewayPoller(client, interval=interval, on_snapshot=executor.verify)
    executor.cycle_index = lambda: poller.cycles_started
    log: list[dict[str, Any]] = []
    t0 = time.time()
    next_at = t0

    async def tick() -> None:
        nonlocal next_at
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - time.time()))

    def zone_of(snapshot: GatewaySnapshot) -> ZoneSnapshot:
        state = snapshot.unit(unit).zone(zone)  # type: ignore[union-attr]
        assert state is not None
        return state

    def value_of(snapshot: GatewaySnapshot) -> bool | None:
        state = zone_of(snapshot)
        return state.on_off if kind is CommandKind.ZONE_POWER else state.comfort_economy

    async def cycle(label: str) -> GatewaySnapshot:
        snapshot = await poller.run_cycle()
        assert snapshot is not None
        state = zone_of(snapshot)
        log.append(
            {
                "label": label,
                "at": time.strftime("%H:%M:%S"),
                "elapsed_s": round(time.time() - t0, 1),
                "value": value_of(snapshot),
                "on_off": [state.on_off, state.control_on_off],
                "comfort_economy": [state.comfort_economy, state.control_comfort_economy],
                "heat_cool": [state.heat_cool, state.control_heat_cool],
                "wheel_code": state.wheel_code,
                "valve_open": state.valve_open,
                "requests_started": transport.stats.requests_started,
                "commands": records_summary(queue.history)[-2:],
            }
        )
        return snapshot

    async def queue_value(snapshot: GatewaySnapshot, value: bool) -> None:
        print(f"\nQUEUED_{kind_text.upper()}_{value} at {time.strftime('%H:%M:%S')}", flush=True)
        validate_zone_mode(
            unit_id=unit, zone=zone, kind=kind, snapshot=snapshot, permissions=permissions
        )
        queue.submit(
            queue.create(kind, unit, zone, value, int(value), transport.connection_generation)
        )
        await executor.flush()  # written now (after the debounce in production)

    async def wait_for(want: bool, label: str) -> tuple[GatewaySnapshot, int | None]:
        snapshot = await cycle(f"{label} write + read-back")
        for n in range(1, max_cycles + 1):
            if value_of(snapshot) is want:
                return snapshot, n - 1
            await tick()
            snapshot = await cycle(f"{label} observe {n}")
        return snapshot, None

    changed_after: int | None = None
    restored_after: int | None = None
    restored = False
    started = False
    try:
        snapshot = await cycle("0 read-only precondition")
        state = zone_of(snapshot)
        assert state.live is True and state.on_off is True and state.comfort_economy is True
        assert (state.on_off, state.comfort_economy) == (
            state.control_on_off,
            state.control_comfort_economy,
        ), "status and control bits disagree"
        await queue_value(snapshot, False)
        started = True
        await tick()
        snapshot, changed_after = await wait_for(False, "1 change")
        await queue_value(snapshot, True)
        await tick()
        snapshot, restored_after = await wait_for(True, "2 restore")
        restored = value_of(snapshot) is True
    finally:
        try:
            if started and not restored:
                await tick()
                snapshot = await cycle("3 pre-restore read")
                if value_of(snapshot) is not True:
                    await queue_value(snapshot, True)
                    await tick()
                    snapshot, _ = await wait_for(True, "4 emergency restore")
                restored = value_of(snapshot) is True
        finally:
            await transport.close()

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "approved": {"unit": unit, "zone": zone, "kind": kind_text},
        "poll_interval_s": interval,
        "change_visible_after_extra_cycles": changed_after,
        "restore_visible_after_extra_cycles": restored_after,
        "restored": restored,
        "writes": len([r for r in queue.history if r.status is CommandStatus.SENT]),
        "transport": {
            "requests_started": transport.stats.requests_started,
            "failures": transport.stats.failures,
        },
        "log": log,
    }
    path = await asyncio.to_thread(_write_report, report)
    print(f"\nreport: {path}\nrestored: {restored}")
    assert restored, "ZONE NOT RESTORED: check the thermostat and set it back by hand"
