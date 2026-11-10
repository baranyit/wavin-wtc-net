# AGENTS.md — Wavin WTC-NET Home Assistant integration

Guidance for AI coding agents (Claude Code, Codex, Copilot, Cursor, etc.) maintaining this repository.
Read it fully before making changes.

## 1. The project

A native async Python Home Assistant integration for Wavin WTC-NET underfloor heating/cooling
controllers over **local Modbus TCP** (no cloud, no bridge, no MQTT). One gateway endpoint serves
several Modbus unit IDs (controllers), each with up to 7 zones. The device is treated as **fragile
and rate-limited**: the transport is deliberately slow and serialized, and **nothing is ever written
unless a user explicitly asked for it**.

The project is in **maintenance mode**. The feature set is complete for the MVP; work is bug fixes,
Home Assistant / pymodbus compatibility, small improvements and support for new installations.
There is no separate design documentation: **the code, its docstrings and the tests are the
documentation**, together with `README.md` (user-facing) and this file (rules). When you change a
behaviour, update the docstrings, the tests, the README and, if a rule changes, this file in the
same change.

## 2. Non-negotiable safety rules

Breaking any of these blocks the change, even when the tests pass.

1. **No automatic writes.** Setup, validation, discovery, refresh/polling, reconnect, reload,
   diagnostics and unload make **zero** Modbus writes. A cycle never generates corrective,
   keepalive or configuration writes; only explicit user commands are written.
2. **Write mode is off by default.** Writing has a master switch plus separate per-zone opt-ins
   per kind (target, lock, on/off + preset). Controller-global controls are never written.
3. **Commands are debounced, then written once.** Validate → map to exactly one raw code → enqueue
   keyed by `(unit, zone, command_kind)` with latest-value-wins → one endpoint-wide 2 s debounce
   (restarted by every command, flushed after 10 s at the latest) → one write per command through
   the shared lock and 1 req/s limiter, in a fixed order → confirmed by the next cycle that started
   after the write. **No read is triggered by a write**, and unwritten commands are dropped on unload.
4. **Optimistic display, read-decided state.** Entities show the requested value as an overlay (the
   snapshot is never altered); the real value returns on error, timeout, supersede, unload or at the
   first post-write cycle. Command states: queued / sent / confirmed / superseded / timeout /
   rejected / transport error.
5. **No replay.** Commands carry an expiry/generation token. Nothing is resent after reconnect,
   reload or a newer command.
6. **No hidden side effects.** A zone action never writes the global coils 4127–4129.
7. **No BMS keepalive** and no base-setpoint enforcement. Timeout and reset semantics are unknown.
8. **Never silently round.** A requested target must map exactly (within tolerance) to a valid raw
   code. Preserve the 0.5 °C resolution. If mode/preset is unknown, the target is *unavailable*.
9. **The physical dial wins.** Polling must never undo a change made on the thermostat.

## 3. Transport rules

- One config entry = one TCP endpoint = **one** async client and **one** serialized queue shared by
  all unit IDs on that endpoint.
- **At most one Modbus request starts per second** per endpoint.
- Group addresses into logical spans, then split by **hard caps: 15 holding registers / 16 coils per
  request**. Never issue large block reads.
- Raise caps or the rate **only** with hardware soak evidence, never adaptively at runtime.
- Cycles never overlap; a tick during a running cycle is skipped or coalesced.
- Bounded timeouts (default 5 s) and bounded exponential reconnect backoff. A failed unit becomes
  *unavailable*, never stale-live; other units keep working. A failed write does not abort
  unrelated reads unless the connection itself is unusable.
- Poll interval default 60 s, 30–300 s, minimum derived from the plan size
  (`ceil(requests × spacing × 1.5)`). Static data (firmware/serial) after (re)connect, then hourly.

## 4. Protocol cheat-sheet

The authoritative map is `protocol.py` (addresses, codecs, `Confidence`) and `polarity.py` (bit
polarity). Addresses use offset 0. Holding registers and coils are **separate address spaces**.
`BOTH` = corroborated by independent sources or hardware; `REFERENCE_ONLY` = single source. Never
build a write path on an address without an approved hardware test, whatever its label.

**Registers**
- 4121 water temp, signed ×0.1 °C · 4135 mode 0 init / 1 heat / 2 cool / 3 changeover
- 4127 RH ×0.1 % · 4128 room temp · 4132 PI output · 4133 dew point · 4134 program/DIP ·
  4500 firmware · 4501–4502 serial
- `0xFFFF` / `0xFFFE` = invalid sentinel.
- Zone `z` (1…7): `B = 4136 + 6·(z−1)`: B+0 temp, B+1 RH, B+2 dew point, B+3 wheel offset,
  B+4 dew-point resistance, B+5 illuminance.
- Setpoints (**read-only**): `4178 + 3·(z−1)` comfort cool / +1 comfort heat / +2 economy heat;
  `4700 + (z−1)` economy cool.
- Wheel: 4600–4603 packed read-only codes; `4604 + (z−1)` writable wheel code 0…12 (FC06).

**Coils**
- Global (never written): 4127 on/off, 4128 comfort/economy, 4129 heat/cool. Outputs/faults 4120,
  4121, 4125, 4126.
- Zone status `S = 4130 + 8·(z−1)`: DRY, OUT/valve, condensation, DWP input, on/off,
  comfort/economy, heat/cool, live.
- Zone writable `C = 4604 + 8·(z−1)`: offset 0 on/off, 1 comfort/economy (FC05); offset 2
  heat/cool is never written.
- Lock command `4656 + (z−1)` (FC05); lock status `4186 + (z−1)` lags the command.

**Wheel → target** (options, never hard-coded):

```
comfort target = neutral(heat|cool) + (raw_code − 6) × step(heat|cool)
```

Codes 0…12, centre 6. Validated for heating (real install 22 °C / 0.5 °C); cooling (26 °C / 0.5 °C)
follows the same rule but is not hardware-validated. Economy ignores the wheel: the heating target
is the economy setpoint register; the economy cooling target is unavailable.

## 5. Architecture

```
custom_components/wavin_wtc_net/
  errors.py            exception hierarchy (transport / Modbus exception / malformed / rejected)
  protocol.py          PURE: addresses, confidence, codecs, wheel math, read planning, decoding
  models.py            immutable typed snapshots (frozen dataclasses)
  polarity.py          fixed bit polarity (single source for core and HA layer)
  transport.py         backend protocol, serialized queue, 1 req/s limiter, caps, timeouts, backoff
  pymodbus_backend.py  PyModbus adapter (retries=0, no auto-reconnect)
  client.py            read-only snapshot client (per-unit failure isolation, fallback split)
  poller.py            non-overlapping cycle driver
  commands.py          permissions, validation, coalescing queue, debounce, overlay, verification
  write_transport.py, write_backend.py   the ONLY write route, built only when write mode is on
  runtime.py, unit_spec.py, calibration.py   HA-free helpers
  coordinator.py, config_flow.py, entity.py, climate.py, sensor.py, binary_sensor.py, lock.py,
  diagnostics.py, strings.json, translations/{en,hu}.json   HA layer
  brand/               icon/logo PNGs (light and dark); SVG masters in assets/brand/
```

- `protocol.py`, `models.py`, `polarity.py` and `transport.py` must not import `homeassistant`.
- Reads and writes are **separate APIs**. The read path has no code route to a write function.
- Controller and zone are separate devices; zones use `via_device`.
- **Identity:** generated, persisted gateway ID + unit + zone. Never `entry_id`; the serial is not
  used for identity until proven reliable. Changing identity is a breaking change.
- Use current HA APIs (runtime data, `DataUpdateCoordinator`, entity descriptions, diagnostics,
  Repairs, translations); check the current HA developer docs when HA deprecates something.

## 6. Coding conventions

- **Language:** chat with the maintainer is in **Hungarian**. Everything committed (code, comments,
  docstrings, README, commit messages, PR descriptions, issues) is in **English**. Hungarian appears
  only in `translations/hu.json` and in the real zone names used as test fixtures.
- Fully async, type-hinted (strict mypy), formatted and linted with ruff.
- Immutable snapshots; no shared mutable state between cycles.
- Normalize transport, Modbus-exception and malformed-response errors into the integration's own
  exception types. Never swallow write errors.
- Logging: no secrets, no full Modbus frames in normal logs. Raw diagnostics stay redacted.
- Every user-facing string in English and Hungarian.
- Self-contained: do not reference external projects or removed documents in code or comments.
  Explain the "why" in the docstring next to the code.

## 7. Testing

Set up once with `uv venv -p 3.13 && uv pip install -e ".[dev]"`, then `source .venv/bin/activate`.
All must be clean before proposing a change:

```
python -m pytest
ruff check . && ruff format --check .
mypy
```

CI runs these (`ci.yml`, pymodbus 3.8.x and latest) plus `hassfest.yml` and `hacs.yml`.
Test doubles live in `tests/mock_wtc.py` (`VirtualClock`, `MockWtcBackend` with fault injection,
write spy, the 6+4 reference topology). Use the virtual clock for anything time-related; tests never
sleep for real, except sub-0.1 s request timeouts.

Every change keeps these categories covered:
- **Write spy:** setup, reads, reconnect, diagnostics, unload, polling with an empty queue → zero
  writes; with commands → only the coalesced explicit writes, none before the debounce ends.
- **Rate/cap:** ≥ 1 s between request starts; ≤ 15 registers / 16 coils; one shared queue; no overlap.
- **Codecs:** signed scaling, sentinels, address boundaries for zones 1…7, packed wheel bytes,
  0.5 °C round-trip.
- **Faults:** malformed, short, exception and timeout responses; reconnect/backoff; partial failure.
- **Commands:** permissions, latest-value-wins, expiry, debounce, overlay, no replay, single-zone
  effect, no global-coil side effect.
- **HA:** flows, 6+4 → ten stable climate devices, availability, translations, redacted diagnostics.

A bug fix comes with a test that fails without it.

## 8. Hardware

Real installation (a test fixture, **not** a default): endpoint `192.168.1.210:502`.
Unit 1: Konyha, Nappali, Balu, Gábor, Luca, Fürdő. Unit 2: Garázs, Dolgozó, Háló, Zuhany.
Neutral 22 °C heating / 26 °C cooling, wheel step 0.5 °C. Hardware-validated so far: reads, and
target/lock/on-off/preset writes on Dolgozó (unit 2 zone 2) only. Cooling is not validated.

- **Never connect to real hardware directly**: no ad-hoc scripts, pymodbus REPL, `modpoll`-style
  probes or one-off reads/writes, read-only ones included.
- Hardware runs go only through the integration itself (`tests/hardware`, opt-in markers
  `hardware` / `hardware_write`, or an HA dev instance), with conservative defaults, one run at a
  time, stopped when done, and a report of what ran, how long, and any errors.
- Write mode stays **off** unless the maintainer approved a specific write test: one agreed zone,
  one raw step, observation, restoration. Each new zone or kind of write needs its own approval.
- Never probe oversized ranges on hardware to find a failure point.

## 9. Out of scope

Automatic seasonal/mode switching, BMS keepalive, program/setpoint editing, global controls,
heat-pump optimization, inferred energy, unverified thermostat models, automatic discovery, and any
write built on an address without a hardware test. Known pitfalls never to reintroduce: setpoint
rewrites on every poll, hard-coded wheel centres, integer-rounded targets, hidden global side
effects, `entry_id`-based identity, one client per entry on a shared endpoint, swallowed write
errors, immediate unbatched writes.

## 10. Open questions (do not resolve by guessing)

- Wheel scale on other thermostat models/installations (hence the options).
- Economy cooling target; cooling in general until a cooling-season run.
- Validity of the setpoint registers 4178+/4700+ beyond reading.
- Packed wheel words 4600–4603 vs the B+3 offset vs the physical dial.
- Serial number reliability for identity.
- Global vs zone bit precedence: master bits are ignored for zone commands; only a zone's own status
  vs control bit disagreement blocks a write.
- BMS timeout, reset semantics; persistence of writes over a power cycle.

If a task depends on one of these, make it configurable and testable, keep writes disabled, and say
so in the PR.

## 11. Versioning and releases

Semantic versioning. **Every merge to `main` raises the version**; the agent picks the level:

- **Major:** breaking (removed/renamed options, entities or services, changed `unique_id`/identity,
  changed meaning of state users rely on, irreversible migration). While on `0.x`, breaking raises
  **minor**; `1.0.0` is the maintainer's decision.
- **Minor:** backward-compatible feature or behaviour change.
- **Patch:** bug fix, or no user-visible behaviour change (docs, tests, CI, refactoring, branding).

In the same PR as the change: raise `version` in `manifest.json` and `pyproject.toml` and in the
README status line; add a CHANGELOG section on top (`## X.Y.Z (unreleased, beta)`); state the level
and reason in the PR description. `tests/test_version.py` enforces agreement. The highest level wins.

Tags, GitHub releases and HACS default-list submission are the maintainer's explicit step. Before a
release: CI green on `main` (CI, Hassfest, HACS validation), CHANGELOG heading dated, one read-only
hardware run on the release commit, then tag `v<version>` and a GitHub (pre-)release.
