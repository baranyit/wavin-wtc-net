# Coding tasks queued from decisions

Tasks decided in `open-decisions.md` but not yet implemented. Remove an entry when it lands.

## Landed in the read core (2026-11-07)

Decisions 2 (static cadence, derived minimum), 3 (`max_gap` 0..8, hole-failure log) and 4 (fallback
split, per-connection exclusions) are implemented in `client.py`, `protocol.py` (`Point.static`,
`plan_reads(blocked=...)`) and `transport.py` (`connection_generation`), with tests in
`tests/test_client.py`, `tests/test_protocol.py` and `tests/test_soak.py`.

## Still open for plan step 3 (HA layer)

1. **Options flow:** validate the poll interval against `WtcReadClient.minimum_poll_interval`
   (44 s for the real 6+4 profile; default 60 s, max 300 s); expose `max_gap` (0..8, default 8)
   as an advanced option with a warning when changed. `GatewayPoller` itself does not enforce
   the minimum.
2. **Diagnostics / Repairs:** surface `UnitSnapshot.excluded_points`, `failed_optional_reads` and
   `WtcReadClient.hole_failures`. The first hardware run uses `hole_failures` to decide about
   switching to documented-only bridging (decision 3, option C).
3. **Polarity constants** (decision 5), HA layer only: on_off True = on, comfort_economy True =
   comfort, heat_cool True = cool, lock True = locked, live True = connected. Tests: both bit
   directions for hvac mode/preset/availability; conflicting zone/master bits block target writes
   (AGENTS.md §13). Hardware run: log status offsets 4/5 next to writable coils offsets 0/1.
