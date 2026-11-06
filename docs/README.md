# Wavin WTC-NET Home Assistant integration specification

Implementation-ready analysis for a new clean-room native Python integration. No implementation or live Modbus action is included.

1. [Source analysis](01-source-analysis.md)
2. [Protocol specification](02-protocol-specification.md)
3. [Functional requirements](03-functional-requirements.md)
4. [Architecture](04-architecture.md)
5. [Configuration](05-configuration.md)
6. [Implementation plan](06-implementation-plan.md)
7. [Testing strategy](07-testing-strategy.md)

Core decisions: local async Modbus TCP; one serialized gateway transport for multiple unit IDs; HomeGate-inspired one-request-per-second pacing; logical batching with small hard request caps; no automatic writes from polling/setup/reconnect; explicit writes are coalesced and executed before the next poll's reads; configurable heat/cool references and wheel step; no TH1 hidden master, automatic BMS keepalive or base-setpoint enforcement.

Main uncertainty: HomeGate models code 0–12 with configurable 0.5 °C steps, while the Python reference assumes 1 °C/code. Setpoint compensation, BMS timeout, lock timing and reference-only registers also need hardware/vendor validation.

First milestone: a read-only protocol core and mock Modbus environment for two units with 6+4 zones, passing prolonged polling/reconnect tests with zero writes.

