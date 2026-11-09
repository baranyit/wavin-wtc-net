# Architecture

Use one config entry/runtime per TCP endpoint. It owns one async client and serialized queue shared by controller unit IDs. Suggested modules: `transport.py` (PyModbus/timeouts/backoff), `protocol.py` (pure addresses/codecs), `models.py` (snapshots), `client.py` (read-only snapshot and explicit commands), `commands.py` (permission/validation/queue/verification), `write_transport.py` + `write_backend.py` (the only write route, a transport subclass constructed only with write mode on), `polarity.py`, `coordinator.py`, config flow, entities, diagnostics and translations.

```text
GatewayRuntime(host,port)
  ├─ GatewayTransport (one client/queue)
  ├─ Controller(unit=1) ─ Zone 1..N
  └─ Controller(unit=2) ─ Zone 1..N
```

Cycle flow: coordinator wakes → perform serialized bounded reads → use those reads to verify earlier writes and build typed snapshots → atomic entity update. A cycle never writes. A partial unit failure marks only its entities unavailable. Entity properties perform no I/O.

Command flow: validate capability/permission/range → map target to one raw code → enqueue by `(unit, zone, command-kind)` with latest-value-wins semantics → restart the endpoint-wide 2 s debounce (flush at the latest 10 s after the first waiting command) → write the queue in fixed order through the shared transport lock and 1 request/s limiter, possibly between two requests of a running cycle → confirm from the next cycle that started after the write (a cycle running at write time is ignored for that command). No read is triggered by a write. Until then the entities show the requested value (optimistic overlay, `CommandQueue.optimistic`); the snapshot is never altered, and the overlay ends on the first post-write cycle for a wheel target, on the final status for lock/mode kinds, and on error, expiry, supersede or unload (decision 22). Commands carry an expiry/generation token, preventing replay after reconnect, reload or a newer command.

Controller and zone devices are separate; zones use `via_device`. Prefer validated serial+unit for identity; until serial reliability is proven, persist a generated gateway ID and combine it with unit/zone—never rely on `entry_id` alone.

Initial poll default: 60 s, configurable 30–300 s, but never below a minimum derived from the read plan: `ceil(requests × request_spacing × 1.5)` (44 s for the real 6+4 profile). Static points (firmware, serial) are read on the first cycle after (re)connect and then at most hourly (decision 2 in `open-decisions.md`). The endpoint scheduler starts at HomeGate's conservative maximum of one request per second. Logical address groups are further split to initial hard caps of 15 registers/16 coils; no adaptive increase occurs automatically. Normalize transport/Modbus/malformed-response errors, track last success/latency/failure count, and use bounded exponential reconnect backoff. Use current HA config-entry, coordinator, runtime-data, entity-description, diagnostics, Repairs and translation APIs at implementation time.

If a cycle is still running when the next interval arrives, cycles never overlap: skip/coalesce the tick and continue through the same queue. A failed write does not abort unrelated reads unless the connection itself is unusable.


## Read policy summary (decisions 2-4)

Steady-state cycle for the real 6+4 profile: 27 requests; first cycle after (re)connect: 29
(static points). `max_gap` defaults to 8 (0..8). A failing required request is retried in the same
cycle first without hole reads, then with corroborated points only; failing optional addresses are
excluded (and never bridged) until the next reconnect. See `docs/open-decisions.md`.
