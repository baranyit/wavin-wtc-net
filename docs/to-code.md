# Coding tasks queued from decisions

Tasks decided in `open-decisions.md` but not yet implemented. Remove an entry when it lands.

## Landed in the read core (2026-11-07)

Decisions 2 (static cadence, derived minimum), 3 (`max_gap` 0..8, hole-failure log) and 4 (fallback
split, per-connection exclusions) are implemented in `client.py`, `protocol.py` (`Point.static`,
`plan_reads(blocked=...)`) and `transport.py` (`connection_generation`), with tests in
`tests/test_client.py`, `tests/test_protocol.py` and `tests/test_soak.py`.

## Landed in the HA layer (2026-11-07)

Items 1-3 below are implemented: options validation against `minimum_poll_interval`, `max_gap`
option, diagnostics + Repairs issue for excluded points, polarity constants (`const.py`).
Still to do: conflicting zone/master bit handling (needs the command layer, plan step 5) and the
hardware-run logging of status offsets 4/5 vs writable coils 0/1 (plan step 4).

## Original list (for reference)

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

## Landed: command layer (2026-11-07, plan step 5)

`commands.py`, `write_transport.py`, `write_backend.py`, `polarity.py`, poller `before_reads`,
coordinator `submit_wheel_target`, climate `set_temperature`, options (`write_enabled`,
`writable_zones`), diagnostics (recent commands). Hardware write test done (see `docs/hardware-write-test-2026-11-07.md`). Dial-turn after write confirmed. Cooling comfort writes opened (decision 11). Remaining: lock
writes after delayed-status validation, cooling writes after a cooling run, economy semantics.
