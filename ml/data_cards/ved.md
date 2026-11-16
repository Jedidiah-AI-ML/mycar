# Data card: Vehicle Energy Dataset (VED)

| Field | Value |
|---|---|
| Source | https://github.com/gsoh/VED |
| Paper | G. Oh, D. J. LeBlanc, H. Peng, "Vehicle Energy Dataset (VED), A Large-scale Dataset for Vehicle Energy Consumption Research", IEEE T-ITS, 2020. doi:10.1109/TITS.2020.3035596 |
| Licence | **Apache License 2.0** (shown on the GitHub repo). Note: Apache-2.0 is a software licence applied here to data. Fine for research and portfolio use; cite the paper. |
| Local path | `$DATASETS_DIR/raw/ved/` |
| Checksums | `raw/ved/CHECKSUMS.txt` (SHA-256, 56 files) |
| Status | Already present on disk before Phase 0 (dynamic 7z already extracted). Moved into `raw/ved/` on 2026-11-13. Not re-downloaded. |

## Files

| Path | Count | Size |
|---|---|---|
| `dynamic/1st/VED_YYMMDD_week.csv` (2017-11-01 to 2018-03-28) | 22 | 1.46 GB (41 to 97 MB each) |
| `dynamic/2nd/VED_YYMMDD_week.csv` (2018-04-04 to 2018-11-07) | 32 | 1.74 GB (17 to 70 MB each) |
| `static/VED_Static_Data_ICE&HEV.xlsx` | 1 | 21 KB (357 vehicles) |
| `static/VED_Static_Data_PHEV&EV.xlsx` | 1 | 10 KB (27 vehicles) |

All 54 weekly files are present (Nov 2017 to Nov 2018), with no gaps.

## Contents (measured 2026-11-13)

- **22,436,808 rows, 384 vehicles, 32,552 trips** (`VehId`, `Trip`).
- Vehicle types (static): **264 ICE**, 93 HEV, 24 PHEV, 3 EV. Ann Arbor, Michigan, USA. Static fields: engine configuration and displacement (e.g. `4-FI 2.0L T/C`, `6-FI 3.6L`), transmission, drive wheels, weight class. Mostly `NO DATA` for class and transmission. **No make, model or year.**
- **Sampling:** irregular. Per-trip median step 900 ms, 10th percentile 100 ms, 90th percentile 1.9 s, with long gaps. Effectively about 1 Hz.
- `Timestamp(ms)` is relative to the trip start; `DayNum` is days since 2017-11-01 00:00 (fractional).
  **`DayNum` is UTC**, not Michigan time (notebook 01: read as local, the quietest hour would be 07:00; as UTC→Detroit it's 02:00, with peaks at 07:00 and 16:00–17:00).
- **VED holds only part of each car's trips:** median 1 trip and 5.2 km per driving day for the ICE cars. Per-day usage figures from VED understate real driving.

| Column | Non-null share | OBD-II PID |
|---|---|---|
| Vehicle Speed [km/h] | 100% | 0x0D |
| Engine RPM | 97.9% | 0x0C |
| MAF [g/s] | 81.4% | 0x10 |
| Absolute Load [%] | 73.4% | 0x43 (absolute load, *not* 0x04 calculated load) |
| OAT [°C] | 58.2% | 0x46 (ambient) |
| STFT Bank 1 / 2 [%] | 78.8% / 30.3% | 0x06 / 0x08 |
| LTFT Bank 1 / 2 [%] | 67.6% / 19.2% | 0x07 / 0x09 |
| Fuel Rate [L/h] | 4.0% | 0x5E |
| Latitude / Longitude | 100% | GNSS (not OBD) |
| AC / heater power, HV battery current/SOC/voltage | ≤17% | not OBD-II standard (HEV/EV only) |

**Missing for AutoCare:** coolant temp (0x05), intake air temp (0x0F), MAP (0x0B), throttle (0x11), O2 sensors, control-module voltage (0x42), battery voltage, DTCs, Mode 06, odometer.

## How AutoCare uses it (PRD Section 8)

- **ICE vehicles only** (264). Drop HEV/PHEV/EV by joining on `VehId` with the static file.
- Learn "normal" distributions and between-car variance for fuel-trim, MAF-vs-RPM/load and speed/RPM indicators. These feed the population priors for cold start.
- Usage patterns: trips per day, trip length, idle share, km/day (Layer 1 forecasting; Lagos traffic will look different, so treat this as a shape prior only).
- Distance logic: compare integrated speed with GPS track distance, and handle gaps.
- Base traces for fault injection (vacuum leak, dirty MAF) on real data.

## Limitations

- US fleet, mostly newer cars, temperate climate and good fuel. Not representative of Nigerian imports, heat or fuel quality.
- No labels, no DTCs and no health ground truth. Unsupervised use only.
- Static data lacks make/model/year, so make/model priors can only be grouped by engine configuration and displacement.
- GPS has been privacy-processed by the authors (per the paper). Don't use it for fine-grained routing.
- Signal availability varies by vehicle. Filter per vehicle before computing indicators.
- **Split rule:** split by vehicle (or by time within a vehicle), never randomly across rows.
