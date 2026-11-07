# Configuration

One config entry represents one endpoint: name, host, port (502), timeout (5 s), advanced address offset (0), controller unit IDs and write mode (off). Each controller stores enabled zone channels/names plus calibration, with optional zone overrides.

Real installation profile (not universal defaults): endpoint `192.168.1.210:502`; unit 1 zones Konyha, Nappali, Balu, Gábor, Luca, Fürdő; unit 2 zones Garázs, Dolgozó, Háló, Zuhany; heating/cooling neutral 22/26 °C; wheel step 0.5 °C.

Calibration options (implemented, defaults = the real profile): `neutral_heat` 22 °C, `neutral_cool` 26 °C, `wheel_step_heat` 0.5 °C and `wheel_step_cool` 0.5 °C per wheel position. Heating and cooling use the same rule with their own neutral and step. Wheel centre 6 and codes 0..12 are protocol constants in `WheelCalibration`. Economy targets do not use the wheel: economy heating is the economy setpoint register, economy cooling is unavailable until validated. There is no setpoint compensation.

Comfort: `target = neutral(heat|cool) + (raw_code−6)×step(heat|cool)`. Economy: see below. The target is shown as the read-only sensor "Target temperature" (`zone_target_temperature` in `protocol.py`).

If mode/preset is unknown, target is unavailable rather than guessed. A requested target must map exactly to a valid raw code within tolerance—never silently round away a half-step.

Setup: enter endpoint → read-only connectivity → explicit unit IDs or bounded user-approved read-only discovery → select/name zones → calibration → create read-only entry. Options cover polling, names, diagnostics and calibration. Transport safety defaults (one request/s, 15-register and 16-coil caps) are advanced settings with conservative bounds and warnings; normal users should not tune them. Reconfigure endpoint/unit IDs read-only. Enabling writes requires a separate warning/acknowledgement; MVP permits only validated wheel and optional lock writes. The UI states that commands take effect at the next poll and may be superseded by a newer pending command.

Write options (implemented): `write_enabled` (default off) and `writable_zones` (explicit list, default empty); write mode needs at least one zone. With writing on, a zone in the list gets a settable comfort target in heating (step `wheel_step_heat`, range neutral ±6 steps); the value is queued, sent as one wheel register at the start of the next poll cycle and shown only after the read-back. Targets that are not a whole wheel step, economy, cooling and off zones are rejected. Queued commands expire after 2 poll intervals and are dropped on reconnect.

Observed H/C selects the active scale but never triggers automatic physical mode switching. Conflicting global/zone bits block ambiguous target writes and produce diagnostics.

