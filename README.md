# Wavin WTC-NET for Home Assistant

A native, local Home Assistant integration for **Wavin WTC-NET** underfloor heating/cooling
controllers over **Modbus TCP**. No cloud, no Java bridge, no MQTT.

> **Status: beta (0.2.0, not released yet).** Reading is the default and has been validated on one
> real installation. Writing is optional, off by default, and has been exercised on hardware on a
> single zone. See [what was validated](docs/supported-devices.md). This is an unofficial project,
> not affiliated with or endorsed by Wavin. Use it at your own risk.

## What it does

One config entry is one gateway endpoint (host and port) and can serve several controllers (Modbus
unit IDs), each with up to 7 zones. Per zone it provides:

- a **climate** entity (current temperature, humidity, heating/cooling/off, valve action),
- a **Target temperature** sensor derived from the thermostat's dial position,
- sensors and binary sensors for valve, dew point, condensation, dry mode, live connection and
  lock state; wheel and setpoint values as disabled diagnostic sensors,
- per controller: water temperature, operating mode, PI output, firmware, serial and fault bits.

Each controller and each zone is its own device. Entity IDs are based on a generated gateway ID,
the unit and the zone, so they survive renames and reloads.

## Safety first

The WTC-NET is treated as a fragile device.

- **Reads only by default.** Setup, polling, reconnect, reload, diagnostics and unload never write.
- **Slow and serialized.** One Modbus request starts per second per gateway, requests are capped
  (15 registers or 16 coils), cycles never overlap, timeouts and reconnects are bounded.
- **Writing is opt-in** (see below), per zone and per kind, and the integration never writes
  anything on its own: no corrective writes, no keepalive, no global switches.
- **Optimistic, but read-checked.** A command is shown at once, written after a 2 s quiet period
  (every further command restarts it, so an automation touching many thermostats is sent as one
  burst) and checked by the next poll. If the read shows something else (a dial turn, an ignored
  write) the real value comes back. Commands expire and are never replayed after a reconnect or
  reload.
- **The thermostat wins.** Polling never undoes a change made on the physical dial.

## Installation

HACS (custom repository) or manual copy:

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/baranyit/wavin-wtc-net`
   as an *Integration*, install **Wavin WTC-NET**, restart Home Assistant.
   Manual: copy `custom_components/wavin_wtc_net` into your Home Assistant `custom_components`.
2. Settings → Devices & services → Add integration → **Wavin WTC-NET**.

Requires Home Assistant 2026.2 or newer and `pymodbus` (installed automatically).

## Setup

Enter the gateway host, the port (502) and the controllers as `UNIT:ZONES`, for example
`1:6, 2:4` (unit 1 with 6 zones, unit 2 with 4 zones). Setup only reads (one register per
controller) to check connectivity. You can change the endpoint later with *Reconfigure*.

## Options

| Option | Default | Meaning |
|---|---|---|
| Poll interval | 60 s | Minimum is derived from the plan size (44 s for 6+4 zones), maximum 300 s. |
| Maximum address gap | 8 | Advanced: how many unused addresses a read may bridge (0 to 8). Lower means more requests. |
| Heating / cooling neutral | 22 / 26 °C | The dial centre of the thermostat in each mode. |
| Heating / cooling wheel step | 0.5 / 0.5 °C | Degrees per dial position. |
| Enable writing | off | Master switch for everything below. |
| Zones for target temperature | none | Zones where the climate entity can set a comfort target. |
| Zones for lock | none | Zones that get a lock entity (the thermostat's child lock). |
| Zones for on/off and preset | none | Zones where climate can turn on/off and switch comfort/eco. |

### How the target temperature works

For the comfort preset the thermostat's target is
`neutral + (wheel position − 6) × step`, separately for heating and cooling. In economy the dial
has no effect and the target is the economy setpoint. Economy cannot be set from Home Assistant.
A requested value must fit exactly on a dial position (0.5 °C steps by default) and in the range
neutral ± 6 steps. It is never silently rounded.

### What writing can do

With writing enabled for a zone you can:

- set the **comfort target** (one wheel register is written),
- **lock/unlock** the thermostat (one lock coil),
- **turn the zone on/off** and switch **comfort/eco** (one control coil each).

You cannot, by design: change heating/cooling, change setpoints, write the global controls of a
controller, or send a keepalive. Commands show their state (queued, sent, confirmed, timed out,
rejected) in the entity attributes and in the diagnostics.

## Known limitations

- Hardware validation covers one installation (two controllers with 6 and 4 zones), and writes
  were exercised on one zone only. Other thermostat models or firmware are untested.
- **Cooling is not validated on hardware.** The cooling target follows the same rule as heating by
  design, but it has only been tested against a simulator. Treat cooling writes as experimental.
- The behaviour of written values over a controller power cycle is untested.
- The serial number is shown but not used for identification.
- Automatic discovery of controllers does not exist; enter the unit IDs yourself.

## Diagnostics and troubleshooting

Download the diagnostics from the integration page: the host and the generated gateway ID are
redacted, raw register/coil images are included for support, plus transport statistics and the
recent commands. If a point cannot be read, a Repair issue lists the excluded points. For the
protocol notes, test reports and design decisions see [docs/](docs/README.md).

## Development

```bash
uv venv -p 3.13 && uv pip install -e ".[dev]"
source .venv/bin/activate
python -m pytest && ruff check . && ruff format --check . && mypy
```

The hardware tests under `tests/hardware` never run by default. They need an explicit host and
approval, and the write tests are limited to the approved zone. See `AGENTS.md` for the rules.

## License

MIT, see [LICENSE](LICENSE).
