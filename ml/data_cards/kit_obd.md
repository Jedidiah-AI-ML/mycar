# Data card: KIT Automotive OBD-II Dataset

| Field | Value |
|---|---|
| Source | https://publikationen.bibliothek.kit.edu/1000085073 |
| DOI | 10.5445/IR/1000085073 |
| Author / year | Marc Weber, Karlsruher Institut für Technologie (KIT), 2019 |
| Licence | **Creative Commons Attribution 4.0 International (CC BY 4.0)**, per the DataCite record ("Creative Commons Namensnennung 4.0 International"). Attribution required. |
| Local path | `$DATASETS_DIR/raw/kit_obd/` |
| Checksums | `raw/kit_obd/CHECKSUMS.txt` (SHA-256, 81 files) |
| Status | Already on disk (`OBD-II-Dataset/`). Moved into `raw/kit_obd/` on 2026-11-13. The KIT site couldn't be reached from here to cross-check the file list, so confirm the count (81 CSVs) against the landing page. |

## Files

81 CSVs, 172.8 MB total, 2,693,824 rows. Named `YYYY-MM-DD_Seat_Leon_<from>_<to>_<condition>.csv`, recorded July to August 2017.

- `from`/`to` look like place codes (KA = Karlsruhe, S = Stuttgart; RT, CW and BB probably other towns in Baden-Württemberg). *Not confirmed.*
- Conditions (German): `Normal` (55), `Frei` = free-flowing (12), `Stau` = traffic jam (10), plus single files tagged `Vollbremsung` (emergency braking), `Beschleunigung` (acceleration), `Glatteis` (black ice) and `Messfehler` (**measurement error**: exclude it).

## Contents (measured 2026-11-13)

- **One vehicle** (Seat Leon; model year and engine aren't stated in the files). 81 trips, median 50 min (12 to 135 min).
- **Sampling:** a row every about 90 ms, but each row updates only **one** PID in round-robin order (the other columns repeat the last value). Each signal effectively refreshes at about 1 Hz. Forward-filled at the start of each trip.
- Encoding: header is UTF-8 `°C` in 42 files and double-encoded (`Ã‚Â°C`) in 39 files. Normalise headers on load.

| Column | OBD-II PID |
|---|---|
| Engine Coolant Temperature [°C] | 0x05 |
| Intake Manifold Absolute Pressure [kPa] | 0x0B |
| Engine RPM | 0x0C |
| Vehicle Speed Sensor [km/h] | 0x0D |
| Intake Air Temperature [°C] | 0x0F |
| Air Flow Rate from MAF [g/s] | 0x10 |
| Absolute Throttle Position [%] | 0x11 |
| Ambient Air Temperature [°C] | 0x46 |
| Accelerator Pedal Position D / E [%] | 0x49 / 0x4A |

**Missing:** fuel trims, O2, load, voltage, DTCs.

## How AutoCare uses it (PRD Section 8)

- **Supplementary simulator calibration:** coolant warm-up curves (cold starts are visible at the beginning of trips), MAF vs RPM/MAP, and a realistic round-robin polling pattern similar to what the Freematics firmware will do.
- Stop-and-go (`Stau`) vs free-flowing traces as references for drive-cycle shapes.
- Warm-up-time indicator prototype (coolant + intake air temperature at start).

## Limitations

- One European car. No between-vehicle variation.
- Temperate summer climate. Warm-up times won't match Lagos heat.
- No health labels or DTCs.
- Stale (forward-filled) values must be de-duplicated before computing rates or derivatives.
