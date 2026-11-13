# AutoCare: working rules

The source of truth is `docs/PRD.md`. Read it before any work; Section 0 has the full rules.

- **Plan, then wait.** Every phase starts with a written plan in `docs/plans/`. No code until Wisdom approves it.
- **Test gates are mandatory**: pytest for code, a Restfox request list for endpoints, a notebook result for ML. Never move on from a red or skipped gate.
- **ML is notebook-first** (`ml/notebooks/`); promoted code must reproduce the notebook's numbers.
- **Automotive facts you're unsure of** go in config marked `# SEED - validate` and get listed for Wisdom.
- **Non-obvious decisions** get an ADR in `docs/decisions/`.
- **Explain new techniques** in 3 to 6 plain-English lines.
- **Stay in scope** (PRD Section 12): no mobile app, Bluetooth, TimescaleDB, Redis or Celery.
- Datasets live outside the repo at `DATASETS_DIR` (`raw/` is read-only). Never commit data.
- Wisdom is on Windows. Give shell commands in PowerShell form.
- Keep costs down; flag anything that adds a monthly bill before adding it.
