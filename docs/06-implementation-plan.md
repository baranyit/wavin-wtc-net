# Implementation plan

1. **Evidence freeze:** preserve revisions/confidence map; obtain vendor material. Gate: unresolved items cannot enable writes.
2. **Read-only protocol core (first milestone):** pure codecs/snapshots, HomeGate-inspired serialized async scheduler, logical range grouping plus small hard chunks, two-unit 6+4 mock. Gate: 24-hour polling/reconnect/malformed-response suite with zero write calls, no overlapping cycles, no rate/cap violation.
3. **HA read-only integration:** config/options/reconfigure, coordinator, controller/zone entities, localization/diagnostics. Gate: stable IDs, isolated partial failure, hassfest/HACS green.
4. **Hardware read validation:** confirm addressing/rate and correlate dial, offsets, packed codes, setpoints and locks. Gate: signed report establishes exact wheel formula/resolution/range.
5. **Conservative zone commands:** opt-in wheel commands enter a latest-value-wins queue, flush before the next poll reads, and are verified by those reads; then optional lock. Gate: no immediate/out-of-cycle traffic, one-zone effect, half-degree round trip, no replay/optimism.
6. **Extended controls:** evaluate zone mode/preset, then separately permissioned global controls and BMS research. Gate: cross-zone effects tested; no TH1 hidden master.
7. **Publication:** supported-device matrix, security/user docs, CI, releases and Marketplace/HACS preparation.

Base-setpoint writes require calibration proof; BMS keepalive requires identified timeout and safe recovery; automatic discovery requires bounded false-positive/traffic testing.

