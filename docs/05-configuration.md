# Configuration

One config entry represents one endpoint: name, host, port (502), timeout (5 s), advanced address offset (0), controller unit IDs and write mode (off). Each controller stores enabled zone channels/names plus calibration, with optional zone overrides.

Real installation profile (not universal defaults): endpoint `192.168.1.210:502`; unit 1 zones Konyha, Nappali, Balu, Gábor, Luca, Fürdő; unit 2 zones Garázs, Dolgozó, Háló, Zuhany; heating/cooling neutral 22/26 °C; wheel step 0.5 °C.

Calibration fields: heat/cool comfort neutral; optional economy neutrals; wheel center (candidate 6), min/max code (candidate 0/12), °C/code; optional setpoint compensation disabled unless validated.

`target = active_reference(mode,preset) + (raw_code−center_code)×wheel_step`

If mode/preset is unknown, target is unavailable rather than guessed. A requested target must map exactly to a valid raw code within tolerance—never silently round away a half-step.

Setup: enter endpoint → read-only connectivity → explicit unit IDs or bounded user-approved read-only discovery → select/name zones → calibration → create read-only entry. Options cover polling, names, diagnostics and calibration. Transport safety defaults (one request/s, 15-register and 16-coil caps) are advanced settings with conservative bounds and warnings; normal users should not tune them. Reconfigure endpoint/unit IDs read-only. Enabling writes requires a separate warning/acknowledgement; MVP permits only validated wheel and optional lock writes. The UI states that commands take effect at the next poll and may be superseded by a newer pending command.

Observed H/C selects the active scale but never triggers automatic physical mode switching. Conflicting global/zone bits block ambiguous target writes and produce diagnostics.

