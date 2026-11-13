# Data card: Barreto OBD-II datasets (OBDdatasets / obdii-ds3)

| Field | Value |
|---|---|
| Source | https://github.com/cephasax/OBDdatasets ; Kaggle mirror https://www.kaggle.com/datasets/cephasax/obdii-ds3 |
| Author | Cephas Barreto, Master's research, UFRN (Brazil). Thesis (Portuguese): https://repositorio.ufrn.br/jspui/handle/123456789/26017 |
| Licence | ⚠️ **UNCLEAR.** The Kaggle metadata says **"CC0: Public Domain"**, but the GitHub repo has **no licence file**. Treat as CC0 based on the Kaggle listing, credit the author anyway, and ask Wisdom to accept this before anything is published that derives from it. |
| Local path | `$DATASETS_DIR/raw/barreto_obd/` |
| Checksums | `raw/barreto_obd/CHECKSUMS.txt` (SHA-256, 6 files) |
| Status | Already on disk (Kaggle total 17,629,447 bytes, which matches the local files). Moved into `raw/barreto_obd/` on 2026-11-13. |

## Files

| File | Rows | Size |
|---|---|---|
| `exp1_14drivers_14cars_dailyRoutes.csv` (+ `.xlsx` duplicate) | 60,439 | 7.1 MB (+7.3 MB) |
| `exp2_19drivers_1car_1route.csv` (+ `.xlsx`) | 8,261 | 1.5 MB (+1.2 MB) |
| `exp3_4drivers_1car_1route.csv` (+ `.xlsx`) | 1,743 | 0.3 MB (+0.3 MB) |

The `.xlsx` files appear to duplicate the CSVs. Use the CSVs.

## Contents (measured 2026-11-13)

- **exp1:** 14 cars, 14 drivers, daily routes. Makes/models: Chevrolet Agile 2011, Toyota **Corolla 2009**, VW Polo 2006, Peugeot 208 2015, Fiat Palio, Renault Duster, Citroën C3, Nissan Versa 2016, Honda Fit 2005, Ford Focus 2003, plus a few others (model years 2003 to 2016). About 21% of rows are entirely blank.
- **exp2:** 1 car, 19 drivers, same route. **exp3:** 1 car, 4 drivers, same route.
- **Sampling:** about 0.1 to 0.25 Hz (exp1 median step **8 s**, 10th to 90th percentile 4 to 12 s). Slow, like a phone ELM327 logger.
- **Trouble codes: yes, in exp1.** `TROUBLE_CODES` is non-null on 11,925 rows: `P0133` (O2 sensor slow response, B1S1) on 6,070 rows, `C0300` on 5,673, and a few multi-code strings (P0078/P0079/P007E/P2004/P2036/P3000…). These look like they may be unparsed concatenations or manufacturer codes, so validate them. `DTC_NUMBER` holds the Mode 01 PID 0x01 status text ("MIL is OFF1 codes"). exp2/exp3 contain raw hex in that column.
- Values need cleaning: locale decimals (`"4,49"`), units inside the values (`82C`, `2124RPM`), percent strings, and mangled coordinates/timestamps in exp2/exp3 (`"1,51336E+12"`, `-58.321.307`).

| Column | OBD-II PID |
|---|---|
| ENGINE_COOLANT_TEMP | 0x05 |
| ENGINE_LOAD | 0x04 |
| SHORT/LONG TERM FUEL TRIM BANK 1/2 | 0x06 to 0x09 |
| INTAKE_MANIFOLD_PRESSURE | 0x0B |
| ENGINE_RPM | 0x0C |
| SPEED | 0x0D |
| TIMING_ADVANCE | 0x0E |
| AIR_INTAKE_TEMP | 0x0F |
| MAF | 0x10 |
| THROTTLE_POS | 0x11 |
| ENGINE_RUNTIME | 0x1F |
| FUEL_LEVEL | 0x2F |
| BAROMETRIC_PRESSURE | 0x33 |
| EQUIV_RATIO | 0x44 |
| AMBIENT_AIR_TEMP | 0x46 |
| DTC_NUMBER | 0x01 (status) |
| TROUBLE_CODES | Mode 03 |

## How AutoCare uses it (PRD Section 8)

- **Simulator realism:** real ELM327 polling rates, dropouts, missing PIDs per car, and noise levels on older cars (2003 to 2016, close to the Nigerian import fleet).
- A Toyota Corolla 2009 trace for sanity-checking the Corolla 1.8L simulator profile.
- The rows with a P0133 code are a small real-world check for the "O2 sensor ageing" indicator (see limitations).

## Limitations

- Brazilian **flex-fuel (ethanol blends)**. Fuel trims and equivalence ratios aren't comparable with petrol-only cars. Don't use for fuel-trim priors.
- Very slow sampling. No cranking-voltage, O2-switching-rate or idle-stability indicators possible.
- Small: 14 cars, short collection period.
- Messy encoding needs a dedicated cleaning notebook (`ml/notebooks/01_barreto_clean.ipynb`, Phase 5).
- Licence unclear (see above).
