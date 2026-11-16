# Expected monthly running cost (Phase 1)

> Railway prices below are the list prices as last known (RAM about $10/GB-month, vCPU
> about $20/month, volume about $0.15/GB-month, Hobby plan $5/month including $5 of
> usage). **Check https://railway.com/pricing before relying on them** (SEED S11).

## Footprint

Idle RAM measured with `docker stats` on the local containers (2026-11-14). Railway's
own Postgres and real traffic will use more, so the usage column adds headroom.

| Service | What runs | Idle RAM measured | Est. monthly usage |
|---|---|---|---|
| `api` | 1 uvicorn process | 70 MB | ~$1 |
| `worker` | **Now inside `api` (ADR-008)**: no separate service | n/a | $0 |
| `Postgres` | **Moved to Neon (ADR-005)**: not on the Railway bill; Neon free tier, watch its compute usage | n/a | $0 on Railway |
| CPU (mostly idle) + egress | | | ~$1 to 2 |
| **Total usage** | | | **~$2 to 3** |

**Expected Railway bill: $5/month** (usage now sits under the $5 included in the Hobby plan;
plus Neon, $0 while within its free tier). The $5 Hobby fee counts toward usage, so a month
under $5 of usage costs $5. There is no Redis service (ADR-004) and no separate
time-series database (ADR-003).

Not in this number:
- Web app on Vercel (Phase 7): free Hobby tier.
- LLM calls (Phase 4): pay per use, cached by input hash. Expect well under $1/month
  for a few cars on a low-cost model.
- Device data SIM (Phase 8): measured in the field test.

## How it stays low

- One Postgres for data **and** the job queue.
- One uvicorn process, small connection pool (5).
- The UI will read summary tables, never raw readings (Phase 2+).
- Raw-reading retention job (Phase 2+, default 180 days, SEED S9).
- The demo replays stored simulator data on a schedule; no always-on simulator.
- The worker runs inside the api (ADR-008) and leaves the database alone when idle, so
  Neon's free compute (100 CU-h/month) is only used by real traffic plus about 4 short
  wake-ups a day.

Measure real usage in Railway's *Usage* tab after the first week and update this file.
