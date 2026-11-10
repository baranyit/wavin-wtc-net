from __future__ import annotations

import itertools
import random

import pytest

from custom_components.wavin_wtc_net import protocol as p
from custom_components.wavin_wtc_net.models import ControllerMode, RawImage

# --- codecs ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0.0),
        (215, 21.5),
        (0xFF9C, -10.0),
        (0x8000, -3276.8),
        (0x7FFD, 3276.5),
        (0xFFFF, None),
        (0xFFFE, None),
        (None, None),
    ],
)
def test_signed_tenths(raw: int | None, expected: float | None) -> None:
    assert p.decode_signed_tenths(raw) == expected


def test_unsigned_tenths_and_raw_sentinels() -> None:
    assert p.decode_unsigned_tenths(452) == 45.2
    assert p.decode_unsigned_tenths(0xFFFF) is None
    assert p.decode_raw(0xFFFE) is None
    assert p.decode_raw(42) == 42


def test_to_signed16_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        p.to_signed16(0x10000)
    with pytest.raises(ValueError):
        p.to_signed16(-1)


def test_serial_and_mode() -> None:
    assert p.decode_serial(0x5679, 0x0012) == 0x00125679
    assert p.decode_serial(0xFFFF, 0x0012) is None
    assert p.decode_serial(None, 1) is None
    assert p.decode_mode(2) is ControllerMode.COOL
    assert p.decode_mode(9) is None


def test_packed_wheel_bytes_both_orders() -> None:
    word = (7 << 8) | 5
    assert p.unpack_wheel_codes(word, p.WheelByteOrder.LOW_BYTE_FIRST) == (5, 7)
    assert p.unpack_wheel_codes(word, p.WheelByteOrder.HIGH_BYTE_FIRST) == (7, 5)
    assert p.packed_wheel_code(word, 1, p.WheelByteOrder.LOW_BYTE_FIRST) == 5
    assert p.packed_wheel_code(word, 2, p.WheelByteOrder.LOW_BYTE_FIRST) == 7
    assert p.packed_wheel_code(word, 7, p.WheelByteOrder.HIGH_BYTE_FIRST) == 7


# --- address map -------------------------------------------------------------


def test_zone_address_boundaries() -> None:
    assert p.zone_register_base(1) == 4136
    assert p.zone_register_base(7) == 4172
    assert p.zone_setpoint_base(1) == 4178
    assert p.zone_setpoint_base(7) == 4196
    assert p.zone_economy_cool_register(7) == 4706
    assert p.zone_packed_wheel_register(1) == 4600
    assert p.zone_packed_wheel_register(2) == 4600
    assert p.zone_packed_wheel_register(7) == 4603
    assert p.zone_wheel_code_register(1) == 4604
    assert p.zone_wheel_code_register(7) == 4610
    assert p.zone_status_coil_base(1) == 4130
    assert p.zone_status_coil_base(7) == 4178
    assert p.zone_control_coil_base(7) == 4652
    assert p.zone_lock_status_coil(7) == 4192


@pytest.mark.parametrize("zone", [0, 8, -1])
def test_invalid_zone_rejected(zone: int) -> None:
    with pytest.raises(ValueError):
        p.zone_register_base(zone)


def test_unit_points_have_no_collisions_and_never_touch_command_coils() -> None:
    points = p.unit_points(range(1, 8))
    keys = [(pt.table, pt.address) for pt in points]
    assert len(keys) == len(set(keys))
    coils = {pt.address for pt in points if pt.table is p.Table.COIL}
    lock_commands = {p.COIL_LOCK_COMMAND_BASE + z - 1 for z in range(1, 8)}
    assert not coils & lock_commands


# --- read planning -----------------------------------------------------------


def _check_plan(
    points: tuple[p.Point, ...],
    plan: tuple[p.ReadRequest, ...],
    max_registers: int,
    max_coils: int,
    max_gap: int,
) -> None:
    covered: list[tuple[p.Table, int]] = []
    for request in plan:
        cap = max_registers if request.table is p.Table.HOLDING_REGISTER else max_coils
        assert 1 <= request.count <= cap
        addresses = [pt.address for pt in request.points]
        assert addresses[0] == request.address
        assert addresses[-1] == request.last_address
        assert all(b - a - 1 <= max_gap for a, b in itertools.pairwise(addresses))
        covered += [(pt.table, pt.address) for pt in request.points]
    expected = sorted((pt.table.value, pt.address) for pt in points)
    assert sorted((t.value, a) for t, a in covered) == expected


def test_plan_respects_caps_and_covers_every_point_exactly_once() -> None:
    rng = random.Random(1234)
    for _ in range(300):
        max_registers, max_coils = rng.randint(1, 30), rng.randint(1, 64)
        max_gap = rng.randint(0, 12)
        points = tuple(
            p.Point(rng.choice(list(p.Table)), address, f"pt{address}", p.Confidence.BOTH)
            for address in rng.sample(range(4000, 4800), rng.randint(1, 120))
        )
        # same address may appear in both tables; dedupe per table like real plans do
        unique = {(pt.table, pt.address): pt for pt in points}
        points = tuple(unique.values())
        plan = p.plan_reads(
            points, max_registers=max_registers, max_coils=max_coils, max_gap=max_gap
        )
        _check_plan(points, plan, max_registers, max_coils, max_gap)


def test_plan_orders_registers_before_coils_and_marks_optional_requests() -> None:
    plan = p.plan_reads(p.unit_points(range(1, 7)), max_registers=15, max_coils=16, max_gap=8)
    tables = [req.table for req in plan]
    assert tables == sorted(tables, key=lambda t: t is p.Table.COIL)
    firmware = next(req for req in plan if req.address == p.REG_FIRMWARE)
    assert not firmware.required
    first = plan[0]
    assert first.address == p.REG_WATER_TEMPERATURE and first.count == 15 and first.required


def test_six_zone_plan_size() -> None:
    plan = p.plan_reads(p.unit_points(range(1, 7)), max_registers=15, max_coils=16, max_gap=8)
    registers = [r for r in plan if r.table is p.Table.HOLDING_REGISTER]
    coils = [r for r in plan if r.table is p.Table.COIL]
    assert (len(registers), len(coils)) == (8, 8)


def test_plan_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        p.plan_reads((), max_registers=0, max_coils=16, max_gap=0)
    with pytest.raises(ValueError):
        p.plan_reads((), max_registers=15, max_coils=16, max_gap=-1)


# --- wheel math --------------------------------------------------------------

HALF_DEGREE = p.WheelScale(step=0.5)
ONE_DEGREE = p.WheelScale(step=1.0)


def test_half_degree_wheel_formula_matches_real_profile() -> None:
    scale = HALF_DEGREE
    assert p.wheel_code_to_target(6, 22.0, scale) == 22.0
    assert p.wheel_code_to_target(0, 22.0, scale) == 19.0
    assert p.wheel_code_to_target(12, 22.0, scale) == 25.0
    assert p.wheel_code_to_target(7, 26.0, scale) == 26.5  # cooling neutral


def test_one_degree_wheel_formula() -> None:
    scale = ONE_DEGREE
    assert p.wheel_code_to_target(0, 22.0, scale) == 16.0
    assert p.wheel_code_to_target(12, 22.0, scale) == 28.0


@pytest.mark.parametrize("scale", [HALF_DEGREE, ONE_DEGREE])
@pytest.mark.parametrize("reference", [22.0, 26.0, 17.5])
def test_wheel_round_trip_every_code(scale: p.WheelScale, reference: float) -> None:
    for code in range(scale.min_code, scale.max_code + 1):
        target = p.wheel_code_to_target(code, reference, scale)
        assert p.target_to_wheel_code(target, reference, scale) == code


def test_half_degree_is_preserved_and_never_rounded() -> None:
    scale = HALF_DEGREE
    assert p.target_to_wheel_code(21.5, 22.0, scale) == 5
    with pytest.raises(ValueError):
        p.target_to_wheel_code(21.75, 22.0, scale)
    with pytest.raises(ValueError):
        p.target_to_wheel_code(21.5, 22.0, ONE_DEGREE)
    with pytest.raises(ValueError):
        p.target_to_wheel_code(25.5, 22.0, scale)
    with pytest.raises(ValueError):
        p.wheel_code_to_target(13, 22.0, scale)


def test_wheel_scale_validation() -> None:
    with pytest.raises(ValueError):
        p.WheelScale(center_code=13)
    with pytest.raises(ValueError):
        p.WheelScale(step=0)


# --- decoding ----------------------------------------------------------------


def test_decode_with_missing_values_yields_none() -> None:
    image = RawImage.build({p.REG_WATER_TEMPERATURE: 315}, {})
    controller = p.decode_controller(image)
    assert controller.water_temperature == 31.5
    assert controller.mode is None and controller.firmware is None
    zone = p.decode_zone(image, 3)
    assert zone.temperature is None and zone.live is None


def test_blocked_address_is_never_bridged() -> None:
    pts = [
        p.Point(p.Table.COIL, 10, "a", p.Confidence.BOTH),
        p.Point(p.Table.COIL, 14, "b", p.Confidence.BOTH),
    ]
    caps = {"max_registers": 15, "max_coils": 16}
    assert len(p.plan_reads(pts, max_gap=8, **caps)) == 1
    plan = p.plan_reads(pts, max_gap=8, blocked=[(p.Table.COIL, 12)], **caps)
    assert [(r.address, r.count) for r in plan] == [(10, 1), (14, 1)]
    other = [(p.Table.HOLDING_REGISTER, 12)]
    assert len(p.plan_reads(pts, max_gap=8, blocked=other, **caps)) == 1


# --- wheel calibration (heating and cooling, decided 2026-11-07) ---------------------------


def _zone(code: int | None, *, economy_heat: float | None = 17.0) -> p.ZoneSnapshot:
    image = p.RawImage.build(
        {
            p.zone_wheel_code_register(1): code if code is not None else 0xFFFF,
            p.zone_setpoint_base(1) + 2: int((economy_heat or 0) * 10),
        },
        {},
    )
    return p.decode_zone(image, 1)


def test_calibration_defaults_match_the_real_installation() -> None:
    cal = p.WheelCalibration()
    assert (cal.neutral_heat, cal.neutral_cool, cal.step_heat, cal.step_cool) == (22, 26, 0.5, 0.5)
    assert cal.comfort_target(10, cooling=False) == 24.0  # Dolgozo dial check
    assert cal.comfort_target(9, cooling=False) == 23.5  # Nappali dial check
    assert cal.comfort_target(6, cooling=True) == 26.0
    assert cal.comfort_target(8, cooling=True) == 27.0


def test_heating_and_cooling_have_independent_neutral_and_step() -> None:
    cal = p.WheelCalibration(neutral_heat=21.0, neutral_cool=25.0, step_heat=0.5, step_cool=1.0)
    assert cal.comfort_target(8, cooling=False) == 22.0
    assert cal.comfort_target(8, cooling=True) == 27.0
    assert cal.comfort_target(0, cooling=True) == 19.0
    for code in range(13):
        for cooling in (False, True):
            target = cal.comfort_target(code, cooling=cooling)
            assert cal.comfort_code(target, cooling=cooling) == code


def test_calibration_never_rounds_a_partial_step() -> None:
    cal = p.WheelCalibration(step_cool=1.0)
    assert cal.comfort_code(24.5, cooling=False) == 11
    with pytest.raises(ValueError, match="whole wheel step"):
        cal.comfort_code(26.5, cooling=True)
    with pytest.raises(ValueError, match="outside wheel range"):
        cal.comfort_code(40.0, cooling=False)


@pytest.mark.parametrize("bad", [0.0, -0.5, float("nan"), float("inf")])
def test_calibration_rejects_invalid_steps(bad: float) -> None:
    with pytest.raises(ValueError, match="step"):
        p.WheelCalibration(step_heat=bad)
    with pytest.raises(ValueError, match="step"):
        p.WheelCalibration(step_cool=bad)


def test_calibration_rejects_non_finite_neutral() -> None:
    with pytest.raises(ValueError, match="neutral"):
        p.WheelCalibration(neutral_heat=float("nan"))
    with pytest.raises(ValueError, match="neutral"):
        p.WheelCalibration(neutral_cool=float("inf"))


def test_zone_target_comfort_economy_and_unknowns() -> None:
    cal = p.WheelCalibration()
    assert p.zone_target_temperature(_zone(10), cal, cooling=False, comfort=True) == 24.0
    # economy heating ignores the wheel (Konyha: code 5, dial 17 C)
    assert p.zone_target_temperature(_zone(5), cal, cooling=False, comfort=False) == 17.0
    # unvalidated or unknown -> unavailable, never guessed
    assert p.zone_target_temperature(_zone(5), cal, cooling=True, comfort=False) is None
    assert p.zone_target_temperature(_zone(None), cal, cooling=False, comfort=True) is None
    assert p.zone_target_temperature(_zone(13), cal, cooling=False, comfort=True) is None
