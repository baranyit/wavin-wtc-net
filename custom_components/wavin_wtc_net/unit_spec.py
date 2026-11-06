"""Parse and format the user-facing controller description ``"1:6, 2:4"``.

Each item is ``<modbus unit id>:<number of zones>``; zones are always numbered 1..N.
Pure Python: no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping

from .protocol import MAX_ZONES, validate_unit_id


class UnitSpecError(ValueError):
    """The unit description is malformed."""


def parse_units(text: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in text.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        unit_text, sep, zones_text = item.partition(":")
        try:
            if not sep:
                raise ValueError
            unit_id = validate_unit_id(int(unit_text))
            zones = int(zones_text)
        except ValueError as err:
            raise UnitSpecError(f"invalid item {item!r}") from err
        if not 1 <= zones <= MAX_ZONES:
            raise UnitSpecError(f"zones must be 1..{MAX_ZONES}: {item!r}")
        if unit_id in result:
            raise UnitSpecError(f"duplicate unit {unit_id}")
        result[unit_id] = zones
    if not result:
        raise UnitSpecError("no units given")
    return result


def format_units(units: Mapping[int, int]) -> str:
    return ", ".join(f"{unit}:{zones}" for unit, zones in sorted(units.items()))
