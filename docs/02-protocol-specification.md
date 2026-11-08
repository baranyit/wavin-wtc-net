# Modbus protocol specification

Confidence: **both** means corroborated by Python and HomeGate; **reference-only** means only the Python reference listed it. Since 2026-11-07 two more evidence sources exist: the vendor BMS document (`vendor-bms-v1_9.md`, "vendor") and the first read-only hardware run (`hardware-validation-2026-11-07.md`, "hw"). The per-address evidence is in the table in "Evidence status" below. The `Confidence` enum in code still means only "both vs reference-only" (it drives the read fallback and write eligibility) and is deliberately not changed by this re-grade.

## Transport

Modbus TCP, default port 502. HomeGate proves its configured keys 1/2 are used as unit IDs, but hardware confirmation remains required. Source addresses are used with offset 0; an advanced offset may be retained for diagnostics. Observed functions: read holding registers/coils and write single register/coil.

The WTC-NET is treated as a fragile, rate-limited device. All unit IDs share one endpoint queue and at most one Modbus request starts per second, matching HomeGate's proven limiter. Addresses are first grouped into logical contiguous/near-contiguous spans, then split by hard wire-request caps. Initial caps are 15 holding registers and 16 coils per request; they may only be increased from hardware soak evidence. Thus neither many small bursts nor one oversized range is allowed. The Python reference's 50 ms gap is not the default because it is substantially more aggressive than the stable HomeGate cadence.

## Registers

| Address | Meaning/encoding | Confidence |
|---:|---|---|
| 4121 | water temperature, signed ×0.1 °C | both |
| 4127 | controller RH ×0.1 % | reference-only |
| 4128 | controller room temp, signed ×0.1 °C | reference-only |
| 4132 | PI output ×0.1 % | reference-only |
| 4133 | dew point ×0.1 °C | reference-only |
| 4134 | program/DIP raw code | reference-only |
| 4135 | 0 init, 1 heat, 2 cool, 3 changeover | both |
| 4500 | firmware, decimal interpretation | reference-only |
| 4501–4502 | serial low/high, combined 32-bit | reference-only |

Python treats `0xFFFF`/`0xFFFE` as invalid (`api.py:74-77,128-136`).

For zone `z` (1…7), `B=4136+6×(z−1)`: `B+0` temperature ×0.1, `+1` RH ×0.1, `+2` dew point ×0.1 (both); `+3` wheel offset ×0.1, `+4` dew-point resistance, `+5` illuminance raw (reference-only).

Setpoints are reference-only: `4178+3×(z−1)` comfort cool, `+1` comfort heat, `+2` economy heat; `4700+(z−1)` economy cool. The reference's ±1 °C compensation is not protocol truth. Registers 4600–4603 pack two read-only wheel codes per word; `4604+(z−1)` is the writable wheel code (address corroborated by both).

Candidate formulas:

- Chosen (hardware dial checks, heating): `target=neutral(heat|cool)+(code−6)×step(heat|cool)`, all four values configurable (real profile 22/26 °C, 0.5/0.5). Economy ignores the wheel.
- Rejected for this installation: Python `offset=code−6` °C (1 °C per code); kept as a testable candidate in code only.

No setpoint register may be rewritten by a read path.

## Coils

Global: 4127 on/off, 4128 comfort/economy, 4129 heat/cool (both). Reference-only outputs/faults: 4120 heat, 4121 cool, 4125 emergency water temperature, 4126 water-sensor fault.

Zone status `S=4130+8×(z−1)`: DRY, OUT/valve, condensation, DWP input, on/off, comfort/economy, heat/cool, live. Offsets 0–3,6,7 are corroborated; 4–5 are reference-only.

Writable zone base `C=4604+8×(z−1)`: on/off, comfort/economy, heat/cool. HomeGate corroborates the first two. Lock command is `4656+(z−1)`; candidate authoritative delayed status is `4186+(z−1)`.

## Evidence status (2026-11-07)

"vendor" = documented in the vendor BMS document; "hw read" = read on the real installation with plausible values; "hw dial" = value checked against the physical thermostat. Documented or read does not mean writable: every write path still needs its own approved test.

| Address(es) | Vendor | Hardware | Note |
|---|---|---|---|
| 4121, 4135, zone B+0..B+2 | yes | hw read | corroborated by all sources |
| 4127, 4128, 4132, 4133, 4134 | yes | hw read | not compared with a physical value |
| 4500 firmware | yes (1.08 = 1008) | hw read: 1011 on both units | decimal interpretation holds |
| 4501–4502 serial | yes (L-word, H-word) | hw read: distinct per unit | identity use still waits for a stability check |
| zone B+3 wheel offset | yes (0.1 °C signed) | hw dial: comfort heat 3 zones | economy: wheel not applied |
| zone B+4, B+5 | yes | hw read | not checked |
| 4600–4603 packed wheel | yes (L/H byte) | hw read: low byte = odd zone, high = even zone | matches 4604+ codes |
| 4604+(z−1) wheel code | yes (0..12) | hw read, dial 6 = center, 0.5 °C/code | writable, not yet written |
| 4178+3(z−1)+1, +2 comfort/economy heat | yes (read/write) | hw dial: 22 and 17 confirmed | |
| 4178+3(z−1)+0, 4700+ cooling setpoints | yes | hw read: 26 / 30 | no cooling dial check |
| zone status offsets 4, 5 | yes | hw read: equal to control coils 0, 1 | |
| zone status 0–3, 6, 7 | yes | hw read | cooling polarity not seen live |
| zone control coils +0, +1 | yes | hw write: on/off and comfort/economy changed, status followed in the same cycle (Dolgozó) | |
| zone control coil +2 (heat/cool) | yes, but no effect in slave mode; use 4129 | hw read | do not write |
| global coils 4127–4129 | yes | hw read | |
| coils 4120, 4121, 4125, 4126 | yes (outputs, emergency, sensor error) | hw read: all false | |
| lock status 4186+, command 4656+ | yes (locked = 1) | hw write: lock/unlock moved the status coil, confirmed in the same cycle (Dolgozó) | lag shorter than one cycle |

## Safety contract

- Reads and writes are separate APIs; polling can execute only explicitly queued user commands, never automatic corrective/configuration writes.
- Write mode defaults off. Zone and controller-global permissions are separate.
- Validate unit, zone, capability and raw range. A user command is queued, coalesced by target (latest value wins), and written after a 2 s debounce (restarted by each new command, bounded to 10 s) through the same one-request-per-second limiter and transport lock.
- No read follows a write; the next normal read cycle that started after the write provides verification. It reports queued, sent, confirmed, superseded, timeout, rejected or transport error. Entities may show the requested value until that read decides (optimistic overlay, decision 22), but a command is only ever *confirmed* by a read.
- No replay after reconnect/reload.
- No BMS keepalive in MVP. Its timeout, reset semantics and safe value are unknown.

## Hardware backlog

Confirm unit IDs/address offset/function families; correlate physical dial, 4139-style offset and 4600–4603 codes; validate setpoints without writing; confirm global/zone precedence and lock delay. Soak-test the one-request-per-second baseline and small block caps before any tuning; test large-range rejection without sending unsafe requests to real hardware. Obtain BMS timeout evidence from the vendor.

