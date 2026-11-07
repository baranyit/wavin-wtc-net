# Vendor document: WTC3 BMS communication, ver 1.9 / EN 8 (www.wavin.hu)

Source: maintainer-supplied PDF `wavin_modbus_bms_interface_v1_9_en.pdf` (not stored in the repo).
This file records what the vendor states, so it can be cited. It is the first **vendor** evidence;
the confidence labels in `docs/02-protocol-specification.md` and `AGENTS.md` §6 predate it and must
be reviewed against it (see "Follow-ups"). Addresses are offset 0 as in the vendor table.

## Bus and protocol

- Native interface is RS-485 Modbus-RTU, 9600 8N1, answer timeout 2 s, max 64 devices per bus.
  WTC-NET is the TCP side; the vendor document does not describe the gateway itself.
- Max per message: 30 registers or 480 bits (our caps of 15 / 16 are deliberately far below).
- Functions: 01/02 read bits, 03/04 read registers, 05 and 06 single write (firmware >= 1.03),
  15 and 16 multiple write.

## Registers (read only)

- 4121 water temp; 4122–4126 DWP inputs 1–5 (kΩ); 4127 humidity; 4128 sensor temp; 4132 PI output;
  4133 dew point; 4134 DIP4 program switch (0..15); 4135 mode (0 init, 1 heating, 2 cooling,
  3 changeover). 4129–4131 are **not documented**.
- TH1…TH7 blocks of 6 from 4136: temp, humidity, dew point, wheel difference (signed, **0.1 °C**,
  e.g. 0x001E = +3 °C), DWP input resistance, light (lux). Seven blocks exist (4136–4177) whatever
  the installed zone count.
- 4500 firmware (1.08 = 1008), 4501/4502 serial (L-word, H-word), 4503 firmware mode / flash
  integrity (L-byte 2 = normal).
- 4600–4603 wheel position, L/H byte per thermostat (TH1/TH2, TH3/TH4, TH5/TH6, TH7 + DRT-200
  version in H-byte), wheel 0..12.

## Bits

- Read only: 4096–4110 inputs (DI1–DI15), 4112–4121 outputs, 4125 emergency stop (water > 65 °C),
  4126 water sensor error. Thermostat status blocks of 8 from 4130 (DRY, OUT, DWP, DWP_DI, ON/OFF,
  C/E, H/C, LIVE). Locks 4186–4192.
- Polarity (resolves open decision 5): ON/OFF on = 1; C/E comfort = 1; H/C cooling = 1;
  DRY drying = 1; LIVE connected = 1; condensation = 1; lock locked = 1.
- Read/write: 4127 master ON/OFF (ON = 1), 4128 master C/E (comfort = 1), 4129 master H/C
  (cooling = 1); per-thermostat controls 4604–4606, 4612–4614, … 4652–4654 (ON/OFF, C/E, H/C;
  block stride 8; "see" the status bits); locks 4656–4662 (locked = 1). 4194–4201: do not use.
- Per-thermostat H/C overwrite has no effect in slave mode; use the common bit 4129.
  Effect of C/E and ON/OFF depends on the Configurator settings.

## Read/write registers

- Setpoints 4178–4198 (3 per thermostat: cooling 100..500, heating 100..500, economy/heating
  50..500, all 0.1 °C). Economy cooling 4700–4706 (100..500). Drying setpoint 4707 (0.1 %).
- Wheel position 4604–4610, 0..12, lowest position = 0.
- Values written by the BMS are effective until the next power cycle, then defaults return.
  Out-of-range writes may cause problems or unexpected behaviour.

## Remote-mode timeout (BMS keepalive)

Only when C/E, H/C, ON/OFF are configured to DI/BUS mode: the BMS must keep writing the master
bits; default max gap 5 minutes (configurable up to 65000 s). Without refresh the WTC3 falls back
to its physical inputs. Factory default is master-thermostat-driven.

## Follow-ups (not done yet)

- Done 2026-11-07 (evidence table in `docs/02-protocol-specification.md`, `AGENTS.md` §6): re-grade of setpoints 4178+,
  4700+, packed wheel 4600–4603, wheel difference (B+3), firmware/serial, lock status/commands and
  zone H/C control are now vendor-documented. Documented does not mean hardware-validated, so
  keep writes off until plan step 4.
- Wheel scale: the vendor gives the wheel difference in 0.1 °C and positions 0..12 but no step
  size. A ±3 °C range over 12 steps would fit 0.5 °C/step; this is consistent, not proof.
- Open question "BMS timeout": answered for the documented configuration (above); the safe
  value and whether this installation uses DI/BUS mode are still unknown. No keepalive stays
  the rule.
- Decisions 4 and 5 were revisited: decision 5 is confirmed by the hardware run (status = control
  bits); decision 4 keeps the code-level `both` / `reference-only` split.
