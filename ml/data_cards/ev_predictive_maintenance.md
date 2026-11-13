# Data card: EV_Predictive_Maintenance_Dataset_15min (Kaggle, synthetic)

> **Use: PIPELINE PLUMBING TESTS ONLY.** Never used to judge model quality (PRD Section 8). Its EV features are out of v1 scope (PRD 12).

| Field | Value |
|---|---|
| Source | Kaggle (exact dataset URL and owner not recorded yet). ⚠️ **Wisdom: add the URL.** |
| Licence | ⚠️ **UNKNOWN.** Record it from the Kaggle page. |
| Local path | **Not found on this machine** (searched `C:\Users\Godswisdom` on 2026-11-13). The old notebook loaded `../bm_autocare/EV_Predictive_Maintenance_Dataset_15min(1).csv`. Leave the file where it is when it turns up; don't move it into `raw/` unless Wisdom says so. |
| Checksums | None yet (file not available). |

## Contents (from the old notebook export `autocare_progress_report.html`)

- 15-minute aggregated rows with a `Timestamp`.
- Features: `SoC`, `Battery_Voltage`, `Battery_Current`, `Battery_Temperature`, `Motor_Temperature`, `Motor_Torque`, `Motor_RPM`, `Motor_Vibration`, `Driving_Speed`, `Distance_Traveled`, `Idle_Time`, `Power_Consumption`, `Load_Weight`, `Reg_Brake_Efficiency`, `Brake_Pad_Wear`, `Suspension_Load`.
- Targets/labels: `Maintenance_Type`, `Failure_Probability`, `RUL`, `TTF`, `Component_Health_Score`.
- Row count, number of vehicles and sampling details: unknown until the file is available.

## OBD-II mapping

Almost none. `Driving_Speed` ≈ 0x0D. Everything else is EV-specific (motor, SoC, HV battery) or sensors that standard OBD-II doesn't expose (brake pad wear, suspension load, vibration).

## Allowed uses

- Exercising the ML pipeline code (load → feature table → split → train → report) before real indicators exist.
- Testing the `get_safe_features` leakage-exclusion helper.

## Limitations

- Synthetic. Labels are probably generated from the features, so high accuracy means nothing.
- EV only. No petrol engine signals.
- Unknown provenance and licence.
