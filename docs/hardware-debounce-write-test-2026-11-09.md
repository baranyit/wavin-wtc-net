# Hardware write test: debounced immediate write (decision 22), 2026-11-09

Approved by the maintainer: Dolgozó (unit 2, zone 2), one raw wheel step 10 → 9 (24.0 → 23.5 °C),
then restore. Run: `tests/hardware/test_write_validation.py` with `WTC_WRITE_TEST="2:2:10:9"`,
endpoint 192.168.1.210, conservative defaults (1 req/s, 15/16 caps, poll interval 44 s), write mode
only for this zone. Production dispatch path (2 s debounce), one run, stopped when done.

## Result: passed

| Step | Observation |
|---|---|
| Precondition read | wheel code 10, zone live, on, heating, comfort |
| Submit code 9 | written 2.2 s after the submit; `requests_started` grew by exactly 1 (the write), no read triggered; status `sent` |
| Next cycle | wheel code 9, command `confirmed`; observation cycle unchanged |
| Submit code 10 (restore) | written 2.2 s after the submit; exactly 1 request; `sent` |
| Next cycle | wheel code 10, `confirmed`; observation cycle unchanged |

Two writes in total, 0 transport failures, 166 requests in about 6.5 minutes. The zone is back at
the original state (code 10). No anomalies. The human dial/overlay check in Home Assistant was not
part of this run (no HA instance); the overlay is covered by the HA tests.

Caveat unchanged: the wheel read-back is the written register itself, so `confirmed` proves
storage; the earlier reports show the thermostat adopts it.
