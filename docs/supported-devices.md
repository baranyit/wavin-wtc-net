# Supported devices and what was validated

Status as of 2026-11-08. "Validated" means observed on the real installation below, not a general
guarantee.

## Tested installation

- Gateway/controllers: WTC-NET over Modbus TCP, port 502, two controllers (Modbus unit IDs 1 and 2)
  with 6 and 4 zones, firmware 1011 on both.
- Season at test time: heating.

## Matrix

| Area | Status | Evidence |
|---|---|---|
| Connection, polling, rate and caps | validated | `hardware-validation-2026-11-07.md` (83 requests, 0 failures) |
| Temperature, humidity, dew point, valve, live | read validated | same |
| Wheel position and target formula (heating comfort) | validated on 3 zones | dial checks in the same report |
| Economy target = economy setpoint (heating) | validated on 1 zone | same |
| Firmware and serial registers | read validated | same |
| Comfort target write (heating) | validated on 1 zone (Dolgozó) | `hardware-write-test-2026-11-07.md` |
| Physical dial after a write | validated | same (second run) |
| Lock / unlock | validated on 1 zone | `hardware-lock-test-2026-11-08.md` |
| Zone on/off, comfort/eco | validated on 1 zone | `hardware-mode-test-2026-11-08.md` |
| Cooling polarity and cooling target | **not validated on hardware** (simulator only) | open until the cooling season |
| Economy cooling target | not supported | decision 14 |
| Global controls, BMS keepalive, setpoint writes | not supported | decisions 14, 15 |
| Persistence of writes over a power cycle | untested | accepted risk, handle if it occurs |
| Other zones, other thermostat models/firmware | untested | no evidence |

## Reporting a new installation

Open an issue with the diagnostics file (host and gateway ID are redacted), the controller and zone
counts, the firmware, and whether the dial check from `docs/hardware-validation-2026-11-07.md`
matches your neutral and step settings.
