# Testing strategy

## Automated

- Unit: signed/sentinel scaling, every address boundary, packed wheel bytes, both candidate wheel formulas and 0.5 °C precision, capability precedence, identity and redaction.
- Transport/mock Modbus: two units share one serialized endpoint; logical grouping and hard chunk caps; assert at least one second between request starts; malformed/short/error/timeout responses; reconnect/backoff; partial failure; supported PyModbus signatures; cycles never overlap.
- Mandatory write spy: setup, snapshot reads, reconnect, diagnostics, unload and polling with an empty command queue must invoke zero writes. With queued commands, assert only the coalesced explicit writes occur before reads.
- Commands: permission tiers, latest-value-wins coalescing/expiry, no traffic before the next cycle, writes ordered before reads, read-phase success/lag/mismatch/timeout, no optimistic state/replay, one-register zone effect, no global-coil side effect.
- HA: flow/reconfigure/options/migration, 6+4 entities/devices, availability, supported-only features, translations, diagnostics and unload.
- Real PyModbus integration: `PymodbusBackend` + `GatewayTransport` (+ `WtcReadClient`) against a pymodbus TCP server on `127.0.0.1` filled with the 6+4 profile. Covers decoding, pacing, Modbus exceptions, absent unit, server loss/backoff and a server-side write spy (only read function codes may arrive). Skipped without pymodbus; never pointed at real hardware.

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) on every push to `main` and every pull request, Python 3.13:
ruff check/format and strict mypy once; pytest on a pymodbus matrix of the minimum supported release (3.8.x) and the newest release. hassfest and HACS validation run as separate jobs (HACS needs a public repository with topics and brand assets).

Local equivalent (see `AGENTS.md` §9): `uv venv -p 3.13 && uv pip install -e ".[dev]" -C editable_mode=compat`, then pytest, ruff and mypy. The compat editable mode is required because Home Assistant scans `sys.path` and fails on the default editable finder.

## Hardware

Begin read-only: inventory units/firmware/LIVE zones using the one-request-per-second baseline and small blocks; passively correlate dial/mode changes; characterize cadence only after a stable soak. Never test an oversized range directly on production hardware merely to find its failure point. Any later write requires separate user approval and recovery plan: one agreed zone, queued until the next cycle, one raw wheel step, physical/readback observation, restoration; locks/zone controls next; global/BMS tests last.

Record date, firmware, unit/zone, before/after raw values, command, delay and physical result—never credentials or unrelated full payloads.

Release gates: 24-hour mock then read-only hardware soak; outage, HA reload/restart and gateway reboot; resource budget; pytest/type/lint/hassfest/HACS. Block release on any automatic/unrequested poll-time write, unintended cross-zone write, stale-live state, unredacted diagnostics or missing translations.

