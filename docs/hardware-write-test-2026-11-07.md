# Hardware write test, 2026-11-07 (plan step 5 gate)

Approved by the maintainer for one zone: Dolgozó (unit 2, zone 2), one raw wheel step, restore
afterwards (`AGENTS.md` section 10). Run with `tests/hardware/test_write_validation.py`
(`WTC_WRITE_TEST="2:2:10:9"`), the integration's own writable transport, queue and poller.
Write mode on for this one zone only; conservative defaults (1 request/s, 15/16 caps, 44 s cycles).

Result: **passed**. 381 s, 166 requests, 0 failures, exactly 2 writes (one function-06 register
write each), both confirmed by the read-back of the following reads.

| Cycle | Time | Wheel code | Offset | Packed word 4601 | Note |
|---|---|---|---|---|---|
| 0 precondition | 23:19:42 | 10 | +2.0 | 2566 (0x0A06) | 24 °C, heating, comfort, on |
| 1 write 10→9 + read-back | 23:20:53 | 9 | +1.5 | 2310 (0x0906) | command: queued → sent → confirmed |
| 2 observe | 23:22:04 | 9 | +1.5 | 2310 | stable |
| 3 pre-restore | 23:23:14 | 9 | +1.5 | 2310 | stable |
| 4 restore 9→10 + read-back | 23:24:25 | 10 | +2.0 | 2566 | queued → sent → confirmed |
| 5 observe | 23:25:35 | 10 | +2.0 | 2566 | restored |

Findings:
- The write went out as the first request of the cycle, before the reads, and the same cycle's
  reads confirmed it. Request spacing and caps held; no replay in later cycles.
- A single register write moved the whole wheel state: the offset (B+3) and the packed read-only
  word (high byte = even zone) followed the written code. That matches the dial formula
  (22 + (code − 6) × 0.5: 24 → 23.5 → 24).
- Physical display (maintainer): the thermostat showed 23.5 °C after the write and returned to
  24 °C after the restore. So the written code is the dial position the thermostat uses.
- Not tested: power-cycle persistence (vendor: BMS-written values last until power cycle), other
  zones, economy, cooling, lock.

## Second run: physical dial turn after a write (23:34-23:46)

Same zone and step (10 → 9), approved again by the maintainer; then 5 read-only cycles while the
maintainer turned the physical dial from the displayed 23.5 °C to 24.5 °C (two steps). 301
requests, 0 failures, 2 writes (step and restore), both confirmed.

- After the turn the read-back showed code 11, offset +2.5, packed word 2822 (0x0B06), and stayed
  there for all 5 cycles. Polling did not undo the dial change and sent no write (the only writes
  were the step before and the restore after the window).
- So after a BMS-style register write the physical dial still takes effect, and "physical dial
  changes win" (AGENTS §4.9) holds on this hardware.
- The restore wrote code 10 over the hand-set code 11, as designed (the test restores the approved
  starting state); the display returned to 24 °C.
