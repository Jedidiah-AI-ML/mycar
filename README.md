# AutoCare

Early-warning web app for petrol cars, fed by a cellular OBD-II device. Personal MVP and
portfolio project. **Source of truth: [docs/PRD.md](docs/PRD.md).** Decisions are in
[docs/decisions/](docs/decisions/), phase plans in [docs/plans/](docs/plans/).

Status: **Phase 1 (backend foundation) done**: auth, consents, vehicles, sharing, job queue. Live on Railway with Neon Postgres (ADR-005); request list 33/33 against the live API (2026-11-16).

## Run it locally (Windows, PowerShell)

Needs Docker Desktop and [uv](https://docs.astral.sh/uv/).

```powershell
docker compose up -d db                # Postgres on localhost:5433
cd backend
uv sync                                # installs Python 3.12 + dependencies
uv run alembic upgrade head            # create tables
uv run uvicorn app.main:app --reload   # API on http://localhost:8000  (docs: /docs)
```

The background worker runs inside the API process (ADR-008), so there's nothing else to
start. To run it separately instead, set `WORKER_MODE=standalone` for the API and run
`uv run python -m app.jobs.worker` in a second terminal.

Or everything in containers: `docker compose up --build -d` (db + api).

## Tests

```powershell
docker compose up -d db
cd backend
uv run pytest -q          # 113 tests; creates and drops its own autocare_test database
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

## Simulator (Phase 2)

```powershell
cd simulator
uv run pytest -q                       # physics, calibration and fault gate tests (~2 min)
uv run python -c "from autocare_sim.run import simulate; tl = simulate('toyota_corolla_1_8_2009', days=7, seed=1); print(len(tl.trips), 'trips')"
```

With a fault and its ground truth (when it started; when its DTC went pending, then confirmed):

```powershell
uv run python -c "from autocare_sim.run import simulate; from autocare_sim.faults import fault; import json; tl = simulate('toyota_corolla_1_8_2009', days=30, seed=1, faults=[fault('ageing_o2')]); print(json.dumps(tl.ground_truth()['faults'], indent=1))"
```

Nine faults in `simulator/faults/faults.yaml`; when the simulated ECU sets each DTC is in
`simulator/faults/dtc_rules.yaml` (both SEED, ADR-009).

Five car profiles in `simulator/profiles/` (SEED values for Wisdom to validate); drive
cycles and the Lagos schedule in `simulator/profiles/cycles.yaml`; numbers measured from
real datasets in `simulator/profiles/calibration.yaml` (written by `ml/notebooks/01_...`).

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
    jobs/       enqueue(), worker (embedded in the API), handlers (ADR-004, ADR-008)
    config/     consents.yaml (SEED - validate)
  tests/
  restfox/      Phase 1 request list + Postman/Restfox export
docs/           PRD, ADRs, plans, cost, deploy guide
ml/data_cards/  dataset documentation (data itself lives in DATASETS_DIR, never in git)
```

## Deploying

See [docs/deploy-railway.md](docs/deploy-railway.md). Expected cost: [docs/cost.md](docs/cost.md).
