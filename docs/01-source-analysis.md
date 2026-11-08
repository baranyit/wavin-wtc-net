# Source analysis

Status: clean-room source audit; no hardware access. Examined local commits: Python reference `e7c4f951ab8c497fcfb2f08a15340404a391ac89`, HomeGate `405777d050c640f1b99f67b4b7fcbc1539b93bb2`, the historical homelab Wavin record (data dated 2026-09-11), and the user-supplied 2026-11-06 topology.

## Python reference

The HACS-style integration (`wavin_wtc3` 1.0.2, `pymodbus>=3.8.0`) uses a coordinator, one client/slave per entry, config/options flows, and climate/sensor/switch platforms. It polls every 30 s (`const.py:23-24`, `__init__.py:63-75`), reads registers/coils in 15/16-item chunks with 50 ms gaps (`api.py:180-220`), and exposes controller plus 1–7 zone devices.

Critical findings:

1. `read_all()` rewrites every differing active comfort setpoint to raw 240/250 (`api.py:346-377`). Setup validation and every coordinator refresh call it (`config_flow.py:47-59`, `__init__.py:63-79`). Polling therefore writes configuration. The new design forbids automatic/configuration writes from setup, refresh, reconnect, diagnostics and unload; a refresh may flush only explicit queued user commands before reading.
2. Heat/cool/economy centers are hard-coded as 25/24/17/24 °C (`const.py:73-78`), conflicting with the real installation's 22/26 °C profile.
3. Setpoints use an unverified −1 °C decode/+1 °C encode compensation (`api.py:80-96`).
4. Wheel codes 0–12 are treated as −6…+6 °C in 1 °C steps (`api.py:99-111`), and climate requests are rounded to integers (`climate.py:213-217`).
5. TH1 mode/preset changes also write global coils (`climate.py:168-203`), an unsafe hidden whole-controller side effect.
6. IDs contain `entry.entry_id` (`entity.py:20-45`); remove/re-add can change identity.
7. Multiple entries on one endpoint have independent clients/locks (`api.py:143-150`).
8. Lock writes skip immediate verification because status lags (`api.py:295-305`); delayed verification is the correct general pattern.
9. The README mentions periodic global writes for BMS timeout, but no exact timeout/register/recovery model is implemented. Keepalive must remain disabled.

## HomeGate

HomeGate is useful protocol evidence, not an architectural template.

- YAML `units` keys become `Wtc.unitId` (`WtcHandler.java:51-54`) and are passed via `setUnitID` on every request (`WtcConnector.java:55-80`). Units 1 and 2 are therefore Modbus unit IDs in this code.
- Requests share a lock and a one-request-per-second limiter (`WtcConnector.java:32-35,84-99`). Scheduled refresh walks every unit sequentially and reads its register ranges before its coil ranges (`WtcHandler.java:56-81`). This conservative pacing is direct evidence from the comparatively stable HomeGate deployment and is the baseline for the new transport.
- HomeGate groups sparse addresses into spans using fixed start-to-address limits of 30 registers and 480 coils (`AddressableRange.java:10-40`, `Wtc.java:68-80`). The grouping concept is retained, but the literal 480-coil ceiling is not: it can still produce a large wire request. The new implementation must split every logical span again by a small hard request-size cap.
- Controller register 4121/4135 and global coils 4127–4129 match the Python source (`Wtc.java:21-26`).
- Zone registers start 4136, stride 6; status coils 4130, stride 8; writable controls 4604, stride 8; lock writes 4656+ (`Thermostat.java:16-23,50-97`).
- Wheel register `4604 + zone−1` maps as `neutral + (code−6)×wheel_step` (`Thermostat.java:62-64`; reverse: `WriteableRegister.java:24-30`). At 22 °C/0.5 °C, code 6 is 22 °C and the range is 19–25 °C.
- Command exceptions are logged and swallowed (`WriteableRegister.java:24-32`, `WriteableCoil.java:22-28`), so it is not a safe acknowledgement model.
- HomeGate executes MQTT-originated writes immediately (`WriteableRegister.java:24-30`, `WriteableCoil.java:22-25`). The new design intentionally changes this: user commands enter a coalescing queue and are written after a 2 s debounce (decision 22; originally flushed before the next read cycle). No read is triggered by a write, and one command is one request, so the traffic stays bounded and serialized.

## Conclusions

Both sources agree on core addressing and local Modbus TCP. They conflict on wheel scale (configurable 0.5 versus fixed 1 °C/code). The Python-only 4178+/4700+ setpoints, ±1 °C compensation, packed wheel words, firmware/serial and lock readback need hardware/vendor confirmation. Adopt HomeGate's serialized, one-request-per-second pacing and range grouping, refined with smaller hard block caps; adopt poll-aligned write coalescing followed by read-back. Reject poll-time *automatic* writes, hard-coded centers and TH1 master side effects.

