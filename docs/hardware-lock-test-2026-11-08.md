# Hardware lock test, 2026-11-08 (decision 13)

Approved by the maintainer for one zone: Dolgozó (unit 2, zone 2), initially unlocked. Run with
`tests/hardware/test_lock_validation.py` (`WTC_LOCK_TEST="2:2"`) through the integration's own
writable transport, queue and poller. Lock writes enabled for this one zone only; conservative
defaults (1 request/s, 15/16 caps, poll grid 44 s start to start).

Result: **passed**. 115 s, 85 requests, 0 failures, exactly 2 writes (one function-05 coil write
each: lock, unlock), both confirmed, zone ended unlocked.

| Cycle | Ended | Lock status (4186+, zone 2) | Command |
|---|---|---|---|
| 0 precondition | 00:09:13 | unlocked | none |
| 1 lock write + read-back | 00:09:56 | **locked** | lock: queued → sent → confirmed |
| 2 unlock write + read-back | 00:10:40 | **unlocked** | unlock: queued → sent → confirmed |

Findings:
- The lock coil write (address 4656+(z-1), here 4657) took effect and the status coil
  showed the new state already in the read phase of the same cycle (0 extra cycles, both
  directions). So the lag between the command and the status coil is shorter than the time from
  the write to the status read inside one cycle (some seconds to about half a cycle). The exact
  lag cannot be measured without extra out-of-cycle reads, which the safety rules forbid; the
  4-cycle verification window is therefore generous.
- 4186+(z-1) works as the lock status address for this installation (it changed with the command).
- Not tested: other zones, persistence over a power cycle.

## Second run: thermostat behaviour while locked (00:34-00:37)

Same zone and sequence, with a hold window (`WTC_LOCK_HOLD_CYCLES=2`) so the maintainer could look
at the thermostat. 203 s, 2 writes (lock, unlock), both confirmed, zone ended unlocked.

- Lock queued 00:34:35, visible in the read-back (status coil) at 00:35:17, zone held locked for
  two more read-only cycles, unlock queued 00:36:44, read-back unlocked.
- Physical thermostat (maintainer): the lock symbol appeared, and the operation on the thermostat
  (dial/buttons) was blocked while locked ("tiltva"); after the unlock the thermostat was usable
  again.
- So lock = child lock of the thermostat's own controls; Home Assistant/BMS writes are not
  affected. The earlier first run did not record the display; this closes that open point.
