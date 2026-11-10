"""Read-only snapshot client for all controllers behind one gateway endpoint.

The read path has no route to any write operation. Explicit user commands will be
added in a later milestone as a separate API (``commands.py``).

Read policy:

* static points (firmware, serial) are read on the first cycle after a (re)connect and then
  at most every ``static_refresh_interval``; last known values are kept in between;
* ``max_gap`` (0..8) bridges unused addresses to save requests;
* a failing request never causes extra traffic beyond a bounded same-cycle fallback:
  first without hole reads, then with corroborated points only. Optional points that failed
  are skipped for the rest of the connection.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from .errors import WtcMalformedResponseError, WtcModbusExceptionError, WtcTransportError
from .models import GatewaySnapshot, RawImage, UnitSnapshot
from .protocol import (
    MAX_ZONES,
    Confidence,
    Point,
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
MAX_ALLOWED_GAP = 8
DEFAULT_STATIC_REFRESH_INTERVAL = 3600.0
ABSOLUTE_MIN_POLL_INTERVAL = 30.0
POLL_SLACK_FACTOR = 1.5
_MAX_HOLE_FAILURE_LOG = 20

_Key = tuple[Table, int]


def minimum_poll_interval(requests: int, request_spacing: float) -> float:
    """Smallest allowed poll interval for a plan of ``requests`` requests per cycle.

    ``ceil(requests x request_spacing x 1.5)``, never below 30 s. Pass the size of the
    *first-cycle* (full) plan, which includes the static points.
    """
    if requests < 1 or request_spacing <= 0:
        raise ValueError("requests and request_spacing must be positive")
    return max(
        ABSOLUTE_MIN_POLL_INTERVAL, float(math.ceil(requests * request_spacing * POLL_SLACK_FACTOR))
    )


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


@dataclass(slots=True)
class _UnitState:
    """Mutable per-unit read state; never leaves the client (snapshots stay immutable)."""

    generation: int = -1
    static_read_at: float | None = None
    static_registers: dict[int, int] = field(default_factory=dict)
    excluded: dict[_Key, str] = field(default_factory=dict)
    no_bridge: bool = False


class WtcReadClient:
    def __init__(
        self,
        transport: GatewayTransport,
        units: Sequence[UnitConfig],
        *,
        max_gap: int = DEFAULT_MAX_GAP,
        static_refresh_interval: float = DEFAULT_STATIC_REFRESH_INTERVAL,
        clock: Clock | None = None,
    ) -> None:
        if not units:
            raise ValueError("at least one unit is required")
        if not 0 <= max_gap <= MAX_ALLOWED_GAP:
            raise ValueError(f"max_gap must be 0..{MAX_ALLOWED_GAP}")
        if static_refresh_interval <= 0:
            raise ValueError("static_refresh_interval must be positive")
        ids = [unit.unit_id for unit in units]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate unit ids")
        self._transport = transport
        self._units = tuple(units)
        self._max_gap = max_gap
        self._static_refresh_interval = static_refresh_interval
        self._clock: Clock = clock or AsyncioClock()
        self._points: dict[int, tuple[Point, ...]] = {
            unit.unit_id: unit_points(unit.zones) for unit in self._units
        }
        self._state: dict[int, _UnitState] = {unit.unit_id: _UnitState() for unit in self._units}
        self._hole_failures: dict[str, int] = {}
        self._plans = {unit.unit_id: self._build_plan(unit, False) for unit in self._units}
        self._full_plans = {unit.unit_id: self._build_plan(unit, True) for unit in self._units}

    @property
    def units(self) -> tuple[UnitConfig, ...]:
        return self._units

    def plan(self, unit_id: int) -> tuple[ReadRequest, ...]:
        """Steady-state plan: static points omitted."""
        return self._plans[unit_id]

    def full_plan(self, unit_id: int) -> tuple[ReadRequest, ...]:
        """First-cycle plan: includes the static points."""
        return self._full_plans[unit_id]

    @property
    def requests_per_cycle(self) -> int:
        """Requests in a steady-state cycle (without exclusions or fallbacks)."""
        return sum(len(plan) for plan in self._plans.values())

    @property
    def first_cycle_requests(self) -> int:
        """Requests in the first cycle after a (re)connect."""
        return sum(len(plan) for plan in self._full_plans.values())

    @property
    def minimum_poll_interval(self) -> float:
        """Smallest poll interval the options flow may accept for this plan."""
        return minimum_poll_interval(
            self.first_cycle_requests, self._transport.settings.min_request_interval
        )

    @property
    def hole_failures(self) -> dict[str, int]:
        """Requests that failed while bridging holes but succeeded without them (diagnostics)."""
        return dict(self._hole_failures)

    async def read_snapshot(self) -> GatewaySnapshot:
        started = self._clock.monotonic()
        units = []
        for unit in self._units:
            units.append(await self.read_unit(unit))
        return GatewaySnapshot(tuple(units), started, self._clock.monotonic())

    async def read_unit(self, unit: UnitConfig) -> UnitSnapshot:
        state = self._state[unit.unit_id]
        self._begin_unit(state)
        include_static = self._static_due(state)
        registers: dict[int, int] = {}
        coils: dict[int, bool] = {}
        failed_optional: list[str] = []
        error: str | None = None

        for request in self._current_plan(unit, state, include_static):
            try:
                await self._read_request(unit, state, request, registers, coils, failed_optional)
            except (WtcModbusExceptionError, WtcMalformedResponseError) as err:
                error = f"{request.describe()}: {err}"
            except WtcTransportError as err:
                error = f"{type(err).__name__}: {err}"
            if error is not None:
                state.generation = self._transport.connection_generation
                return self._unavailable(unit, error)

        state.generation = self._transport.connection_generation
        if include_static:
            state.static_read_at = self._clock.monotonic()
            for point in self._points[unit.unit_id]:
                if point.static and point.address in registers:
                    state.static_registers[point.address] = registers[point.address]
        merged = {**state.static_registers, **registers}

        image = RawImage.build(merged, coils)
        return UnitSnapshot(
            unit_id=unit.unit_id,
            available=True,
            controller=decode_controller(image),
            zones=tuple(decode_zone(image, zone) for zone in sorted(unit.zones)),
            error=None,
            failed_optional_reads=tuple(failed_optional),
            raw=image,
            read_at=self._clock.monotonic(),
            excluded_points=tuple(sorted(state.excluded.values())),
        )

    # -- planning -------------------------------------------------------------------

    def _build_plan(self, unit: UnitConfig, include_static: bool) -> tuple[ReadRequest, ...]:
        return self._plan_points(
            [p for p in self._points[unit.unit_id] if include_static or not p.static],
            self._max_gap,
        )

    def _plan_points(
        self, points: Sequence[Point], max_gap: int, blocked: Collection[_Key] = ()
    ) -> tuple[ReadRequest, ...]:
        settings = self._transport.settings
        return plan_reads(
            points,
            max_registers=settings.max_registers_per_request,
            max_coils=settings.max_coils_per_request,
            max_gap=max_gap,
            blocked=blocked,
        )

    def _begin_unit(self, state: _UnitState) -> None:
        """A (re)connect resets what this connection learned: static timer, exclusions, gap."""
        if not self._transport.stats.connected or state.generation != (
            self._transport.connection_generation
        ):
            state.static_read_at = None
            state.excluded.clear()
            state.no_bridge = False

    def _static_due(self, state: _UnitState) -> bool:
        if state.static_read_at is None:
            return True
        return self._clock.monotonic() - state.static_read_at >= self._static_refresh_interval

    def _current_plan(
        self, unit: UnitConfig, state: _UnitState, include_static: bool
    ) -> tuple[ReadRequest, ...]:
        if not state.excluded and not state.no_bridge:
            return self._full_plans[unit.unit_id] if include_static else self._plans[unit.unit_id]
        points = [
            p
            for p in self._points[unit.unit_id]
            if (include_static or not p.static) and (p.table, p.address) not in state.excluded
        ]
        return self._plan_points(points, 0 if state.no_bridge else self._max_gap, state.excluded)

    # -- reading with bounded fallback -------------------------------------------------

    async def _read_request(
        self,
        unit: UnitConfig,
        state: _UnitState,
        request: ReadRequest,
        registers: dict[int, int],
        coils: dict[int, bool],
        failed_optional: list[str],
    ) -> None:
        try:
            await self._execute(unit, request, registers, coils)
            return
        except (WtcModbusExceptionError, WtcMalformedResponseError) as err:
            first_error = err

        if not request.required:
            self._exclude(state, request.points)
            failed_optional.append(f"{request.describe()}: {first_error}")
            return

        # Stage A: the failure may come from bridged holes -> retry without hole reads.
        if request.count > len(request.points):
            try:
                for sub in self._plan_points(request.points, 0):
                    await self._execute(unit, sub, registers, coils)
            except (WtcModbusExceptionError, WtcMalformedResponseError):
                pass
            else:
                state.no_bridge = True
                key = request.describe()
                if key in self._hole_failures or len(self._hole_failures) < _MAX_HOLE_FAILURE_LOG:
                    self._hole_failures[key] = self._hole_failures.get(key, 0) + 1
                return

        # Stage B: corroborated points only. Optional points of this request are dropped
        # for the rest of the connection. A failure here propagates -> unit unavailable.
        keep = [p for p in request.points if p.confidence is Confidence.BOTH]
        dropped = [p for p in request.points if p.confidence is not Confidence.BOTH]
        for sub in self._plan_points(keep, 0):
            await self._execute(unit, sub, registers, coils)
        self._exclude(state, dropped)
        failed_optional.append(f"{request.describe()}: {first_error}")

    async def _execute(
        self,
        unit: UnitConfig,
        request: ReadRequest,
        registers: dict[int, int],
        coils: dict[int, bool],
    ) -> None:
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

    @staticmethod
    def _exclude(state: _UnitState, points: Sequence[Point]) -> None:
        for point in points:
            state.excluded[(point.table, point.address)] = (
                f"{point.table.value}[{point.address}] {point.name}"
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
