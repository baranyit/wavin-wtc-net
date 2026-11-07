# Hardware read validation, 2026-11-07 (plan step 4, first run)

Read-only run of `tests/hardware/test_read_validation.py` against the real installation
(`192.168.1.210:502`, units 1:6 and 2:4 zones). Defaults only: 1 request/s, 15/16 caps, write mode
off, one first cycle plus two steady cycles. 169 s, 83 requests, 0 failures, zero writes.

| Question | Result | Status |
|---|---|---|
| `max_gap=8` bridging (decision 3) | `hole_failures` empty, no excluded points | Keep default; option C not needed |
| Wheel formula | `offset = (code - 6) x 0.5 C` for all 10 zones (codes 4..12) | Consistent with 0.5 C; see dial check below |
| Packed words 4600-4603 | Low byte = odd zone code, high byte = even zone code; match the per-zone codes | Confirmed on this install |
| Status vs control bits (decision 5) | On/off and comfort/economy status equal control coils in all zones | Confirmed |
| Heat/cool polarity | `False` everywhere while controller mode = HEAT | Cool polarity still unobserved |
| Setpoints | 26 / 22 / 17 / 30 in every zone, no +-1 C compensation visible | Consistent with neutral 22 C |
| Firmware / serial | 1011 on both units; serials differ per unit | Serial identity still not allowed until validated |
| Lock status | All unlocked | Lock delay not tested |
| Latency | about 0.56 s per request, cycles take 26-28 s | Poll minimum 44 s is adequate |

## Dial check (Dolgozó, unit 2 zone 2)

The maintainer reported the physical state: target 24 C, heating, comfort. The run read code 10,
offset +2.0 C, comfort heat setpoint 22 C, comfort bit set, heat/cool bit clear.
`22 + (10 - 6) x 0.5 = 24 C` matches. The 1 C-per-code candidate would give 26 C, so it is
rejected for this installation. One zone, one value: re-check one more zone and a cooling case
before treating the 0.5 C step as universal.

Still open: cooling polarity (needs cool mode), lock delay.
Raw reports stay local (`tests/hardware/reports/`, git-ignored).
