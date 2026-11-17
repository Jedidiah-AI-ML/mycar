# ADR-007: Simulator and ML notebooks as separate uv projects

- **Status:** Accepted (2026-11-17, Phase 2 step 3). Refines Phase 2 decision Q1.
- **Date:** 2026-11-17

## Context

Q1 approved a "uv workspace" with `backend` and `simulator`. A uv workspace has a single
lockfile at the repo root. But Railway (and later Render) builds the API from `backend/`
only (root directory `/backend`, `uv sync --frozen` against `backend/uv.lock`). Moving
the lock to the root would mean changing the Docker build context and the hosting
settings that have just been made to work.

## Decision

Three independent uv projects in one repo, each with its own `pyproject.toml` and
`uv.lock`:

| Project | Holds | Depends on |
|---|---|---|
| `backend/` | API, gateway, jobs (deployed) | nothing from the others |
| `simulator/` | car + device simulator (`autocare_sim`) | only the generated `contracts/` files (JSON) |
| `ml/` | notebooks | `DATASETS_DIR`; writes `simulator/profiles/calibration.yaml` |

They share data, not imports. The contract between them is `contracts/` (ADR-006) and
the calibration YAML. When a backend test needs simulator output (Phase 2 step 10, the
gate run), the gate script calls the simulator as a separate process, or adds
`autocare-sim` as a backend *dev* path dependency. It's never a runtime dependency.

## Consequences

- **Good:** the API image stays small (no numpy, scipy, pandas). Hosting config is unchanged.
- **Good:** each part can be run and tested alone (`cd simulator; uv run pytest`).
- **Cost:** three lockfiles to keep current. Shared versions (numpy in ml and simulator)
  can drift; acceptable for a solo project.
