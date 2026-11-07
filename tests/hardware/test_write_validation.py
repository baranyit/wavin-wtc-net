"""Opt-in HARDWARE WRITE test (plan step 5 gate). Never runs in CI or by default.

Needs a separate, explicit maintainer approval for ONE zone and ONE raw wheel step (AGENTS.md
section 10, docs/07-testing-strategy.md). Run with the exact approved change, nothing else:

    WTC_HARDWARE_HOST=192.168.1.210 WTC_HARDWARE_UNITS="1:6,2:4" \\
    WTC_WRITE_TEST="2:2:10:9" \\
        python -m pytest tests/hardware/test_write_validation.py -m hardware_write -s

``WTC_WRITE_TEST`` is ``unit:zone:from_code:to_code``. The whole path is the integration's own:
writable transport, command queue, poller. Sequence:

1. read-only cycle; abort without writing unless the zone is live, on, heating, comfort and its
   wheel code equals ``from_code``;
2. queue the target for ``to_code``; next cycle writes it before the reads and verifies;
3. one more observation cycle;
4. queue the restore to ``from_code``; next cycle writes it and verifies; one more observation.

Exactly two writes at most. If the restore cannot be confirmed the report says so loudly. The
physical dial is observed by the human, who is told the timestamps in the report.
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
    validate_wheel_target,
)
from custom_components.wavin_wtc_net.models import GatewaySnapshot
from custom_components.wavin_wtc_net.poller import GatewayPoller
from custom_components.wavin_wtc_net.protocol import WheelCalibration
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.transport import AsyncioClock
from custom_components.wavin_wtc_net.unit_spec import parse_units
from custom_components.wavin_wtc_net.write_transport import WritableGatewayTransport

HOST = os.environ.get("WTC_HARDWARE_HOST")
APPROVAL = os.environ.get("WTC_WRITE_TEST")

pytestmark = [
    pytest.mark.hardware_write,
    pytest.mark.skipif(
        not (HOST and APPROVAL), reason="needs WTC_HARDWARE_HOST and the approved WTC_WRITE_TEST"
    ),
    pytest.mark.usefixtures("socket_enabled"),
]

CAL = WheelCalibration()  # the installation's calibration: 22 / 26, 0.5 / 0.5


def _zone_view(snapshot: GatewaySnapshot, unit: int, zone: int) -> dict[str, Any]:
    state = snapshot.unit(unit).zone(zone)  # type: ignore[union-attr]
    assert state is not None
    return {
        "wheel_code": state.wheel_code,
        "wheel_offset_c": state.wheel_offset,
        "packed_wheel_word": state.packed_wheel_word,
        "temperature": state.temperature,
        "on": state.on_off,
        "comfort": state.comfort_economy,
        "cooling": state.heat_cool,
        "live": state.live,
        "comfort_heat_setpoint": state.comfort_heat_setpoint,
    }


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = Path(os.environ.get("WTC_REPORT_DIR", Path(__file__).parent / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"write-validation-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def test_one_zone_one_step_and_restore() -> None:
    assert HOST is not None and APPROVAL is not None
    pytest_socket.socket_allow_hosts([HOST], allow_unix_socket=True)
    unit, zone, from_code, to_code = (int(x) for x in APPROVAL.split(":"))
    assert abs(to_code - from_code) == 1, "exactly one raw wheel step is approved"
    units = parse_units(os.environ.get("WTC_HARDWARE_UNITS", "1:6,2:4"))
    port = int(os.environ.get("WTC_HARDWARE_PORT", "502"))

    transport, client = build_client(HOST, port, units, writable=True)
    assert isinstance(transport, WritableGatewayTransport)
    interval = client.minimum_poll_interval
    clock = AsyncioClock()
    queue = CommandQueue(clock, ttl=interval * 3)
    permissions = CommandPermissions(True, frozenset({(unit, zone)}))  # exactly this zone
    executor = CommandExecutor(transport, queue, permissions)
    poller = GatewayPoller(
        client, interval=interval, before_reads=executor.before_reads, on_snapshot=executor.verify
    )

    log: list[dict[str, Any]] = []
    t0 = time.time()

    async def cycle(label: str) -> GatewaySnapshot:
        snapshot = await poller.run_cycle()
        assert snapshot is not None
        log.append(
            {
                "label": label,
                "at": time.strftime("%H:%M:%S"),
                "elapsed_s": round(time.time() - t0, 1),
                "writes_so_far": len([r for r in queue.history if r.status is CommandStatus.SENT]),
                "requests_started": transport.stats.requests_started,
                "zone": _zone_view(snapshot, unit, zone),
                "commands": records_summary(queue.history),
            }
        )
        return snapshot

    def queue_target(snapshot: GatewaySnapshot, code: int) -> None:
        target = CAL.comfort_target(code, cooling=False)
        raw = validate_wheel_target(
            unit_id=unit,
            zone=zone,
            target=target,
            snapshot=snapshot,
            calibration=CAL,
            permissions=permissions,
        )
        assert raw == code
        queue.submit(
            queue.create(
                CommandKind.WHEEL_TARGET, unit, zone, target, raw, transport.connection_generation
            )
        )

    restored = False
    stepped = False
    outcome = "not started"
    try:
        snapshot = await cycle("0 read-only precondition")
        view = _zone_view(snapshot, unit, zone)
        # Preconditions: abort without any write unless the zone is exactly as approved.
        assert view["wheel_code"] == from_code, f"wheel code is {view['wheel_code']}, not approved"
        assert view["live"] is True and view["on"] is True
        assert view["cooling"] is False and view["comfort"] is True

        queue_target(snapshot, to_code)
        stepped = True
        await asyncio.sleep(interval)
        await cycle("1 write step + read-back")
        await asyncio.sleep(interval)
        await cycle("2 observe")
        dial_cycles = int(os.environ.get("WTC_DIAL_TURN_CYCLES", "0"))
        if dial_cycles:
            # No writes in this window: the human turns the physical dial, we only read.
            print("\nDIAL_NOW: turn the thermostat dial one step now", flush=True)
            for n in range(dial_cycles):
                await asyncio.sleep(interval)
                await cycle(f"2.{n + 1} observe dial turn")
            print("\nDIAL_WINDOW_OVER", flush=True)
        outcome = "step written and observed"
    finally:
        try:
            if stepped:
                # Restoration always runs once a step was queued, through the same command path.
                await asyncio.sleep(interval)
                snapshot = await cycle("3 pre-restore read")
                current = _zone_view(snapshot, unit, zone)["wheel_code"]
                if current != from_code:
                    queue_target(snapshot, from_code)
                    await asyncio.sleep(interval)
                    await cycle("4 restore write + read-back")
                    await asyncio.sleep(interval)
                    snapshot = await cycle("5 observe after restore")
                    current = _zone_view(snapshot, unit, zone)["wheel_code"]
                restored = current == from_code
        finally:
            await transport.close()

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "approved_change": {"unit": unit, "zone": zone, "from_code": from_code, "to_code": to_code},
        "outcome": outcome,
        "restored": restored,
        "poll_interval_s": interval,
        "writes": len([r for r in queue.history if r.status is CommandStatus.SENT]),
        "transport": {
            "requests_started": transport.stats.requests_started,
            "failures": transport.stats.failures,
        },
        "log": log,
    }
    path = await asyncio.to_thread(_write_report, report)
    print(f"\nreport: {path}\nrestored: {restored}")
    assert restored, "RESTORE NOT CONFIRMED: check the thermostat and restore it by hand"
