# AutoCare

Early-warning web app for petrol cars, fed by a cellular OBD-II device. Personal MVP and
portfolio project. **Source of truth: [docs/PRD.md](docs/PRD.md).** Decisions are in
[docs/decisions/](docs/decisions/), phase plans in [docs/plans/](docs/plans/).

Status: **Phase 1 (backend foundation)**: auth, consents, vehicles, sharing, job queue.

## Run it locally (Windows, PowerShell)

Needs Docker Desktop and [uv](https://docs.astral.sh/uv/).

```powershell
docker compose up -d db                # Postgres on localhost:5433
cd backend
uv sync                                # installs Python 3.12 + dependencies
uv run alembic upgrade head            # create tables
uv run uvicorn app.main:app --reload   # API on http://localhost:8000  (docs: /docs)
```

In a second terminal, the background worker:

```powershell
cd backend
uv run python -m app.jobs.worker
```

Or everything in containers: `docker compose up --build -d` (db + api + worker).

## Tests

```powershell
docker compose up -d db
cd backend
uv run pytest -q          # 99 tests; creates and drops its own autocare_test database
uv run ruff check .
```

API check (same list as the Restfox collection), against a running API:

```powershell
cd backend
uv run python restfox/phase1_requests.py run http://localhost:8000
```

Restfox: import `backend/restfox/autocare_phase1.postman_collection.json` (Postman format),
set `base` and `run`, and run the requests in order. Each description says the expected
status and which response field to copy into which variable.

## Layout

```
backend/
  app/
    api/        health, auth, account (/me), vehicles, access (sharing), admin
    core/       config, security (passwords, tokens), JSON logging
    db/         engine/session, base, alembic migrations
    models/     SQLAlchemy models
    schemas/    Pydantic request/response models
    services/   consents, audit trail, VIN checks
    jobs/       enqueue(), worker, handlers (ADR-004)
    config/     consents.yaml (SEED - validate)
  tests/
  restfox/      Phase 1 request list + Postman/Restfox export
docs/           PRD, ADRs, plans, cost, deploy guide
ml/data_cards/  dataset documentation (data itself lives in DATASETS_DIR, never in git)
```

## Deploying

See [docs/deploy-railway.md](docs/deploy-railway.md). Expected cost: [docs/cost.md](docs/cost.md).
