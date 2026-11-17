# ADR-010: Device upload order and clock handling

- **Status:** Accepted (Phase 2 step 5; carries over to the Phase 8 firmware)
- **Date:** 2026-11-17

## Context

The emulated Freematics (step 5) and later the real AutoCare firmware buffer batches in
flash and upload them over a patchy cellular link. Two firmware choices affect what the
gateway must handle.

## Decision

1. **Upload order: freshest first after an outage.** When coverage returns with a backlog,
   the device sends the newest batch first, then drains the backlog oldest-first.
   Critical packets jump the whole queue. When flash is full, the **oldest** batch is
   dropped.
2. **Retries:** a lost request or lost reply backs off from 5 s, doubling up to 5 min.
   A lost reply means the same batch (same key) is sent again; the server's idempotency
   handles it (ADR-006). `sent_at` and `gnss_time` are refreshed on every attempt, so the
   server always gets a current clock pair.
3. **Clock:** the device keeps its own clock, which drifts (±20 ppm, SEED) and restarts at
   0 (1970) when the RTC loses power on a reboot. It is set from a GNSS fix or from the
   ack's `server_time`, but **only at a batch boundary**, so times inside one batch never
   jump. The server does the real correction (step 8), using `sent_at` against receive
   time and `gnss_time`.

## Why

- Freshest first: the dashboard and alert rules care most about *now*. After a 2-hour
  dead zone, the owner should see the current state in seconds, not after the backlog
  drains. Old data still arrives; trip assembly (step 9) is order-independent.
- Dropping the oldest when flash is full keeps recent data, which matters more for alerts.
  At about 3 KB per minute of driving, 4 MB of flash holds roughly 20 driving hours.
- Correcting the clock only between batches keeps every batch internally consistent, so
  the server can shift a whole batch by one offset.

## Consequences

- The gateway must accept out-of-order and duplicate batches. The simulator produces both:
  in 5 car-weeks, 79 duplicates and 69 out-of-order arrivals.
- Batches can carry 1970 timestamps until the first fix or ack after a reboot. The step 8
  skew correction must handle that case, not just small drift.
