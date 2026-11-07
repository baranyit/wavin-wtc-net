# Hardware zone mode test, 2026-11-08 (decision 16)

Approved by the maintainer for one zone, both kinds: Dolgozó (unit 2, zone 2), run one after the
other with `tests/hardware/test_mode_validation.py` (`WTC_MODE_TEST="2:2:preset"` and
`"2:2:power"`), through the integration's own writable transport, queue and poller. Mode writes
enabled for this one zone only; conservative defaults; poll grid 44 s. Start state: on, comfort,
heating, wheel code 10 (24 °C).

Result: **both passed**. Each run 115 s, 85 requests, 0 failures, exactly 2 writes (one function-05
control-coil write for the change, one for the restore), all confirmed, zone restored.

| Run | Cycle ended | Status coils after the write | Command |
|---|---|---|---|
| preset: precondition | 00:27:06 | comfort/economy comfort, on | none |
| preset: economy | 00:27:49 | comfort/economy **economy** (control equal) | queued → sent → confirmed |
| preset: restore | 00:28:33 | comfort | queued → sent → confirmed |
| power: precondition | 00:29:18 | on | none |
| power: off | 00:30:01 | on/off **off** (control equal) | queued → sent → confirmed |
| power: restore | 00:30:45 | on | queued → sent → confirmed |

Findings:
- Control coil offsets 0 (on/off) and 1 (comfort/economy) of 4604+8(z−1) work, the status coils
  at offsets 4 and 5 followed in the read phase of the same cycle (0 extra cycles). Status and
  control bits stayed equal throughout.
- Nothing else moved: heat/cool stayed heating (coil unchanged, never written), the wheel code stayed 10.
- Physical display (maintainer): economy and the switch-off were both visible on the Dolgozó
  thermostat; the switch-on was visible after the restore.
- The configurator setting did not block the BMS-side change in this installation.
- Not tested: other zones, cooling, behaviour over a power cycle, interaction with the master bits.
