# ADR-002: Device adapters with capability profiles

- **Status:** Accepted (2026-11-13, with the Phase 1 plan)
- **Date:** 2026-11-13

## Context

v1 builds one device (Freematics ONE+, ADR-001), but later devices are expected: a Bluetooth dongle through a future mobile app, a Teltonika tracker, a teammate's custom cellular board. Devices also differ in *what* they can read. Some cars or adapters don't support Mode 06; some devices can't sample cranking voltage fast enough; GNSS may be switched off for privacy. If downstream code assumes every signal exists, a missing signal looks the same as a healthy one. That breaks principle 4 (honest uncertainty).

## Decision

1. **Gateway boundary.** All device traffic enters through `backend/app/gateway/`. Each device type has an **adapter** module implementing one small interface:
   - `authenticate(request) -> Device`: per-device token or HMAC secret (stored as a hash).
   - `parse(payload) -> NormalisedBatch`: validates the vendor payload and converts it to **canonical rows** (`vehicle_id, trip_id, ts, signal, value`, plus DTC, Mode 06 and event records) using canonical signal names (e.g. `rpm`, `speed_kmh`, `coolant_c`, `stft_b1_pct`).
   - `ack(batch) -> Response`: idempotent acknowledgement keyed by `(device_id, trip_id, seq)`.
   - `encode_command(command) -> bytes`: serialises remote commands (Quick Health Scan, poll-rate change).
2. **Capability profile.** Each device has a JSONB profile of booleans (live PIDs, Mode 03, Mode 07, Mode 0A, Mode 06, freeze frame, battery voltage, high-rate cranking voltage, GNSS, accelerometer, VIN, remote commands), plus the car's supported-PID bitmap once it's known. The **effective capability** is device capability AND what the vehicle supports AND user consent (e.g. location off).
3. **Feature gating.** Every health indicator and feature declares the capabilities it needs. If one is missing, the indicator is marked `unavailable` (not "normal") and the UI says so in plain words.
4. **Registry.** Adapters are looked up by `devices.type`. v1 registers exactly one: `freematics_one_plus`. Ingestion URL: `POST /ingest/{device_type}/batches`.
5. Everything after the gateway (trips, indicators, anomaly scoring, alerts) only ever sees canonical rows. It never imports adapter code.

## Consequences

- **Good:** adding a device later means one new adapter file and one capability profile, with no changes to business logic.
- **Good:** the simulator's "Freematics emulation" mode tests the real adapter end to end, and capability switches (e.g. no Mode 06) test graceful degradation (PRD 8).
- **Cost:** a little more structure up front (an interface, a registry, a canonical signal list). This is the one planned "for later" exception PRD Section 0 allows.
- **Rule:** no second adapter gets built or stubbed in v1 (PRD 12).
