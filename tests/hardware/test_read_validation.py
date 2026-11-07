"""Opt-in READ-ONLY hardware validation (plan step 4). Never runs in CI or by default.

Run only with explicit maintainer approval, one run at a time, write mode off:

    WTC_HARDWARE_HOST=192.168.1.210 WTC_HARDWARE_UNITS="1:6,2:4" \\
        python -m pytest tests/hardware -m hardware -s

Everything goes through the integration's own ``PymodbusBackend`` + ``GatewayTransport`` +
``WtcReadClient`` with conservative defaults (1 request/s, 15-register / 16-coil caps). The
backend has no write API at all. Cycles: one first cycle (static points) and ``WTC_CYCLES`` steady
cycles spaced by the derived minimum poll interval (default 2). The report is written to
``WTC_REPORT_DIR`` (default ``tests/hardware/reports``) as JSON and contains raw values only: no
credentials. Interpretation of the report is a human step (docs/vendor-bms-v1_9.md, decisions 3, 5).
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

from custom_components.wavin_wtc_net.models import GatewaySnapshot
from custom_components.wavin_wtc_net.runtime import build_client
from custom_components.wavin_wtc_net.unit_spec import parse_units

HOST = os.environ.get("WTC_HARDWARE_HOST")

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(not HOST, reason="set WTC_HARDWARE_HOST to run the hardware validation"),
    pytest.mark.usefixtures("socket_enabled"),
]


def _unit_report(snapshot: GatewaySnapshot) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for unit in snapshot.units:
        entry: dict[str, Any] = {
            "unit_id": unit.unit_id,
            "available": unit.available,
            "error": unit.error,
            "failed_optional_reads": list(unit.failed_optional_reads),
            "excluded_points": list(unit.excluded_points),
        }
        if unit.available and unit.controller and unit.raw:
            c = unit.controller
            entry["controller"] = {
                "mode": c.mode.name if c.mode else None,
                "mode_raw": c.mode_raw,
                "water_temperature": c.water_temperature,
                "firmware": c.firmware,
                "serial": c.serial,
                "global_on_off": c.global_on_off,
                "global_comfort_economy": c.global_comfort_economy,
                "global_heat_cool": c.global_heat_cool,
                "emergency": c.emergency_water_temperature,
                "sensor_fault": c.water_sensor_fault,
            }
            zones = []
            for z in unit.zones:
                zones.append(
                    {
                        "zone": z.zone,
                        "temperature": z.temperature,
                        "live": z.live,
                        "valve_open": z.valve_open,
                        # decision 5: status offsets 4/5 vs writable coils 0/1 must agree
                        "on_off_status": z.on_off,
                        "on_off_control": z.control_on_off,
                        "comfort_economy_status": z.comfort_economy,
                        "comfort_economy_control": z.control_comfort_economy,
                        "heat_cool_status": z.heat_cool,
                        "heat_cool_control": z.control_heat_cool,
                        "lock_status": z.lock_status,
                        # wheel evidence: B+3 offset, writable code and packed word
                        "wheel_offset_c": z.wheel_offset,
                        "wheel_code": z.wheel_code,
                        "packed_wheel_word": z.packed_wheel_word,
                        "setpoints": {
                            "comfort_cool": z.comfort_cool_setpoint,
                            "comfort_heat": z.comfort_heat_setpoint,
                            "economy_heat": z.economy_heat_setpoint,
                            "economy_cool": z.economy_cool_setpoint,
                        },
                    }
                )
            entry["zones"] = zones
            entry["raw_registers"] = dict(unit.raw.registers)
            entry["raw_coils"] = dict(unit.raw.coils)
        out.append(entry)
    return out


def _write_report(report: dict[str, Any]) -> Path:
    out_dir = Path(os.environ.get("WTC_REPORT_DIR", Path(__file__).parent / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"read-validation-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def test_read_only_validation_report() -> None:
    assert HOST is not None
    pytest_socket.socket_allow_hosts([HOST], allow_unix_socket=True)  # only the approved endpoint
    units = parse_units(os.environ.get("WTC_HARDWARE_UNITS", "1:6,2:4"))
    port = int(os.environ.get("WTC_HARDWARE_PORT", "502"))
    cycles = int(os.environ.get("WTC_CYCLES", "2"))
    transport, client = build_client(HOST, port, units)  # default conservative settings
    started = time.time()
    snapshots: list[dict[str, Any]] = []
    try:
        for index in range(cycles + 1):  # first cycle includes static points
            if index:
                await asyncio.sleep(client.minimum_poll_interval)
            cycle_start = time.time()
            snapshot = await client.read_snapshot()
            snapshots.append(
                {
                    "cycle": index,
                    "duration_s": round(time.time() - cycle_start, 1),
                    "requests_so_far": transport.stats.requests_started,
                    "units": _unit_report(snapshot),
                }
            )
    finally:
        await transport.close()

    stats = transport.stats
    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host_redacted": True,
        "units": {str(u): z for u, z in units.items()},
        "first_cycle_requests": client.first_cycle_requests,
        "steady_cycle_requests": client.requests_per_cycle,
        "duration_s": round(time.time() - started, 1),
        "transport": {
            "requests_started": stats.requests_started,
            "failures": stats.failures,
            "last_latency_s": stats.last_latency,
        },
        "hole_failures": client.hole_failures,
        "register_polarity_note": "see docs/open-decisions.md decision 5",
        "snapshots": snapshots,
    }
    path = await asyncio.to_thread(_write_report, report)
    print(f"\nreport: {path}")

    # Read-only by construction; this only guards the contract (rate, caps are in the transport).
    assert any(u["available"] for u in snapshots[0]["units"]), "no unit was reachable"
    assert stats.requests_started <= client.first_cycle_requests + cycles * (
        client.requests_per_cycle * 3
    )
    assert not hasattr(transport, "write_coil") and not hasattr(transport, "write_register")
