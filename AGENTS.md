# AGENTS.md — Wavin WTC-NET Home Assistant integration

Guidance for AI coding agents (Claude Code, Codex, Copilot, Cursor, etc.) working in this repository.
Read this file fully before making changes. When this file and `docs/` disagree, **`docs/` wins** — then fix this file.

## 1. Project in one paragraph

A clean-room, native async Python Home Assistant integration for Wavin WTC-NET underfloor heating/cooling
controllers over **local Modbus TCP** (no cloud, Java or MQTT). One gateway endpoint can serve several
Modbus unit IDs (controllers), each with up to 7 zones. The device is treated as **fragile and
rate-limited**, so the transport is deliberately slow and serialized, and **no write may ever happen
unless a user explicitly asked for it**.

## 2. Current status

- **Done – plan step 2, read-only protocol core:** codecs, address map, read planning, serialized
  transport, read client, poller, PyModbus adapter, in-process mock (6+4 zones) and a 24 h
  virtual-time soak suite (zero writes, ≥ 1 s between request starts, caps, no overlap, outages).
- **Done – open decision 1:** real-pymodbus integration tests against a localhost server
  (`tests/test_pymodbus_integration.py`) and GitHub Actions CI (`.github/workflows/ci.yml`,
  pymodbus 3.8.x + latest).
- **Done – decisions 2-4 in code:** static points on a slow cadence, derived minimum poll
  interval, `max_gap` 0..8, fallback split with per-connection exclusions (`docs/to-code.md`).
- **Done – plan step 3 (code):** config/options/reconfigure flows, coordinator over `GatewayPoller`,
  read-only climate/sensor/binary_sensor entities, translations (en/hu), redacted diagnostics,
  Repairs issue for excluded points, HA tests in `tests/ha`; CI (lint, pytest matrix, hassfest,
  HACS) is green.
- **Done – plan step 4 (read-only hardware validation):** first run 2026-11-07, see
  `docs/hardware-validation-2026-11-07.md`. Open decisions 1-9 are decided. The wheel rule is
  closed: comfort `neutral + (code - 6) x step` with configurable heat/cool neutral and step
  (22/26 °C, 0.5/0.5 here), economy ignores the wheel. Cooling is accepted by rule, not measured.
- **Done – plan step 5 (code, mock/loopback tested, not hardware tested):** wheel comfort-target
  commands (heating and, by decision 11, cooling), permission tiers (write mode off by default + per-zone opt-in), queue
  with latest-value-wins, expiry and reconnect guard, flush before the next cycle's reads,
  read-back verification (2 cycles, never rewritten). Details: `docs/open-decisions.md` decision 10.
- **Hardware write test passed** on Dolgozó (unit 2 zone 2, one step 24 → 23.5 → 24 °C, restored):
  `docs/hardware-write-test-2026-11-07.md`. Other zones are untested; each new zone or kind of
  write needs its own approval.
- Not yet done: plan step 1 evidence freeze, a cooling-season read-only run, optional lock writes,
  plan step 6 and later.
- Do not start later plan steps (writes, global controls) before the earlier gates pass.

## 3. Source of truth (`docs/`)

| File | Use it for |
|---|---|
| `docs/README.md` | Core decisions, main uncertainty, first milestone |
| `docs/01-source-analysis.md` | Why things are designed this way; defects of the Python reference and HomeGate |
| `docs/02-protocol-specification.md` | Register/coil map, encodings, confidence levels, safety contract |
| `docs/03-functional-requirements.md` | FR-01…FR-13, non-functional rules, MVP acceptance |
| `docs/04-architecture.md` | Module layout, cycle and command flow, identity, polling defaults |
| `docs/05-configuration.md` | Config entry model, calibration formula, setup flow, real installation profile |
| `docs/06-implementation-plan.md` | Milestones and their gates |
| `docs/07-testing-strategy.md` | Required automated tests, hardware test rules, release gates |
| `docs/vendor-bms-v1_9.md` | Vendor BMS interface facts (first vendor evidence; re-grade confidence from it) |
| `docs/open-decisions.md` | Open maintainer decisions after milestone 1; work through in order |
| `docs/to-code.md` | Coding tasks already decided but not yet implemented |

Cite the relevant doc and section in PR descriptions when you implement or change a behaviour.

## 4. Non-negotiable safety rules

Breaking any of these blocks the change, even when the tests pass.

1. **No automatic writes.** Setup, validation, discovery, refresh/polling, reconnect, reload,
   diagnostics and unload make **zero** Modbus writes. A polling cycle may write **only** explicit
   user commands that are already queued, and never generates corrective or configuration writes.
   (The Python reference rewrote setpoints on every poll. Never reproduce that.)
2. **Write mode is off by default.** Zone permissions and controller-global permissions are separate
   opt-ins. MVP allows only validated wheel writes and, optionally, lock writes.
3. **Commands are queued, not sent.** Validate → map to exactly one raw code → enqueue keyed by
   `(unit, zone, command_kind)` with latest-value-wins → flush at the **start of the next scheduled
   cycle**, before the reads → confirm from that cycle's reads. No immediate transaction, no focused
   re-read, no out-of-cycle traffic.
4. **No optimistic state.** Report queued / sent / confirmed / superseded / timeout / rejected /
   transport error. Entity state changes only after read-back confirmation.
5. **No replay.** Commands carry an expiry/generation token. Nothing is resent after reconnect,
   reload or a newer command.
6. **No hidden side effects.** A zone action never writes global coils 4127–4129. TH1 has **no**
   hidden master behaviour.
7. **No BMS keepalive** and no base-setpoint enforcement. Timeout and reset semantics are unknown.
8. **Never silently round.** A requested target must map exactly, within tolerance, to a valid raw
   code. Preserve the 0.5 °C resolution. If mode/preset is unknown, the target is *unavailable*,
   not guessed.
9. **Physical dial changes win.** Polling must never undo a change made on the thermostat.

## 5. Transport rules

- One config entry = one TCP endpoint = **one** async client and **one** serialized queue shared by
  all unit IDs on that endpoint.
- **At most one Modbus request starts per second** per endpoint (HomeGate baseline). Do not use the
  Python reference's 50 ms gaps.
- Group addresses into logical spans, then split each span by **hard caps: 15 holding registers / 16
  coils per request**. Never issue HomeGate's 480-coil-sized reads.
- Raise caps or the rate **only** with hardware soak evidence, never adaptively at runtime. These are
  advanced options with conservative bounds and warnings.
- Cycles never overlap. If a tick arrives while a cycle is still running, skip or coalesce it.
- Bounded timeouts (default 5 s) and bounded exponential reconnect backoff. A failed unit becomes
  *unavailable*, never stale-live. Other units keep working.
- A failed write does not abort unrelated reads unless the connection itself is unusable.
- Default poll interval 60 s, configurable 30–300 s, minimum derived from the plan size
  (`ceil(requests × spacing × 1.5)`). Static data (firmware/serial) is read after (re)connect, then hourly.

## 6. Protocol cheat-sheet

Addresses use offset 0 (an advanced offset exists for diagnostics only). Holding registers and coils
are **separate address spaces**: e.g. register 4604 and coil 4604 are different things.
"both" = corroborated by both sources; "ref-only" = only the Python reference listed it. Vendor
document and first hardware run add evidence: see `docs/02-protocol-specification.md` "Evidence status".
The code `Confidence` enum is unchanged. Never build write paths on addresses without an approved
hardware test, whatever their label.

**Registers**
- 4121 water temp, signed ×0.1 °C (both) · 4135 mode 0 init / 1 heat / 2 cool / 3 changeover (both)
- 4127 RH ×0.1 % · 4128 room temp · 4132 PI output · 4133 dew point · 4134 program/DIP ·
  4500 firmware · 4501–4502 serial (all ref-only)
- `0xFFFF` / `0xFFFE` = invalid sentinel.
- Zone `z` (1…7): `B = 4136 + 6·(z−1)`. B+0 temp, B+1 RH, B+2 dew point (both); B+3 wheel offset,
  B+4 dew-point resistance, B+5 illuminance (ref-only).
- Setpoints (vendor-documented, hw-read; **read-only for us**): `4178 + 3·(z−1)` comfort cool / +1 comfort heat / +2 economy
  heat; `4700 + (z−1)` economy cool. The reference's ±1 °C compensation is **not** protocol truth.
- Wheel: 4600–4603 packed read-only codes (ref-only); `4604 + (z−1)` writable wheel code (both).

**Coils**
- Global: 4127 on/off, 4128 comfort/economy, 4129 heat/cool (both). Outputs/faults 4120, 4121, 4125,
  4126 (ref-only).
- Zone status `S = 4130 + 8·(z−1)`: DRY, OUT/valve, condensation, DWP input, on/off, comfort/economy,
  heat/cool, live. Offsets 0–3, 6 and 7 are corroborated; 4–5 are ref-only.
- Zone writable `C = 4604 + 8·(z−1)`: on/off, comfort/economy (both), heat/cool (ref-only).
- Lock command `4656 + (z−1)`. Candidate delayed lock status `4186 + (z−1)`. Lock status lags, so
  verify it with a delay.

**Wheel → target** (configurable, never hard-coded):

```
comfort target = neutral(heat|cool) + (raw_code − 6) × step(heat|cool)
```

Closed 2026-11-07 (hardware dial checks, heating; cooling by maintainer rule): codes 0…12, centre 6,
`neutral_heat`/`neutral_cool`/`wheel_step_heat`/`wheel_step_cool` are options (real install 22/26 °C,
0.5/0.5). Economy ignores the wheel (heating target = economy setpoint register; economy cooling
unavailable). The Python reference's 1 °C per code is rejected here, kept testable in code only.

## 7. Architecture and code layout

Package layout (HA custom component, HACS-style). Modules marked ✓ exist:

```
custom_components/wavin_wtc_net/
  errors.py            ✓ exception hierarchy (transport / Modbus exception / malformed / rejected)
  protocol.py          ✓ PURE: addresses, confidence, codecs, wheel math, read planning, decoding
  models.py            ✓ typed immutable snapshots (frozen dataclasses)
  transport.py         ✓ backend protocol, serialized queue, 1 req/s limiter, caps, timeouts, backoff
  pymodbus_backend.py  ✓ PyModbus adapter (retries=0, no auto-reconnect, slave=/device_id=)
  client.py            ✓ read-only snapshot client (per-unit failure isolation, optional reads)
  poller.py            ✓ non-overlapping cycle driver; commands phase will be added before reads
  runtime.py, unit_spec.py ✓  HA-free helpers (client factory + probe, "1:6, 2:4" parser)
  coordinator.py, config_flow.py, entity.py, climate.py, sensor.py, binary_sensor.py,
  diagnostics.py, strings.json, translations/{en,hu}.json ✓  (read-only HA layer)
  polarity.py ✓   fixed bit polarity (single source for core and HA layer)
  commands.py ✓   permissions, validation, coalescing queue, expiry, read-back verification
  write_transport.py, write_backend.py ✓  the ONLY write route: one wheel register (4604..4610,
                  codes 0..12, FC06), a transport subclass built only when write mode is on
  # coordinator command phase: poller hook before_reads (send queue) → reads → on_snapshot (verify)
tests/
```

```
GatewayRuntime(host, port)
  ├─ GatewayTransport (one client/queue)
  ├─ Controller(unit=1) ─ Zone 1..N
  └─ Controller(unit=2) ─ Zone 1..N
```

- `protocol.py`, `models.py` and `transport.py` must not import `homeassistant`. They must be
  testable in plain pytest.
- Reads and writes are **separate APIs**. The read path has no code route to a write function.
- Controller and zone are separate devices; zones use `via_device`.
- **Stable identity:** validated serial+unit when it is proven reliable. Until then, a generated and
  persisted gateway ID + unit + zone. **Never rely on `entry_id`.**
- Use the current HA APIs (config-entry runtime data, `DataUpdateCoordinator`, entity descriptions,
  diagnostics, Repairs, translations). Check current HA developer docs instead of copying patterns
  from the old Python reference.

## 8. Coding conventions

- **Language:** chat with the maintainer is in **Hungarian**. Everything committed (code, comments,
  docstrings, docs, commit messages, PR descriptions, issues) is in **English**. Hungarian appears in
  the repo only in `translations/hu.json` and in the real zone names used as test fixtures.

- Python, fully async, type-hinted (strict mypy target), formatted and linted with ruff.
- Immutable typed snapshots; no shared mutable state between cycles.
- Errors: normalize transport, Modbus-exception and malformed-response errors into the integration's
  own exception types. Track last success, latency and failure count.
- Logging: no secrets, no full Modbus frames in normal logs. Raw diagnostics are disabled by default.
- User-facing strings: English primary, Hungarian translation required for every key.
- Keep docs in sync: if behaviour diverges from `docs/`, update the doc in the same change.

## 9. Testing requirements

Run before proposing any change (all must be clean). Set up once with
`uv venv -p 3.13 && uv pip install -e ".[dev]"`, then `source .venv/bin/activate`:

```
python -m pytest            # async tests run under pytest-asyncio (auto mode) from the HA test plugin
ruff check . && ruff format --check .
mypy                        # strict, custom_components only
```

Later also hassfest and HACS validation in CI. Test doubles live in `tests/mock_wtc.py`
(`VirtualClock`, `MockWtcBackend` with fault injection, write spy and the real 6+4 profile).
Use the virtual clock for anything time-related. Tests must never sleep for real, except
sub-0.1 s request timeouts.

Mandatory test categories (see `docs/07-testing-strategy.md`):
- **Write spy:** setup, snapshot reads, reconnect, diagnostics, unload and polling with an empty queue
  → **zero** write calls. With queued commands → only the coalesced explicit writes, all before the reads.
- **Rate/cap:** ≥ 1 s between request starts per endpoint. No request exceeds 15 registers / 16 coils.
  Two units share one queue. Cycles never overlap.
- **Codecs:** signed scaling, sentinels, every address boundary for zones 1…7, packed wheel bytes,
  both wheel formulas, 0.5 °C round-trip.
- **Faults:** malformed, short, exception and timeout responses; reconnect/backoff; partial unit failure.
- **Commands:** permission tiers, latest-value-wins, expiry, no traffic before the next cycle, no
  optimistic state, no replay, single-zone effect, no global-coil side effect.
- **HA:** flows, 6+4 → ten stable climate devices, availability, translations, redacted diagnostics.

The reference mock topology is two units with 6 + 4 zones (matches the real installation).

## 10. Hardware and the real installation

Real profile (a test fixture, **not** a universal default): endpoint `192.168.1.210:502`.
Unit 1: Konyha, Nappali, Balu, Gábor, Luca, Fürdő. Unit 2: Garázs, Dolgozó, Háló, Zuhany.
Neutral 22 °C heating / 26 °C cooling. Wheel step 0.5 °C.

- **Agents must never connect to real hardware directly.** No ad-hoc scripts, pymodbus REPL sessions,
  `modpoll`-style probes or one-off reads/writes against a live endpoint, read-only ones included.
- **Integration test runs may reach the real endpoint.** The agent may start the integration itself
  (e.g. a Home Assistant dev/test instance or an explicit hardware test suite), so all traffic goes
  through the integration's own transport, rate limiter, block caps and safety rules. Conditions:
  - write mode stays **off** unless the human maintainer explicitly approved a specific write test;
  - use the conservative transport defaults (1 req/s, 15/16 caps), with no tuned or oversized settings;
  - only one test run against the endpoint at a time, and stop it when the test is done;
  - report what ran, for how long, and any errors or anomalies.
- Real-hardware testing starts read-only at 1 req/s with small blocks. Never test oversized ranges on
  production hardware to find a failure point.
- Any hardware write needs separate human approval and a recovery plan: one agreed zone, one raw wheel
  step, observation, restoration.

## 11. Explicitly out of scope (MVP)

Automatic seasonal/mode switching, BMS keepalive, program/base-setpoint editing, heat-pump
optimization, inferred energy, unverified thermostat models, automatic discovery without bounded
traffic testing, and any write built on ref-only addresses.

## 12. Clean-room rule

The Python reference (`wavin_wtc3`) and HomeGate (Java) are **evidence, not templates**. Do not copy
their code. Re-derive behaviour from `docs/`. Do not reintroduce their known defects: poll-time
setpoint rewrites, hard-coded 25/24/17/24 °C centers, integer-rounded targets, TH1 global side
effects, `entry_id`-based identity, per-entry clients on a shared endpoint, swallowed write errors,
immediate MQTT-style writes.

## 13. Open questions (do not resolve by guessing)

- Wheel scale is closed for this installation (see §6); other thermostat models/installations may differ, hence the options.
- Economy cooling target (not covered by the comfort rule).
- Setpoint ±1 °C compensation; 4178+/4700+ setpoint validity.
- Packed wheel words 4600–4603 vs B+3 offset vs physical dial correlation.
- Firmware/serial registers, so serial-based identity is not yet allowed.
- Lock delay and authoritative lock status address.
- Global vs zone bit precedence. Decision 10: master bits are ignored for the wheel command (real install runs zones in comfort while the master bit is economy); only a zone's own status vs control bit disagreement blocks a target write.
- BMS timeout, reset semantics and safe value.
- Unit-ID semantics on real hardware (keys 1/2 → Modbus unit IDs is evidenced only in HomeGate code).

If a task depends on one of these, implement it as configurable/testable, keep writes disabled, and say so in the PR.
