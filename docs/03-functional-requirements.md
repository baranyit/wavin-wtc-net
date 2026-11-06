# Functional requirements

- **FR-01:** Native async Python/HA integration; local Modbus TCP; no cloud, Java or MQTT dependency.
- **FR-02:** Multiple gateways and multiple explicit unit IDs per gateway; all hardware-allowed zones (currently evidenced 1–7/unit).
- **FR-03:** Read-only setup/discovery plus manual unit/zone selection and names.
- **FR-04:** One climate per zone with supported current temperature, target, HVAC mode/action, preset and availability.
- **FR-05:** Humidity, dew point, demand/valve, live and verified controller/fault measurements; raw diagnostics disabled by default.
- **FR-06:** Separate configurable heat/cool neutral temperatures and explicit comfort/economy references.
- **FR-07:** Configurable wheel center/range/step; preserve confirmed 0.5 °C resolution.
- **FR-08:** Physical dial changes update HA and are never undone by polling.
- **FR-09:** Target writes affect only the intended zone. They are coalesced and executed at the beginning of the next polling cycle, then confirmed by that cycle's reads.
- **FR-10:** Global controls are explicit and separate; TH1 has no hidden master behavior.
- **FR-11:** Optional lock control after delayed-status validation.
- **FR-12:** Config, options and reconfigure flows; stable IDs survive rename/reload.
- **FR-13:** English primary strings and Hungarian translation; redacted diagnostics.

## Safety/non-functional

- Setup, reconnect, diagnostics and unload make zero writes. A polling cycle may write only explicit queued user commands before its reads; it must never generate corrective writes itself.
- Read-only is default; wheel/lock and global/base-setpoint permissions are separate.
- One serialized I/O queue per endpoint; initially no more than one request starts per second; small hard block caps prevent oversized reads; bounded timeout/backoff and no command replay.
- Failed units become unavailable, not stale-live; other units remain usable.
- Protocol code has no HA imports; typed immutable snapshots; async bounded I/O.
- CI covers unit/mock/HA tests, type/lint, hassfest and HACS validation.
- Trusted-LAN guidance; no secrets/full frames in normal logs or diagnostics.

## MVP acceptance

Two units with 6+4 zones yield ten stable climate devices; 24-hour polling with an empty command queue makes zero writes and never exceeds the configured request rate/block caps; 22/26 °C and 0.5 °C are preserved; outage/recovery works; a permitted target waits for the next cycle, changes one zone and becomes state only after confirmation; no zone action writes 4127–4129.

Deferred: automatic seasonal switching, BMS keepalive, program/base-setpoint editing, heat-pump optimization, inferred energy and unverified thermostat models.

