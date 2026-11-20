# Phase 3, step 1: distance and usage

From `ml/notebooks/02_distance_and_usage.ipynb`. Numbers: `ml/reports/phase3_distance.yaml`. All decisions are **SEED - validate** (measured on Michigan cars and the simulator, not Lagos cars).

## Distance

- VED clean trips: GPS distance / speed integral = {'p10': 0.984, 'p50': 1.007, 'p90': 1.036, 'n': 17068}.
- Per-car speed-sensor bias vs GPS (%): {'p5': -1.579, 'p50': 0.675, 'p95': 3.293, 'n': 207}.
- Polling slower than 1 Hz (|error| %, p50/p90/p99): {2: {'p50': 0.084, 'p90': 0.251, 'p99': 0.398, 'n': 400.0}, 5: {'p50': 0.241, 'p90': 0.743, 'p99': 1.533, 'n': 400.0}, 10: {'p50': 0.617, 'p90': 1.718, 'p99': 3.64, 'n': 400.0}}.
- Simulated Lagos trips, device 1 Hz integral vs true distance (%): {'p5': -0.071, 'p50': -0.0, 'p95': 0.093, 'n': 76}.
- **Gap policy** (whichever method is better at each hole length): bridge holes up to 10 s; longer holes use the GPS chord × detour factor {2: 0.997, 5: 1.0, 10: 1.005, 20: 1.007, 30: 1.017, 60: 1.035, 120: 1.077, 300: 1.147, 600: 1.251} when fixes exist. Without GPS: bridge holes up to 120 s, trip-average speed beyond; trip distance marked partial when a hole exceeds 20 s.
- Median |error| by method and hole length (% of the hole's distance, test cars): {2: {'bridge': 0.4, 'chord': 3.8, 'chord_x_detour': 3.8, 'mean_speed': 38.4}, 5: {'bridge': 1.2, 'chord': 3.6, 'chord_x_detour': 3.6, 'mean_speed': 39.6}, 10: {'bridge': 3.3, 'chord': 4.0, 'chord_x_detour': 4.1, 'mean_speed': 39.1}, 20: {'bridge': 8.1, 'chord': 3.7, 'chord_x_detour': 3.8, 'mean_speed': 38.0}, 30: {'bridge': 14.0, 'chord': 4.4, 'chord_x_detour': 4.5, 'mean_speed': 37.6}, 60: {'bridge': 21.0, 'chord': 5.2, 'chord_x_detour': 6.0, 'mean_speed': 35.8}, 120: {'bridge': 29.4, 'chord': 8.8, 'chord_x_detour': 8.7, 'mean_speed': 29.4}, 300: {'bridge': 34.5, 'chord': 12.6, 'chord_x_detour': 11.0, 'mean_speed': 21.5}, 600: {'bridge': 42.7, 'chord': 19.3, 'chord_x_detour': 11.1, 'mean_speed': 6.6}}.
- **Flag a trip** when GPS and speed distances differ by more than 13 % (99th percentile of clean VED trips).

## km per day

- Median error of next-30-day km: {'ved': {'mean60': 45.5, 'ewma14': 46.6, 'mean30': 51.6, 'baseline_other_cars': 57.5, 'mean7': 79.7}, 'simulator': {'baseline_other_cars': 12.8, 'mean30': 13.5, 'mean60': 13.9, 'ewma14': 16.1, 'mean7': 21.9}}.
- Best on VED: `mean60`; best on the simulator: `baseline_other_cars`.
- On VED (real cars) the trailing **60**-day mean beat the 30-day mean by about 6 points; both beat the trivial baseline. The simulator can't separate them: its days are statistically identical, so the other cars' average wins trivially. **Chosen: trailing 30-day mean, as the PRD specifies; the 60-day evidence is raised with Wisdom** and to be re-checked on field data (Phase 9).
- Errors of 45–50 % on VED are mostly VED's partial logging (whole days missing), not the method.
- Cold start (simulator, % error with d days of data): {1: {'blend': 6.2, 'own_data': 43.0, 'prior': 6.2}, 3: {'blend': 15.6, 'own_data': 43.0, 'prior': 6.2}, 7: {'blend': 8.4, 'own_data': 8.8, 'prior': 6.2}, 14: {'blend': 21.9, 'own_data': 28.0, 'prior': 6.2}}. The simulator's days are drawn from one Lagos schedule, so the prior is close to right by construction; this checks the prior's level and that blending is safe, not how fast a real car's own pattern emerges.
- **Lagos prior: 30 km/day**, counted as 7 days of data and blended with the car's own data.

## Odometer mismatch

- Flag when |entered − estimate| > max(500 km, 5 % of km since the last anchor). 95 % of VED cars' speed sensors are within ±3.33 % of GPS.

## Limits

- VED is Michigan driving logged by a research logger; Lagos usage and road shapes differ. The detour factors and km/day levels are to be re-checked on the field-test car (Phase 9).
- VED per-day km is understated (partial logging), so VED informs how *steady* usage is, not how much.
