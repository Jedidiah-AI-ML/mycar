# Phase 1 plan: Backend foundation (APPROVED 2026-11-13: open questions resolved as recommended)

**Exit gate (PRD 13):** migrations run on Railway; auth and vehicle CRUD pass in pytest and in Restfox.
**Starting point:** near zero (see `docs/phase0-audit.md`). Three files are adapted from `vpm-backend`.

## Steps (dependency order)

Each step ends with its gate green before the next starts. Tests run against a local Postgres in Docker (`docker compose up -d db`); each test session creates and drops its own database.

### Step 1: Repo, tooling, app skeleton
| File | Purpose |
|---|---|
| `git init`, first commit of the Phase 0 docs | Version control. Railway deploys from GitHub (private repo, see open question Q7). |
| `backend/pyproject.toml`, `backend/uv.lock`, `.python-version` (3.12) | uv-managed deps: fastapi, uvicorn, pydantic v2, pydantic-settings, sqlalchemy[asyncio], asyncpg, alembic, pyjwt, pwdlib[argon2], email-validator. Dev: pytest, pytest-asyncio, httpx, ruff. |
| `backend/app/main.py` | App factory; CORS from settings; request-ID middleware; routers. (Adapted from vpm-backend.) |
| `backend/app/core/config.py` | `Settings` (pydantic v2), `get_settings()`. (Adapted.) |
| `backend/app/core/logging.py` | Structured JSON logs (stdlib `logging` + JSON formatter, no extra dependency); request ID in every log line. |
| `backend/app/api/health.py` | `GET /health` (liveness). `GET /health/ready` (DB `SELECT 1` + Alembic revision == head; 503 otherwise). (Adapted.) |
| **Gate** | `tests/test_health.py`: `/health` 200; `/health/ready` 200 with DB and 503 with DB down (dependency override). `ruff check` clean. |

### Step 2: Containers
| File | Purpose |
|---|---|
| `backend/Dockerfile` | Multi-stage: `uv sync --frozen --no-dev` in a builder, slim runtime, non-root user, `CMD uvicorn ... --port ${PORT:-8000}`. |
| `docker-compose.yml` | `db` (postgres:16) + `api`. **No Redis, no Timescale.** |
| `backend/railway.toml` | Build from Dockerfile; healthcheck `/health`; **pre-deploy command `alembic upgrade head`**. |
| **Gate** | PowerShell: `docker compose up --build -d`, then `Invoke-RestMethod http://localhost:8000/health` returns `ok`. |

### Step 3: Database layer + Alembic
| File | Purpose |
|---|---|
| `backend/app/db/session.py` | Async engine and `AsyncSession` dependency; pool sized small (5). |
| `backend/app/db/base.py` | Declarative base with a naming convention (stable constraint names in migrations), `TimestampMixin`. |
| `backend/alembic.ini`, `backend/app/db/alembic/env.py` | Async Alembic env reading `DATABASE_URL`. |
| `backend/tests/conftest.py` | Creates a throwaway test DB, runs `alembic upgrade head`, gives a transactional session and an `httpx.AsyncClient`. |
| **Gate** | `test_migrations.py`: upgrade head → downgrade base → upgrade head on an empty DB; `alembic check` reports no drift between models and migrations. |

### Step 4: Core models + migration `0001_core`
| File | Purpose |
|---|---|
| `backend/app/models/{user,consent,vehicle,access,audit,job}.py` | Tables below. |
| `backend/app/db/alembic/versions/0001_core.py` | Creates them. |
| **Gate** | Model tests: unique email (case-insensitive), one active owner per vehicle, one active consent per purpose, fuel-type check rejects non-petrol. |

### Step 5: Auth
| File | Purpose |
|---|---|
| `backend/app/core/security.py` | Argon2 password hashing (pwdlib); JWT access tokens (HS256, 30 min); opaque refresh tokens stored **hashed**, rotated on every use. |
| `backend/app/api/auth.py`, `schemas/auth.py` | `POST /auth/register` (requires the service-processing consent), `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /me`. |
| `backend/app/api/deps.py` | `current_user` dependency. |
| **Gate** | `test_auth.py`: register → login → me; wrong password 401; expired/garbage JWT 401; refresh rotation (an old refresh token is rejected after use); logout revokes; register without consent → 422. |

### Step 6: Consents + data export/deletion (NDPA, PRD 10)
| File | Purpose |
|---|---|
| `backend/app/config/consents.yaml` | Purposes (`service_processing`, `location`, `model_improvement`) with version and plain-English text. **SEED: wording needs Wisdom's review.** |
| `backend/app/api/consents.py` | `GET /me/consents`, `POST /me/consents/{purpose}` (grant), `DELETE /me/consents/{purpose}` (revoke: sets `revoked_at`, never deletes). |
| `backend/app/api/account.py` | `GET /me/export` (JSON of everything held about the user so far); `DELETE /me` (soft delete now, purge job later; see Q5). |
| **Gate** | `test_consents.py`: grant, revoke, re-grant keeps history; export contains the user, consents and vehicles; deleted user can't log in. |

### Step 7: Vehicles + per-vehicle authorisation + sharing
| File | Purpose |
|---|---|
| `backend/app/api/deps.py` | `require_vehicle_role(min_role)`, used by **every** `/vehicles/{id}` route. Returns 404 (not 403) to users with no access, so vehicle IDs don't leak. |
| `backend/app/services/vin.py` | VIN format check (17 chars, no I/O/Q); check digit is **warn-only** (not mandatory outside North America). |
| `backend/app/api/vehicles.py`, `schemas/vehicle.py` | `POST /vehicles` (creator becomes owner), `GET /vehicles` (all I can see, with my role), `GET/PATCH/DELETE /vehicles/{id}`. |
| `backend/app/api/access.py` | `GET /vehicles/{id}/access`, `POST` (owner shares with an **existing user by email** as viewer/manager), `PATCH` role, `DELETE` (revoke). Every change writes an `audit_events` row. |
| **Gate** | `test_vehicles.py`: CRUD; **role matrix test** (owner/manager/viewer/stranger × every endpoint, about 40 parametrised cases); can't remove the last owner; audit rows written. |

Role rules: **owner** does everything, including share, transfer and delete. **manager** edits vehicle details (and, later, service records, device claim, Quick Health Scan). **viewer** reads only.

### Step 8: Job queue (ADR-004)
| File | Purpose |
|---|---|
| `backend/app/jobs/queue.py` | `enqueue(name, payload, run_at=None, dedupe_key=None, session=...)`. Uses the caller's session, so the job commits or rolls back with the data. |
| `backend/app/jobs/registry.py` | `@job("name", max_attempts=5)` decorator. |
| `backend/app/jobs/worker.py` | Loop: claim with `FOR UPDATE SKIP LOCKED`, set a lease (`locked_until`), run, mark done/failed; exponential backoff; re-queue expired leases (crashed worker); `LISTEN/NOTIFY` wake-up with a 5 s polling fallback; graceful shutdown. Entrypoint `python -m app.jobs.worker`. |
| `backend/app/jobs/handlers/system.py` | `system.ping` (test job) and `account.purge` (finishes `DELETE /me`). |
| **Gate** | `test_jobs.py`: 2 workers × 200 jobs, each runs **exactly once**; failing job retries then goes to `dead`; enqueue inside a rolled-back transaction leaves no job; expired lease is reclaimed; dedupe key blocks duplicates. |

### Step 9: Railway deploy
- Railway project in an **EU region** (Q6): services `postgres`, `api` (Dockerfile, pre-deploy `alembic upgrade head`) and `worker` (same image, start command `python -m app.jobs.worker`).
- **Gate:** deploy logs show the migration applied; `GET https://<api>.up.railway.app/health/ready` → 200; full Restfox list below passes against Railway; a `system.ping` enqueued via an admin-only endpoint runs on the Railway worker.

### Step 10: Docs
- `docs/cost.md` (expected monthly cost, see below), `README.md` (PowerShell quick-start), `backend/restfox/phase1.md` + exported collection JSON.

## Phase 1 schema

All PKs are `uuid` (`gen_random_uuid()`) except `jobs` and `audit_events` (`bigint identity`). All times are `timestamptz` (UTC in the DB, Africa/Lagos in the UI).

**users**: `id`, `email` (unique on `lower(email)`), `password_hash`, `display_name`, `timezone` (default `Africa/Lagos`), `is_admin` bool, `is_demo` bool, `email_verified_at` null, `created_at`, `updated_at`, `deleted_at` null.

**refresh_tokens**: `id`, `user_id` → users, `token_hash` unique, `issued_at`, `expires_at`, `revoked_at` null, `replaced_by` null, `user_agent`.

**consents**: `id`, `user_id` → users, `purpose` enum (`service_processing`, `location`, `model_improvement`), `version` text, `granted_at`, `revoked_at` null. Partial unique `(user_id, purpose) WHERE revoked_at IS NULL`. Rows are never deleted, so the history is the audit.

**vehicles**: `id`, `vin` char(17) null (partial unique `WHERE deleted_at IS NULL`), `vin_source` enum (`manual`, `device`), `make`, `model`, `year` smallint (1996 to current+1), `engine` text (e.g. `2ZR-FE 1.8L I4`), `displacement_l` numeric(3,1), `cylinders` smallint, `fuel_type` (CHECK = `petrol` in v1), `protocol` null, `supported_pids` jsonb null, `nickname`, `odometer_at_install_km` int (0 to 1,500,000), `installed_at` date, `created_by` → users, `created_at`, `updated_at`, `deleted_at`.

**vehicle_access**: `id`, `vehicle_id` → vehicles, `user_id` → users, `role` enum (`owner`, `manager`, `viewer`), `granted_by` → users, `granted_at`, `revoked_at` null. Partial unique `(vehicle_id, user_id) WHERE revoked_at IS NULL`; partial unique `(vehicle_id) WHERE role='owner' AND revoked_at IS NULL` (exactly one owner).

**audit_events**: `id`, `actor_user_id`, `action` (e.g. `access.granted`, `consent.revoked`, `account.deleted`), `subject_type`, `subject_id`, `details` jsonb, `created_at`.

**jobs**: `id`, `name`, `payload` jsonb, `status` enum (`queued`, `running`, `succeeded`, `failed`, `dead`), `priority` smallint, `run_at`, `attempts`, `max_attempts`, `locked_by`, `locked_until`, `last_error`, `dedupe_key` (partial unique while queued/running), `created_at`, `finished_at`. Index `(priority, run_at) WHERE status='queued'`.

Created in Phase 2 with the features that write them: `devices`, `device_assignments`, `device_commands`, `trips`, `readings` (BRIN on `ts`), summary tables, `dtc_events`, `mode06_results`. Later phases add the rest of PRD Section 8.

## Restfox request list (Phase 1)

Environment variables: `base` (local or Railway URL), `token`, `refresh`, `vid`.

| # | Request | Expect |
|---|---|---|
| 1 | `GET {{base}}/health` | 200 `{"status":"ok"}` |
| 2 | `GET {{base}}/health/ready` | 200, `db: ok`, `migrations: head` |
| 3 | `POST /auth/register` `{email, password, display_name, consents:["service_processing"]}` | 201 |
| 4 | `POST /auth/register` same email, different case | 409 |
| 5 | `POST /auth/login` | 200 `access_token`, `refresh_token` → save |
| 6 | `POST /auth/login` wrong password | 401 |
| 7 | `GET /me` with token | 200 |
| 8 | `GET /me` without token | 401 |
| 9 | `POST /auth/refresh` | 200 new pair; repeat with the old refresh → 401 |
| 10 | `GET /me/consents` | `service_processing` active |
| 11 | `POST /me/consents/location`, then `DELETE` | 201, then 204; GET shows history |
| 12 | `POST /vehicles` (Corolla 2010, 1.8L, odometer 145000) | 201 → save `vid` |
| 13 | `POST /vehicles` with fuel_type `diesel` | 422 |
| 14 | `POST /vehicles` with VIN containing `O` | 422 |
| 15 | `GET /vehicles` | list with `role: owner` |
| 16 | `PATCH /vehicles/{{vid}}` nickname | 200 |
| 17 | Register user B; `POST /vehicles/{{vid}}/access` `{email:B, role:viewer}` | 201 |
| 18 | As B: `GET /vehicles/{{vid}}` | 200; `PATCH` → 403 |
| 19 | User C (no access): `GET /vehicles/{{vid}}` | 404 |
| 20 | Owner: `DELETE /vehicles/{{vid}}/access/{B}` | 204; B's GET → 404 |
| 21 | `GET /me/export` | JSON with user, consents, vehicles |
| 22 | `DELETE /vehicles/{{vid}}` | 204 |
| 23 | `DELETE /me`, then `POST /auth/login` | 204, then 401 |

## Expected monthly cost

Railway charges a **Hobby plan at $5/month, which includes $5 of usage**, then bills RAM, CPU, volume storage and egress by the minute. *These are Railway's list prices as I last knew them (RAM about $10/GB-month, vCPU about $20/month, volume about $0.15/GB-month). Check railway.com/pricing before signing up.*

| Service | Idle RAM | Est. usage |
|---|---|---|
| api (1 uvicorn worker) | ~150 MB | ~$1.5 |
| worker | ~120 MB | ~$1.2 |
| Postgres + 1 to 5 GB volume | ~200 MB | ~$2 to 3 |
| CPU (mostly idle) + egress | | ~$1 to 2 |
| **Total** | | **~$6 to 8 usage → about $6 to 8/month billed** (the $5 plan fee counts toward usage) |

Vercel web (Phase 7) is free on the Hobby tier. LLM (Phase 4) is pay-per-use and cached; expect under $1/month for a few cars. A device data SIM is separate (Phase 8).

**How to keep it low:** one Postgres for data and queue (no Redis); a single uvicorn worker; small DB pool; the UI reads summary tables, never raw readings; a raw-reading retention job (Phase 2+); the demo uses scheduled replay instead of an always-on process. Optional: run the worker inside the api container to save about $1/month (Q3).

## Open questions for Wisdom

1. **Phase 1 table scope.** I propose only identity, vehicles, access, audit and jobs now, with device/trip/readings tables in Phase 2 where they're tested. Or create every Section 8 table now?
2. **Queue:** home-grown `jobs` table (recommended; about 150 lines and easy to understand) or Procrastinate (a library, with cron built in)?
3. **Worker:** separate Railway service on the same image (recommended: clean logs, independent restarts, about $1/month more) or a second process in the api container?
4. **Sharing in Phase 1:** share only with users who already have an account, by email. Email invitation links come in Phase 6, when email exists. OK?
5. **Account deletion:** soft delete now, hard purge after **30 days** (SEED). Or delete immediately?
6. **Railway region:** EU West (Amsterdam) for Nigeria latency. Do you already have a Railway project or account?
7. **GitHub:** create a private repo (e.g. `autocare`) so Railway can deploy from it?
8. **VIN decode:** Phase 1 = manual make/model/year entry + VIN validation. Free NHTSA vPIC decode (works for US-market imports) as an optional Phase 2 add-on?
9. **Auth style:** email + password now. Magic-link login needs an email provider (Phase 6).

## SEED values and assumptions to validate

| # | Value | Where | Why unsure |
|---|---|---|---|
| S1 | Consent wording and version `2026-11-v1` | `config/consents.yaml` | Needs NDPA-aware wording; ideally a legal check |
| S2 | Access token 30 min, refresh 30 days | settings | Product trade-off: convenience vs risk |
| S3 | Account purge after 30 days | settings | NDPA sets no fixed period |
| S4 | VIN check digit warn-only | `services/vin.py` | Mandatory only for North-American-market VINs |
| S5 | Model year range 1996 to current+1 | schema check | OBD-II mandatory in the US from MY1996; EOBD petrol from 2001 in the EU. Some Nigerian imports may be non-US-market |
| S6 | Odometer 0 to 1,500,000 km | schema check | Sanity bound only |
| S7 | v1 fuel type `petrol` only | schema check | Per PRD 1; relaxing later needs a migration |
| S8 | Readings volume about 1.2 M rows/car/month (2 h driving/day) | ADR-003 | Lagos usage could be much higher (long idle in traffic) |
| S9 | Raw-reading retention 180 days | ADR-003, Phase 2 | Storage cost vs re-analysis needs |
| S10 | Job retry: 5 attempts, backoff 10 s × 2ⁿ | `jobs/` | Generic default |
| S11 | Railway prices above | `docs/cost.md` | May have changed; verify |
| S12 | Freematics ONE+ LTE about USD 150 to 250 | ADR-001 | Verify when buying (Phase 8) |
