# Changelog

## 0.1.0 (unreleased, beta)

First public version.

- Local Modbus TCP polling of several controllers behind one gateway; serialized, one request per
  second, hard request caps, bounded timeouts and backoff; static values (firmware, serial) read
  after a connect, then hourly.
- Per-zone climate, sensors and binary sensors, per-controller sensors, diagnostics with redaction
  and a Repairs issue for excluded points. English and Hungarian translations.
- Wheel calibration options (heating and cooling neutral and step) and a derived target
  temperature sensor.
- Optional, off-by-default writing with per-zone opt-ins: comfort target (wheel register), lock
  (lock coil), zone on/off and comfort/eco (control coils). Queue with latest-value-wins, one 2 s
  debounce for all zones (restarted by each command, 10 s at most), optimistic display that falls
  back to the real value, confirmation by the next poll (no extra reads), expiry and no replay.
- Validated on one real installation (read) and one zone (write): see `docs/supported-devices.md`.
- Not included: global controls, BMS keepalive, setpoint editing, economy targets, discovery.
