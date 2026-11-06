# Modbus protocol specification

Confidence: **both** means corroborated by Python and HomeGate; **reference-only** needs hardware/vendor confirmation.

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

- HomeGate: `target=neutral(mode)+(code−6)×configured_step` (0.5 °C in the real profile).
- Python: `offset=code−6` °C.

No setpoint register may be rewritten by a read path.

## Coils

Global: 4127 on/off, 4128 comfort/economy, 4129 heat/cool (both). Reference-only outputs/faults: 4120 heat, 4121 cool, 4125 emergency water temperature, 4126 water-sensor fault.

Zone status `S=4130+8×(z−1)`: DRY, OUT/valve, condensation, DWP input, on/off, comfort/economy, heat/cool, live. Offsets 0–3,6,7 are corroborated; 4–5 are reference-only.

Writable zone base `C=4604+8×(z−1)`: on/off, comfort/economy, heat/cool. HomeGate corroborates the first two. Lock command is `4656+(z−1)`; candidate authoritative delayed status is `4186+(z−1)`.

## Safety contract

- Reads and writes are separate APIs; polling can execute only explicitly queued user commands, never automatic corrective/configuration writes.
- Write mode defaults off. Zone and controller-global permissions are separate.
- Validate unit, zone, capability and raw range. A user command is queued, coalesced by target (latest value wins), and flushed at the start of the next scheduled poll cycle through the same one-request-per-second limiter.
- After the queued writes, the normal read phase provides verification without an extra immediate request burst. It reports queued, sent, confirmed, superseded, timeout, rejected or transport error; never optimistic success.
- No replay after reconnect/reload.
- No BMS keepalive in MVP. Its timeout, reset semantics and safe value are unknown.

## Hardware backlog

Confirm unit IDs/address offset/function families; correlate physical dial, 4139-style offset and 4600–4603 codes; validate setpoints without writing; confirm global/zone precedence and lock delay. Soak-test the one-request-per-second baseline and small block caps before any tuning; test large-range rejection without sending unsafe requests to real hardware. Obtain BMS timeout evidence from the vendor.

