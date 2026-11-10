# Changelog

## 0.2.1 (unreleased, beta)

Patch: no change in behaviour, options, entities or identity.

- New brand images (icon, logo and dark variants) for Home Assistant and HACS, and a README header.
- The `docs/` folder is removed: the code and its tests are the documentation. The validation
  summary moved into the README.
- Hassfest and HACS validation run as their own workflows, with status badges in the README.
- Packaging: only `custom_components.wavin_wtc_net` is built as a package (fixes the editable
  install next to `assets/`).
- Removed unused code: the transport address offset (never reachable from the options) and the
  fixed wheel scale constants (the scale comes from the options).

## 0.2.0 (unreleased, beta)

Minor: a feature change, not breaking (no option, entity or identity changes).

- Writes no longer wait for the next poll cycle: one 2 s debounce for all zones (restarted by
  every command, 10 s at most), then one request per command. No read is triggered by a write; the
  next poll confirms it.
- Optimistic display: the climate target, on/off, preset and the lock show the requested value at
  once and fall back to the real value on error, timeout, supersede, unload or, for a target, at the
  first poll after the write.
- Hardware-checked on one zone.

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
  (lock coil), zone on/off and comfort/eco (control coils). Queue with latest-value-wins, flush at
  the start of the next poll before the reads, read-back confirmation, expiry and no replay.
- Validated on one real installation (read) and one zone (write).
- Not included: global controls, BMS keepalive, setpoint editing, economy targets, discovery.
