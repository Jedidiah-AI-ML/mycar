# ADR-006: Device payload contract v1

- **Status:** Accepted (2026-11-16, Phase 2 step 1)
- **Date:** 2026-11-16
- **Source of truth:** `backend/app/gateway/contract.py`. Generated:
  `contracts/freematics_one_plus.v1.schema.json` and `contracts/signals.v1.json`
  (`uv run python -m app.gateway.contract`; a test fails if they drift).

## Context

The simulator (Phase 2) and the AutoCare firmware on the Freematics ONE+ (Phase 8) must
send exactly the same thing, and the gateway must cope with what cellular devices do:
- retry uploads, which can duplicate them
- deliver late and out of order after coverage gaps
- run with wrong clocks
- run newer firmware than the server knows about

Data costs the owner money (a Nigerian data plan), and the ESP32 has little RAM.

## Decision

**Transport.** HTTPS `POST /ingest/freematics_one_plus/batches` with
`Content-Type: application/json` and `Content-Encoding: gzip`. Critical packets go to
`POST /ingest/freematics_one_plus/critical`, sent immediately, small, not batched.
If TLS on the modem proves unreliable, the PRD's HMAC fallback gets its own ADR in
Phase 8.

**Auth.** `Authorization: Bearer <device_id>.<secret>`. The server stores only
`sha256(secret)`. The secret is random and high-entropy, so a slow password hash isn't
needed (step 7).

**Format: JSON + gzip, columnar.**
```json
{"v":1,"device_id":"FM1-0001","fw":"0.1.0","boot":17,"trip":42,"seq":3,"final":false,
 "t0":1763290000000,"sent_at":1763290060250,"gnss_time":null,
 "s":{"rpm":[[0,812],[1000,815]],"coolant":[[0,90]]},
 "g":[[0,6.5244,3.3792,24.0]],
 "ev":[{"t":0,"type":"trip_start"}],
 "net":{"rssi":-71,"bytes_up":183402}}
```
- `s`: each canonical signal name once, then `[dt_ms, value]` pairs. `dt_ms` is ms after
  `t0`, sorted ascending. Values are rounded on the device to the catalogue's `dp`.
- `g`: GNSS fixes `[dt_ms, lat, lon, speed_kmh]`, kept apart so they can be dropped at
  the door without location consent (PRD 10).
- `ev`: typed events: `trip_start`, `trip_end`, `vin`, `pids_supported`, `readiness`,
  `dtc` (mode 03/07/0A), `freeze_frame`, `mode06` (raw values + UASID; scaled on the
  server in Phase 5), `crank` (cranking-voltage trace), `harsh`, `cmd_result`.
- Signal names come from `signals.yaml`. Ids there are stable forever and are what the DB
  stores.

**Idempotency.** Key = `(device_id, boot, trip, seq)`, where `boot` and `trip` are
counters persisted on the device. The server records the key in the same transaction as
the readings. A repeat is acknowledged with `dup: true` and stores nothing.

**Clocks.** `t0` and `sent_at` use the device clock; `gnss_time` is included when there's
a fix. The server compares them with its receive time and corrects skew (step 8). The ack
carries `server_time` so the device can fix its clock.

**Ack and commands.** The response is
`{"ok":true,"dup":false,"server_time":…,"cmds":[{"id":9,"type":"health_scan"}]}`.
Pending commands ride along with acks, so there's no push channel to keep open. The
device reports back with a `cmd_result` event.

**Compatibility rules.**
- Unknown *signal names* and unknown *event types* are **accepted and ignored**, so newer
  firmware never loses data on an older server.
- Unknown *fields* in known objects are **rejected** (catches typos in testing).
- A known event with an invalid field is rejected; it can't hide as "unknown".
- Breaking changes mean `v: 2`, served side by side.

**Limits** (SEED P10): 64 signals and 3,600 samples per signal per batch, 200 events,
samples within 6 h of `t0`. Request size limits (compressed and *decompressed*, against
gzip bombs) are enforced in step 8.

## Consequences

- **Size measured:** a typical 60 s city batch (planned poll rates, 1 Hz accelerometer,
  GNSS every 5 s) is **7.5 KB JSON → 2.26 KB gzipped ≈ 132 KB per driving hour ≈ 8 MB a
  month** at 2 h/day, before HTTPS overhead. Inside the 30 MB/month target (P13).
- JSON is easy to debug with curl, Restfox and logs. If Phase 8 field data shows the
  budget is tight, CBOR is the first step (same structure, ~30 % smaller); that would
  be `v: 2`.
- The firmware must persist `boot` and `trip` counters in flash, and keep unsent batches
  until acked (store-and-forward).
- Raw values for Mode 06 and the crank trace keep the device simple; scaling and
  interpretation live on the server, where they can be fixed without OTA.
