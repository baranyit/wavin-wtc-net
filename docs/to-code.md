# Coding tasks queued from decisions

Tasks decided in `open-decisions.md` but not yet implemented. Remove an entry when it lands.

## From decision 2 — cycle length and request count (B + D)

Spec: `docs/04-architecture.md` (polling defaults), `AGENTS.md` §5.

1. **Static points on a slow cadence (B).**
   - Read firmware/serial (4500–4502) on the first cycle after (re)connect, then at most hourly.
   - Planner/client must support per-point cadence: a normal cycle omits static points
     (27 requests for the 6+4 profile), the first cycle includes them (29).
   - Snapshot keeps the last known static values between reads; a reconnect resets the schedule.
   - Use the injected clock, never wall time.
2. **Derived minimum poll interval (D).**
   - `min_interval = max(30 s, ceil(requests × request_spacing × 1.5))`, computed from the *full*
     (first-cycle) plan. Real 6+4 profile: 44 s.
   - Expose it as a pure function next to the planner so the HA options flow (plan step 3) can
     validate against it; the options flow must reject values below it.
   - Default stays 60 s, maximum 300 s.
3. **Tests (virtual clock, no real sleeping).**
   - First cycle = 29 requests; following cycles = 27; static read again after one hour and after
     reconnect.
   - Static values persist in the snapshot between reads; static read failure does not make the
     unit unavailable (optional read).
   - Minimum interval function: 6+4 profile → 44 s; tiny plan → clamped to 30 s; scales with
     spacing and plan size.
   - Existing 24 h soak suite still passes: zero writes, ≥ 1 s between request starts, caps, no
     overlap.
4. Run `python -m pytest`, `ruff check . && ruff format --check .`, `mypy` before proposing.

## From decision 3 — read through address holes (D)

Spec: `docs/open-decisions.md` decision 3.

1. Make `max_gap` a per-endpoint advanced option, default 8, bounds 0–8, with a warning in the
   options flow when changed. Keep `plan_reads` as is (already takes `max_gap`).
2. Tests: gap 0 → 41 requests, gap 8 → 29 for the real 6+4 profile; derived minimum poll interval
   (decision 2) follows the chosen gap; no request exceeds the caps for any gap in 0–8.
3. Record bridged-hole illegal-address responses in diagnostics (address range, count) so the
   first hardware run can decide about switching to option C.

## From decision 4 — fallback split for failing required requests (B)

Spec: `docs/open-decisions.md` decision 4.

1. In the client/planner: on a Modbus exception for a required request, re-plan that request with
   only its `Confidence.BOTH` points in the same cycle (same pacing and caps); mark the dropped
   optional addresses as failing for this connection and exclude them from later plans. Clear the
   set on reconnect.
2. Failing-address set is exposed in the snapshot/diagnostics (table, address, error); no write
   path is touched.
3. Tests (virtual clock): injected exception on a reference-only address inside a required request
   → unit stays available, extra requests only in the failure cycle, later cycles skip the address,
   reconnect resets it; exception on a corroborated address → unit unavailable; ≥ 1 s spacing and
   caps hold during fallback; zero writes.

## From decision 5 — coil polarity (B)

Spec: `docs/open-decisions.md` decision 5, `docs/vendor-bms-v1_9.md`.

1. Add the polarity constants to the HA layer (plan step 3), e.g. `const.py`: on_off True = on,
   comfort_economy True = comfort, heat_cool True = cool, lock True = locked, live True = connected.
   The core (`protocol.py`, `models.py`) stays neutral.
2. Tests: HA mapping of the raw booleans for climate hvac mode/preset/availability; both
   directions of the comfort and heat/cool bits; a zone with conflicting zone/master bits blocks
   target writes (open question in AGENTS.md §13).
3. Hardware run (plan step 4): log status offsets 4/5 next to writable coils offsets 0/1 per zone
   and compare with the physical state.
