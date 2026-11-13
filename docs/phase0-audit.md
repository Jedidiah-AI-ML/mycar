# Phase 0 audit (2026-11-13)

## 0. Where things are

| Expected (from the brief) | Found |
|---|---|
| Repo at `C:\Users\JD\Desktop\bm_projects\bm_autocare` | **Doesn't exist on this machine.** Wisdom decided: use `C:\Users\Godswisdom\mycar` (was empty) as the AutoCare repo. |
| Phase 1 scaffold: FastAPI, `/health` + `/health/ready`, multi-stage Dockerfile on Railway `PORT`, Railway deploy, mock OBD simulator, uv + pyproject, Python 3.12 | **Not found.** Closest match is `C:\Users\Godswisdom\vpm-backend` (July 2025): mostly empty placeholder files. Audited below as "the existing scaffold". |
| `traiing.ipynb`, `traiing-Copy1.ipynb` | **Not found.** Reviewed `Downloads\autocare_progress_report.html` (an HTML export of that notebook) instead. |
| EV dataset CSV | **Not found** on this machine. |
| Datasets folder | `C:\Users\Godswisdom\Downloads\datasets` with VED, Barreto and KIT already downloaded. |

Unrelated folder seen during the search: `C:\Users\Godswisdom\backend` is a "tutor-backend" project (Clerk, LiveKit, arq/Redis). Not AutoCare; ignored.

Tooling on this machine: uv 0.9.26, git, Docker Desktop, system Python 3.14 (uv will install 3.12 for the project).

## 1. Audit table: `vpm-backend` mapped to the PRD

| Item | Verdict | Reason |
|---|---|---|
| `app/main.py` (FastAPI app, CORS, router include) | **Keep with changes** | Good shape. Read CORS origins from settings instead of hard-coding them, rename to AutoCare, mount health at `/health` (PRD) not `/api/v1/health`. |
| `app/api/v1/health.py` (`/health`, `/health/detailed`) | **Keep with changes** | Keep `/health`. Replace `/health/detailed` with `/health/ready` (checks DB connection and migration head). Drop the Redis check. |
| `app/core/config.py` (pydantic-settings, `lru_cache`) | **Keep with changes** | Switch to Pydantic v2 `SettingsConfigDict` (`class Config` is deprecated). Drop `REDIS_URL` and `OPENAI_API_KEY`. Add JWT, CORS and `DATASETS_DIR`. |
| 34 empty `.py` placeholders (`api/v1/*`, `db/models/*`, `schemas/*`, `services/*`, `tasks/processing.py`) | **Not needed** | All zero bytes. The PRD Section 14 layout replaces them. |
| `app/simulator/mock_obd.py` | **Not needed (empty)** | 0 bytes. **The "existing mock OBD simulator" doesn't exist**, so Phase 2 builds it from scratch. |
| `bridge/obd_bridge.py` | **Not needed: conflict** | Empty, but the name implies a laptop/phone ELM327 bridge. Out of v1 scope (ADR-001). |
| `Dockerfile` | **Replace** | Python 3.11, single stage, pip. Copies `requirements.txt` but the file is named `requirement.txt`, **so the build fails**. Hard-codes port 8000 and ignores Railway's `PORT`. |
| `docker-compose.yml` | **Replace: conflict** | Has a **Redis** service and a commented **TimescaleDB** service. Phase 1 keeps only `postgres:16`. |
| `requirement.txt` | **Replace: conflict** | Pins **redis + celery** (ADR-004), `openai` (use LiteLLM only, Phase 4), old pins. Move to `pyproject.toml` + `uv.lock`. Keep `obd` (python-OBD) in mind for Phase 2 virtual-ELM327 tests. |
| `.env` / `.env.example` | **Replace** | `REDIS_URL`, `OPENAI_API_KEY`. New `.env.example` written in `mycar`. |
| `.gitignore` | **Replace** | Doesn't exclude data files. New one written. |
| `tests/test_health.py` | **Not needed (empty)** | No tests exist yet. |
| `int.ipynb`, `notebooks/` | **Not needed** | Empty. |
| `README.md` | **Not needed** | Empty. |
| Railway config | **Missing** | No `railway.toml`; not clear whether a Railway project exists (open question). |
| git | **Missing** | `vpm-backend` isn't a git repo and nor is `mycar`. |

**Nothing in `vpm-backend` was moved, edited or deleted.** Proposal: copy the three useful files (`main.py`, `health.py`, `config.py`) into `mycar/backend/app/` in Phase 1 step 1 and adapt them. Leave `vpm-backend` as an archive.

## 2. Conflicts with the PRD

1. **Redis + Celery** in `requirement.txt`, plus a Redis service in compose → ADR-004 (Postgres queue).
2. **TimescaleDB** commented in compose and implied by comments → ADR-003.
3. **Bluetooth/ELM327 bridge** (`bridge/obd_bridge.py`) → ADR-001.
4. **`openai` SDK** directly instead of LiteLLM → PRD 7.
5. **Old EV-based supervised classifier** (notebook) → v1 is unsupervised + rules; EV is out of scope; the synthetic EV data is plumbing-only.
6. **Python 3.11** vs PRD's 3.12. **pip/requirements** vs uv.
7. **The PRD assumes a working, deployed scaffold and simulator.** Neither exists, so Phase 1 starts from near zero and Phase 2's simulator work is "build", not "extend".
8. The brief's repo path and datasets placeholder didn't match this machine. Resolved by Wisdom's answers (use `mycar`; datasets in `Downloads\datasets`).

## 3. Old notebook review (`autocare_progress_report.html`)

What it does: loads `EV_Predictive_Maintenance_Dataset_15min(1).csv`, label-encodes `Maintenance_Type`, adds hour/day-of-week, engineers 9 ratio features, defines `get_safe_features(df, target)` with a per-target leakage exclusion list, then runs a RandomForest grid search on a random stratified split. **Cells 5 to 7 were never executed** (`In [ ]`), so there are no results to reproduce.

**Reuse**
- **Leakage-safe feature selection idea.** Declare, per target, which columns are labels or label-derived and exclude them automatically. It already caught that `Health_Stress_Ratio` is built from a label (`Component_Health_Score`). This becomes a small helper in `ml/` with a unit test, and later a rule for Layer 5 (e.g. never use post-repair readings to predict the repair).
- `safe_ratio`-style feature helpers (division with epsilon) for indicators like MAF-per-displacement.
- Printing what was excluded and why. That habit is worth keeping for every evaluation notebook.

**Don't reuse**
- **EV features** (SoC, motor torque/RPM/temperature, HV battery, regen braking): out of scope.
- **Random row split** (`train_test_split(..., stratify=y)`) on time-series rows: neighbouring 15-minute rows from the same vehicle leak into the test set. Use vehicle- or time-based splits (PRD 8).
- **Accuracy as the metric**: v1 reports detection rate, lead time and false alarms per 1,000 km.
- **Supervised classifier on synthetic labels**: v1 Layer 3 is unsupervised.
- `Hour`/`DayOfWeek` as predictors (spurious on synthetic data).

**Bugs noticed (FYI, not fixing):** `autocare_sample.drop('Timestamp')` is missing `axis=1`/`columns=` and would raise `KeyError`. The 1,000-row sample is built but the model trains on the full frame. The target passed in (`Maintenance_Type_Encoded`) isn't a key in the leakage dict, so **no exclusions are applied** for it.
