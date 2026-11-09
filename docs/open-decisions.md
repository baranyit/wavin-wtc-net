# Open decisions after milestone 1 (read-only protocol core)

Status: open. Work through these in order. Each item needs a maintainer decision or action before
the related code changes. Record the outcome under **Decision** and update `AGENTS.md`/the
relevant spec doc in the same change.

Numbers below come from the current read planner (`max_gap=8`, 15-register / 16-coil caps) for the
real profile (unit 1: 6 zones, unit 2: 4 zones).

---

## 1. Test the PyModbus adapter with the real library (Python 3.13)

**Context.** `pymodbus_backend.py` is tested only against a fake `pymodbus` module
(`tests/test_pymodbus_backend.py`, both `slave=` and `device_id=` signatures). The build
environment had no network, so the real library and Python 3.13 could not be installed.

**Options.**
- A. Local run on the maintainer's Mac: `uv venv -p 3.13 && uv pip install -e ".[dev]" && pytest`.
- B. GitHub Actions CI: pytest + ruff + mypy on Python 3.13 with real pymodbus. Optional matrix:
  minimum supported (3.8.x) and latest pymodbus.
- C. Both A and B.

In every option, add an integration test that runs `PymodbusBackend` + `GatewayTransport` against a
local pymodbus Modbus TCP server on localhost. Skip the test when pymodbus is not installed. Never
point it at real hardware.

**Recommendation.** C. A gives fast feedback now. B prevents regressions and is needed later for
hassfest/HACS anyway.

**Maintainer input needed.** Choose A/B/C. If B: confirm the GitHub remote and the pymodbus version
matrix.

**Decision.** C (both), decided 2026-11-06.
- Local: Python 3.13 venv via `uv` on the maintainer's Mac; full suite green with pymodbus 3.16.2 and 3.8.6.
- CI: `.github/workflows/ci.yml` (Python 3.13; ruff + mypy; pytest matrix pymodbus 3.8.x and latest)
  on `baranyit/wavin-wtc-net`.
- New `tests/test_pymodbus_integration.py`: real client against a localhost pymodbus server. Finding:
  pymodbus' server data store shifts protocol addresses by +1, so the test server compensates; the
  adapter itself sends protocol addresses unchanged (offset 0). Absent-unit behaviour differs by
  pymodbus server version (exception vs. silence), so the test accepts both and asserts recovery.

---

## 2. Cycle length and request count

**Context.** One cycle is 29 requests (16 for unit 1 + 13 for unit 2), so about 29 s at 1 req/s.
This fits the 60 s default poll interval. The spec allows a minimum of 30 s, which would leave
almost no slack. Overrunning ticks are skipped, never queued, so it stays safe, but the effective
interval becomes irregular. Firmware/serial (4500–4502) are static, yet they are read every cycle
(1 request per unit).

**Options.**
- A. Keep everything as is.
- B. Read static data (firmware, serial) only on the first cycle after (re)connect, then hourly.
  This gives 27 requests per cycle.
- C. Also move slow-changing reference-only data (setpoints 4178+, economy cool 4700+, packed wheel
  words) to a slower cadence, e.g. every 5th cycle.
- D. Raise the minimum poll interval from 30 s to 60 s, or derive the minimum from the plan size
  (requests × interval × 1.5).

**Recommendation.** B + D. C only if hardware soak shows a need.

**Maintainer input needed.** Choose. D changes `docs/04-architecture.md` (poll range 30–300 s).

**Decision.** B + D (decided 2026-11-06).
- Static data (firmware, serial 4500–4502) is read on the first cycle after (re)connect and then at
  most hourly. Steady-state cycle: 27 requests; first cycle after connect: 29.
- The minimum poll interval is derived from the *full* plan (first-cycle size):
  `min_interval = ceil(requests × request_spacing × 1.5)`, never below 30 s. For the real profile
  that is ceil(29 × 1 s × 1.5) = 44 s. The default stays 60 s, the maximum 300 s. The options flow
  must reject values below the derived minimum.
- Spec text updated in `docs/04-architecture.md` and `AGENTS.md` §5. Code (planner cadence for
  static points, derived minimum) is still to be implemented, with tests on the virtual clock.
- Option C stays deferred until hardware soak shows a need.

---

## 3. Reading through address holes (`max_gap = 8`)

**Context.** Requests bridge up to 8 unused addresses. Values from the holes are discarded. Holes
currently read:

| Request | Unused addresses read |
|---|---|
| registers 4121–4135 | 4122–4126 (documented), 4129–4131 (undocumented) |
| registers 4166–4180 (unit 1) | 4172–4177 (zone 7 block, documented, not configured) |
| registers 4600–4609 / 4600–4607 | 4603 / 4602–4603 (documented) |
| coils 4120–4135 | 4122–4124 (undocumented) |
| zone control coils 4604+8·n | offsets 3–7 of each zone block (undocumented) |

HomeGate's stable deployment evidences only the register span 4121–4135. If the device answers an
unused address with "illegal data address", every request covering that hole fails.

Request count per cycle for different gap limits: 0 → 41, 2 → 39, 4 → 36, 8 → 29.

**Options.**
- A. Keep `max_gap = 8` (fewest requests).
- B. `max_gap = 0` (no hole reads, 41 requests, about 41 s per cycle; then the 60 s interval is tight).
- C. Bridge only spans proven readable (start with HomeGate's 4121–4135), and extend the allowlist
  from the hardware read validation (plan step 4).
- D. Keep A, but make the gap a per-endpoint advanced option and verify it in the first read-only
  hardware run.

**Recommendation.** D now. Move to C if the hardware run shows illegal-address responses.

**Maintainer input needed.** Choose. Do you know of any WTC-NET register map (vendor PDF) that lists
valid ranges?

**Vendor document check (WTC3 BMS interface v1.9, see `docs/vendor-bms-v1_9.md`).** Most holes in
the table above are *documented* addresses, not unused ones:
- Registers 4122–4126 (DWP inputs), 4172–4177 (TH7 block) and 4600–4603 (packed wheel positions)
  are all documented. The vendor lists seven thermostat blocks regardless of installed zones.
- Only these bridged addresses are **undocumented**: registers 4129–4131 (3), coils 4122–4124 (3)
  and coil offsets 3–7 between zone control blocks (5 per block, e.g. 4607–4611).
- Bridging documented addresses only gives 40 requests per cycle (no real saving over gap 0).
  Bridging the undocumented holes is what makes 29.
- Evidence that the undocumented holes are readable: HomeGate's stable deployment reads register
  span 4121–4135 (includes 4129–4131), and its 480-coil reads (the vendor's own per-message
  limit is 30 registers / 480 bits) necessarily cover undocumented coil addresses. The vendor
  document itself says nothing about the response to undocumented addresses.

**Decision.** D (decided 2026-11-06, from the vendor document).
- Keep `max_gap = 8` as the default (29 requests per cycle) and make it a per-endpoint advanced
  option (0–8). Do not raise it above 8.
- The first read-only hardware run (plan step 4) must record whether any bridged read returns an
  illegal-address exception. If so, switch to C: bridge only documented addresses plus proven spans.
- A failing required request is handled by the fallback split of decision 4.
- Coding task queued in `docs/to-code.md`.

---

## 4. Reference-only addresses inside required requests

**Context.** A request is *required* if it covers at least one corroborated ("both") point. A
reference-only address in the same request can fail the whole request. A failed required request
makes the whole unit unavailable. Example: zone register blocks interleave corroborated offsets
(0–2) with reference-only offsets (3–5).

**Options.**
- A. Keep as is. A hardware failure shows up immediately as an unavailable unit with a clear error.
- B. Fallback split: when a required request gets a Modbus exception, retry in the same cycle with
  only its corroborated addresses (more requests only in the failure case, still paced at 1 req/s).
  Remember the failing optional addresses per connection so later cycles plan without them.
- C. Plan corroborated and reference-only points separately: always 49 requests per cycle (too
  slow for 60 s).

**Recommendation.** B. It keeps the normal cycle at 29 requests and degrades gracefully. Log a
Repairs/diagnostic entry naming the failing addresses.

**Maintainer input needed.** Choose.

**Vendor document check (`docs/vendor-bms-v1_9.md`).** Every point we read is vendor-documented,
including the former reference-only ones (zone offsets 3–5, setpoints 4178+/4700+, 4500–4503,
4600–4603, status coil offsets 4–6, lock status). The risk of a Modbus exception is therefore
lower than assumed, but documented is not hardware-validated (firmware variants, gateway behaviour).

**Decision.** B (decided 2026-11-07).
- When a required request gets a Modbus exception, retry in the same cycle with only its
  corroborated addresses (still paced at 1 req/s, caps unchanged). The normal cycle stays at 29
  requests.
- Remember the failing optional addresses per connection; later cycles plan without them. Forget
  them on reconnect so a transient fault does not hide data permanently.
- Emit a diagnostics entry (and Repairs issue in plan step 3) naming the failing addresses.
- If even the corroborated-only retry fails, the unit becomes unavailable as before.
- Coding task queued in `docs/to-code.md`.

---

## 5. Coil polarity (`on_off`, `comfort_economy`, `heat_cool`)

**Context.** The meaning of a set bit is not confirmed for global coils 4127–4129, zone status
offsets 4–6 or zone control offsets 0–2. Snapshots therefore use neutral names and raw booleans. The
HA climate entity (plan step 3) needs to know e.g. whether `heat_cool = True` means cooling.

**Options.**
- A. Maintainer states the current physical state (e.g. "heating, comfort, all zones on"). One
  read-only integration test run on the real endpoint correlates the bits with that state.
- B. Keep neutral in the core and add a polarity mapping in the HA layer (constants first,
  configurable only if units differ). Confirm it in plan step 4.
- C. Both: B as the structure, A as the evidence.

**Recommendation.** C. A read-only integration test run is allowed by `AGENTS.md` §10. Write mode
stays off.

**Maintainer input needed.** Current physical state of the installation (mode, preset, which zones
are on). Approval for a read-only integration test run against `192.168.1.210:502`.

**Evidence.** Two independent sources agree, so polarity is no longer unknown:
- Vendor document (`docs/vendor-bms-v1_9.md`): ON/OFF on = 1, C/E comfort = 1, H/C cooling = 1,
  lock locked = 1, LIVE connected = 1, DRY/condensation = 1.
- HomeGate (`wavin/definition/Mapper.java`, `Thermostat.java`, `Wtc.java`): `ON_OFF_MAP` true → on,
  `E_C_MAP` true → comfort / false → eco, `H_C_MAP` true → cool / false → heat. Applied to zone
  status offset 6 (cooling) and 7 (connected), writable coils 4604+8·n (on) and +1 (comfort), lock
  coils 4656+, and master coils 4127 (on), 4128 (comfort), 4129 (cooling).
- Side finding: HomeGate reads zone on/comfort from the writable coils (offsets 0 and 1 of
  4604+8·n), not from status offsets 4/5. The vendor document says those two sets mirror each
  other ("see 4134–4136"), so either works; compare both in the first hardware run.

**Decision.** B with the evidence above (decided 2026-11-07).
- Core snapshots keep neutral names and raw booleans. The HA layer owns one constants table:
  on_off True = on; comfort_economy True = comfort; heat_cool True = cool; lock True = locked;
  live True = connected. Not user-configurable.
- No separate hardware run is needed for polarity. The first read-only run (plan step 4) only
  confirms it by checking the bits against the physical state and compares status offsets 4/5
  with writable coils offsets 0/1. A mismatch reopens this decision.
- Coding task queued in `docs/to-code.md`.

## 6. Cooling polarity and cooling targets (decided 2026-11-07)

Option chosen: accept from the vendor document and HomeGate without a hardware measurement. The
installation is in heating season. Cooling polarity stays as in decision 5. The cooling target
formula stays configurable and testable, and **no cooling target write was built** at that time (superseded by decision 11). Revisit when the installation switches to cooling.

**Update 2026-11-07 (same day, maintainer):** the cooling comfort target is closed by rule. It works
exactly like heating with its own centre value: `neutral_cool + (code - 6) x wheel_step_cool`
(this installation: heat 22, cool 26, both steps 0.5). The implementation is open: `neutral_heat`,
`neutral_cool`, `wheel_step_heat` and `wheel_step_cool` are options (`WheelCalibration`). Economy
cooling target stays unavailable (not covered by the rule). Writes remain off.

## 7. Lock in the MVP (decided 2026-11-07)

Read only. The lock status (candidate 4186+) is shown, all zones are currently unlocked. Lock
writes (4656+) and the lock delay measurement come with the command layer and a separately
approved single-zone test. Authoritative lock status address stays open until then.

## 8. Confidence labels (decided 2026-11-07)

Re-graded per evidence, in the documents only: see "Evidence status" in
`docs/02-protocol-specification.md`. The code `Confidence` enum is not changed, so read fallback
and write eligibility behave as before. Open: whether a hardware-confirmed tier should be added to
the enum when the command layer needs it.

## 9. Wheel formula (decided 2026-11-07)

Three dial checks suffice for heating: comfort target = comfort setpoint + (code − 6) × 0.5 °C;
economy target = economy setpoint (wheel not applied). Cooling follows the same rule by decision 6/11 (not measured).
Details: `docs/hardware-validation-2026-11-07.md`. Open: why the vendor says nothing about the step
size, and whether other thermostat models differ.


## 10. Command layer design (decided 2026-11-07, plan step 5)

Choices made by the implementer within the maintainer's rules; revisit with hardware evidence.

- **Scope:** wheel comfort target, heating and (decision 11) cooling. Economy (wheel has no
  effect), off or not-live zones are rejected locally. Lock writes are not built.
- **One write route:** `WritableGatewayTransport.write_wheel_code` writes one register in
  4604..4610 with a value 0..12 (function code 06) and nothing else. It exists only if write mode
  is on and at least one zone is selected; otherwise the read-only transport class is used.
- **Flow (changed by decision 22):** validate against the latest snapshot (no I/O) → queue
  (latest-value-wins per unit/zone) → written after a 2 s debounce through the shared 1 request/s
  limiter → verified from the next cycle's reads, with one more cycle of grace → TIMEOUT. Never
  rewritten. (Originally: sent at the start of the next scheduled cycle, before its reads.)
- **Expiry/replay:** a command expires after 2 poll intervals and is dropped if the connection was
  lost or reconnected since it was queued.
- **Conflicting bits (AGENTS §13):** the hardware run shows the master comfort/economy bit False
  while most zones are in comfort, so a master/zone mismatch is normal and does not block. Only a
  zone's own status bit disagreeing with its control bit blocks (CONFLICTING_BITS).
- **Hardware result (2026-11-07):** the approved one-zone, one-step write test passed, see
  `docs/hardware-write-test-2026-11-07.md`: the write moves the dial position the thermostat uses
  and was restored. A second run confirmed that a physical dial turn after a write takes effect and
  polling does not undo it. Still open: persistence over a power cycle (vendor: BMS-written values
  last until power cycle).

## 11. Cooling comfort writes (decided 2026-11-07, supersedes the write part of decision 6)

The maintainer opens cooling comfort writes now, by rule and without a cooling measurement:
`neutral_cool + (code - 6) x wheel_step_cool` (this installation 26 °C, 0.5 °C), the same single
wheel register and the same queue/verify path as heating. Risk accepted: cooling polarity
(`heat_cool` True = cooling) rests on the vendor document and HomeGate, not on a live observation;
a cooling read-only run in the cooling season should still confirm it. Economy cooling stays
rejected (decision 14). `RejectReason.COOLING_UNVALIDATED` is removed.

## 12. Other zones (decided 2026-11-07)

The one-zone hardware write test (Dolgozó, unit 2 zone 2) is accepted as proof for the mechanism;
the other nine zones are not tested separately. Any zone can be enabled in the options. Docs state
that only Dolgozó was exercised on hardware.

## 13. Lock writes (decided 2026-11-07)

One-zone hardware test first (separate approval and zone choice, lock on / delayed status
observation / lock off), then a lock entity with the same queue and read-back if the status and its
delay are clear; otherwise lock stays read-only.

**Built 2026-11-07 (code, mock and loopback tested; hardware test pending):** `CommandKind.LOCK`,
separate `lock_zones` opt-in (a wheel zone is not a lock zone), single-coil write of 4656+(z-1) with
FC05 through the same queue, read-back from the status coil 4186+ with 4 verification cycles (the
lag is unknown), a `lock` entity only for lock zones, `tests/hardware/test_lock_validation.py`. The
command coil itself is not read. The lock entity shows only the polled status.

**Hardware result (2026-11-08):** the one-zone lock test passed on Dolgozó, see
`docs/hardware-lock-test-2026-11-08.md`: lock and unlock both confirmed within the writing cycle,
status address 4186+ confirmed. Lock writes are accepted for the MVP.

Observed on the thermostat (second run, 2026-11-08): the lock symbol appears and its own
controls are blocked while locked; unlocking restores them.

## 14. Economy (decided 2026-11-07)

Stays read-only. The economy heating target is shown, the integration does not set it.
Economy setpoint writes (4178+3(z-1)+2) and economy cooling are not built.

## 15. Global controls and BMS keepalive (decided 2026-11-07)

Out of the MVP. Global coils 4127-4129 and any keepalive are not written; they stay readable
(global bits appear in diagnostics). A later decision, with its own permission tier and a
system-wide hardware test, can reopen them (cooling season for 4129).

## 16. Zone on/off and comfort/economy (decided 2026-11-07, plan step 6 first half)

Built: `CommandKind.ZONE_POWER` and `ZONE_PRESET` write the zone control coils at offsets 0 (on/off)
and 1 (comfort/economy) of 4604+8(z-1) through the same queue and read-back (status coils 4 and 5,
3 verification cycles), under a separate `mode_zones` opt-in. The zone heat/cool coil (offset 2)
and all global coils are not writable by the transport (the vendor says the per-zone heat/cool has
no effect in slave mode). HA: climate `turn_on` / `turn_off`, `set_hvac_mode` (off, or the
current mode while off, never a heat/cool change) and `set_preset_mode` (comfort/eco) for mode
zones. The effect may depend on the Configurator settings (vendor), so a one-zone hardware test
(`tests/hardware/test_mode_validation.py`, per kind, separate approval) is still required before
the feature counts as validated.

**Hardware result (2026-11-08):** both kinds passed on Dolgozó, see
`docs/hardware-mode-test-2026-11-08.md`: change and restore confirmed within the writing cycle,
nothing else changed. Zone on/off and preset writes are validated for this installation.

## 17. Identity (decided 2026-11-07)

Stays the generated gateway ID + unit + zone. The serial (4501-4502) is shown in device info and
diagnostics only; it is not used for identification.

## 18. Publication (decided 2026-11-07)

Not yet. First the remaining open points (zone mode hardware test, cooling-season read-only run,
termostat behaviour while locked); then plan step 7 (README, supported-device matrix, release).

## 19. Cooling (decided 2026-11-08)

Not handled now. The cooling polarity check and any cooling hardware test are postponed to the
cooling season (summer). Until then cooling stays as in decision 11: enabled by rule, simulator
tested, documented as experimental and not validated on hardware.

## 20. Power cycle persistence (decided 2026-11-08)

Not tested. If written values turn out not to survive a controller power cycle, handle it as a bug
then (no preventive keepalive or re-write; AGENTS safety rule 5 and 7 stay).

## 21. Publication documents (decided 2026-11-08)

Prepared but not executed: `README.md`, `CHANGELOG.md`, `docs/supported-devices.md`,
`docs/release-checklist.md`. No tag, no GitHub release, no HACS submission yet. The maintainer
triggers publication explicitly.

## 22. Debounced immediate writes with an optimistic overlay (decided 2026-11-08)

Writes are rare and user-initiated, so waiting up to a whole poll interval was needless latency.
This **changes** AGENTS safety rules 3 and 4 and decision 10's flow. Decided by the maintainer:

- **Debounce.** One quiet period per endpoint (not per zone): every new command restarts a 2 s
  timer; when it expires the whole queue is written in a fixed order (unit, zone, kind), one request
  per command, through the same transport lock and 1 request/s limiter. An automation that sets all
  thermostats therefore produces one burst after its last command. A command waiting longer than
  10 s (steady stream, e.g. a dragged slider) is flushed anyway. Latest-value-wins is unchanged.
- **No forced read-back.** Nothing is read because of a write. The next scheduled cycle verifies
  (wheel 2, mode 3, lock 4 cycles, then TIMEOUT, never rewritten). The write may land between two
  requests of a running cycle; a cycle that was already running at write time is ignored for that
  command because its reads may predate the write.
- **Optimistic overlay.** From submit until the command is final the entity shows the requested
  value (climate target / on-off / preset, lock). The snapshot itself is never modified. The overlay
  ends at the first cycle that started after the write for a wheel target (so a dial turn or an
  ignored write shows the real value again after one cycle), and when the command becomes final for
  lock and mode kinds (their status lags the command). It also ends on error, rejection, expiry,
  supersede and unload. Status stays visible as `last_command_status`.
- **Unchanged:** zero writes from polling, setup, reload, diagnostics (only an explicit submit arms
  the timer); permissions, allow-lists, expiry, reconnect guard, no replay (waiting commands are
  dropped on unload), no feature switch.
- **Known weak spot:** the wheel read-back is the written register itself, so CONFIRMED proves
  storage, not that the thermostat adopted it (see the hardware reports).
- **Hardware (2026-11-09):** the approved one-zone check passed (Dolgozó, one step and restore, the
  write cost exactly one request, confirmed by the next cycle), see
  `docs/hardware-debounce-write-test-2026-11-09.md`.
