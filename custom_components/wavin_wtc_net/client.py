"""Read-only snapshot client for all controllers behind one gateway endpoint.

The read path has no route to any write operation. Explicit user commands will be
added in a later milestone as a separate API (``commands.py``).
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import WtcMalformedResponseError, WtcModbusExceptionError, WtcTransportError
from .models import GatewaySnapshot, RawImage, UnitSnapshot
from .protocol import (
    MAX_ZONES,
    ReadRequest,
    Table,
    decode_controller,
    decode_zone,
    plan_reads,
    unit_points,
    validate_unit_id,
    validate_zone,
)
from .transport import AsyncioClock, Clock, GatewayTransport

DEFAULT_MAX_GAP = 8


@dataclass(frozen=True, slots=True)
class UnitConfig:
    unit_id: int
    zones: tuple[int, ...]

    def __post_init__(self) -> None:
        validate_unit_id(self.unit_id)
        if not self.zones:
            raise ValueError("a unit needs at least one zone")
        for zone in self.zones:
            validate_zone(zone)
        if len(set(self.zones)) != len(self.zones):
            raise ValueError("duplicate zone numbers")
        if len(self.zones) > MAX_ZONES:
            raise ValueError("too many zones")


class WtcReadClient:
    def __init__(
        self,
        transport: GatewayTransport,
        units: Sequence[UnitConfig],
        *,
        max_gap: int = DEFAULT_MAX_GAP,
        clock: Clock | None = None,
    ) -> None:
        if not units:
            raise ValueError("at least one unit is required")
        ids = [unit.unit_id for unit in units]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate unit ids")
        self._transport = transport
        self._units = tuple(units)
        self._clock: Clock = clock or AsyncioClock()
        settings = transport.settings
        self._plans: dict[int, tuple[ReadRequest, ...]] = {
            unit.unit_id: plan_reads(
                unit_points(unit.zones),
                max_registers=settings.max_registers_per_request,
                max_coils=settings.max_coils_per_request,
                max_gap=max_gap,
            )
            for unit in self._units
        }

    @property
    def units(self) -> tuple[UnitConfig, ...]:
        return self._units

    def plan(self, unit_id: int) -> tuple[ReadRequest, ...]:
        return self._plans[unit_id]

    @property
    def requests_per_cycle(self) -> int:
        return sum(len(plan) for plan in self._plans.values())

    async def read_snapshot(self) -> GatewaySnapshot:
        started = self._clock.monotonic()
        units = []
        for unit in self._units:
            units.append(await self.read_unit(unit))
        return GatewaySnapshot(tuple(units), started, self._clock.monotonic())

    async def read_unit(self, unit: UnitConfig) -> UnitSnapshot:
        registers: dict[int, int] = {}
        coils: dict[int, bool] = {}
        failed_optional: list[str] = []
        for request in self._plans[unit.unit_id]:
            try:
                if request.table is Table.HOLDING_REGISTER:
                    reg_values = await self._transport.read_holding_registers(
                        unit.unit_id, request.address, request.count
                    )
                    _store(registers, request, reg_values)
                else:
                    coil_values = await self._transport.read_coils(
                        unit.unit_id, request.address, request.count
                    )
                    _store(coils, request, coil_values)
            except (WtcModbusExceptionError, WtcMalformedResponseError) as err:
                if request.required:
                    return self._unavailable(unit, f"{request.describe()}: {err}")
                failed_optional.append(f"{request.describe()}: {err}")
            except WtcTransportError as err:
                return self._unavailable(unit, f"{type(err).__name__}: {err}")

        image = RawImage.build(registers, coils)
        return UnitSnapshot(
            unit_id=unit.unit_id,
            available=True,
            controller=decode_controller(image),
            zones=tuple(decode_zone(image, zone) for zone in sorted(unit.zones)),
            error=None,
            failed_optional_reads=tuple(failed_optional),
            raw=image,
            read_at=self._clock.monotonic(),
        )

    def _unavailable(self, unit: UnitConfig, error: str) -> UnitSnapshot:
        return UnitSnapshot(
            unit_id=unit.unit_id,
            available=False,
            controller=None,
            zones=(),
            error=error,
            failed_optional_reads=(),
            raw=None,
            read_at=self._clock.monotonic(),
        )


def _store[T](target: dict[int, T], request: ReadRequest, values: Sequence[T]) -> None:
    """Keep only the addresses that were asked for, not gap fillers."""
    for point in request.points:
        target[point.address] = values[point.address - request.address]
