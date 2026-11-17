"""Engine and vehicle physics, one second at a time.

Given a speed trace, this produces what the car's computers would report: RPM, load,
airflow, temperatures, fuel trims, sensor voltages, battery voltage, fuel level, plus
the device's accelerometer and GNSS. Relationships are physical, so the signals stay
consistent with each other (e.g. MAF follows RPM × load × displacement, which matched
real VED engines to within 0.4 % in the calibration notebook).

Faults (step 4) never special-case this code: they change the `Health` parameters over
days and kilometres, and the physics turns that into realistic symptoms.
"""

import math
from dataclasses import dataclass, field

import numpy as np

from autocare_sim.config import VehicleProfile, calibration, catalogue, normal_from_pcts

RHO_AIR = 1.184  # g/L at 25 °C (same constant as the calibration notebook)
AFR_STOICH = 14.7  # petrol: grams of air per gram of fuel
FUEL_G_PER_L = 745.0
G = 9.81
C_RR = 0.012  # rolling resistance coefficient


@dataclass
class Health:
    """Parameters that faults change. Defaults describe a healthy car."""

    battery_rest_v: float = 12.65  # open-circuit voltage, engine off, rested
    battery_crank_drop_v: float = 2.4  # how far voltage dips while cranking
    charging_v: float = 14.10  # alternator regulation set-point
    charging_sag_v_per_load: float = 0.0  # failing alternator: voltage falls with load
    thermostat_stuck_open: float = 0.0  # 0 = fine, 1 = fully stuck open
    operating_offset_c: float = 0.0  # e.g. low coolant runs hotter
    cooling_capacity: float = 1.0  # < 1: radiator/fan/coolant can't shed heat at idle
    vacuum_leak_gs: float = 0.0  # unmetered air after the MAF, g/s
    maf_scale: float = 1.0  # dirty MAF reads this fraction of true airflow
    o2_response_s: float = 0.1  # upstream sensor time constant (ageing: slower)
    misfire_rate: float = 0.0  # fraction of firing events lost on the weak cylinder
    catalyst_efficiency: float = 1.0  # 1 = healthy, falls as the catalyst degrades


@dataclass
class CarState:
    """What carries over from one trip to the next."""

    coolant_c: float
    fuel_l: float
    ltft_b1: float
    ltft_b2: float
    trim_offset_b1: float  # this car's normal fuel-trim offset (between-car spread)
    trim_offset_b2: float
    maf_ratio: float  # this car's MAF reading vs the physics formula
    warmups_since_clear: int = 0
    dist_since_clear_km: float = 0.0
    odometer_km: float = 0.0
    lat: float = 6.5244
    lon: float = 3.3792
    heading_rad: float = 0.0
    iat_c: float = 30.0
    lambda_b1: float = 1.0

    @classmethod
    def new(cls, profile: VehicleProfile, rng: np.random.Generator, lat: float, lon: float):
        cal = calibration()
        m, sd = normal_from_pcts(cal["fuel_trim_ved"]["ltft_b1_pct_per_car"])
        off = float(np.clip(rng.normal(m, sd * 0.6), -6, 8))
        mr_m, mr_sd = normal_from_pcts(cal["airflow"]["maf_over_predicted_per_car"])
        return cls(
            coolant_c=28.0,
            fuel_l=profile.fuel_tank_l * float(rng.uniform(0.4, 0.9)),
            ltft_b1=off,
            ltft_b2=off + float(rng.normal(0, 1.0)),
            trim_offset_b1=off,
            trim_offset_b2=off + float(rng.normal(0, 1.0)),
            maf_ratio=float(np.clip(rng.normal(mr_m, mr_sd), 0.93, 1.07)),
            lat=lat,
            lon=lon,
            heading_rad=float(rng.uniform(0, 2 * math.pi)),
        )


@dataclass
class TripOutput:
    signals: dict[str, np.ndarray]  # true values at 1 Hz (not yet rounded or filtered)
    lat: np.ndarray
    lon: np.ndarray
    gnss_speed: np.ndarray
    crank_volts: np.ndarray  # cranking trace at CRANK_HZ
    resting_v: float  # battery just before cranking
    distance_km: float
    extras: dict = field(default_factory=dict)


CRANK_HZ = 50


def _crank_trace(rest_v: float, health: Health, cold: bool, rng) -> np.ndarray:
    """Battery voltage while the starter turns: a sharp dip, ripple, then charging."""
    n = int(1.6 * CRANK_HZ)
    t = np.arange(n) / CRANK_HZ
    drop = health.battery_crank_drop_v + (0.3 if cold else 0.0)
    v = np.full(n, rest_v)
    crank = (t >= 0.1) & (t < 1.1)
    v[crank] = rest_v - drop + 0.25 * np.sin(2 * math.pi * 9 * t[crank])  # compression ripple
    v[(t >= 0.1) & (t < 0.15)] = rest_v - drop - 0.6  # inrush spike
    after = t >= 1.1
    v[after] = health.charging_v - (health.charging_v - rest_v) * np.exp(-(t[after] - 1.1) / 0.2)
    return v + rng.normal(0, 0.03, n)


def simulate_trip(
    profile: VehicleProfile,
    speed_kmh: np.ndarray,
    ambient_c: float,
    baro_kpa: float,
    parked_s: float,
    state: CarState,
    health: Health,
    rng: np.random.Generator,
) -> TripOutput:
    p, h, st = profile, health, state
    n = len(speed_kmh)
    cal = calibration()
    tau_warm = float(cal["thermal_kit"]["warmup_tau_s"]["p50"])  # KIT: ~437 s

    out = {k: np.zeros(n) for k in (
        "rpm", "speed", "load", "throttle", "coolant", "iat", "stft_b1", "ltft_b1", "stft_b2",
        "ltft_b2", "maf", "map", "o2_b1s1_v", "o2_b1s2_v", "lambda_b1s1", "ecu_voltage",
        "fuel_level", "ambient", "baro", "abs_load", "timing_adv", "batt_v", "acc_x", "acc_y",
        "acc_z", "runtime_since_start")}  # fmt: skip
    lat, lon, gspd = np.zeros(n), np.zeros(n), np.zeros(n)
    closed_loop = np.zeros(n, dtype=bool)
    fuel_cut = np.zeros(n, dtype=bool)
    gear_arr = np.zeros(n, dtype=int)

    # Battery before cranking: rested value plus surface charge after a short stop.
    resting_v = h.battery_rest_v + 0.25 * math.exp(-parked_s / 3600)
    cold_start = st.coolant_c < 40
    crank = _crank_trace(resting_v, h, cold_start, rng)
    coolant_start = st.coolant_c

    gear = 0
    stft_noise = [0.0, 0.0]
    rpm_prev = p.idle_rpm_cold if cold_start else p.idle_rpm_warm
    misfire_events = 0
    weak_fan_phase = rng.uniform(0, 90)
    dist_km = 0.0

    for i in range(n):
        v = float(speed_kmh[i])
        a = (v - float(speed_kmh[i - 1])) / 3.6 if i else 0.0  # m/s²
        v_ms = v / 3.6
        T = st.coolant_c
        warmth = min(max((T - 20) / 45, 0.0), 1.0)  # 0 cold .. 1 warm

        # ---- gear (automatic) ------------------------------------------------------
        if v < 1:
            gear = 0
        else:
            while gear < len(p.upshift_kmh) and v > p.upshift_kmh[gear] * (1.0 + 0.25 * (a > 0.8)):
                gear += 1
            while gear > 0 and v < p.upshift_kmh[gear - 1] * 0.75:
                gear -= 1
        gear_arr[i] = gear

        # ---- road load -> demand ---------------------------------------------------
        force = p.mass_kg * a + p.mass_kg * G * C_RR * (v > 0.5) + 0.5 * 1.2 * p.cd_a_m2 * v_ms**2
        p_wheel = max(force * v_ms, 0.0)
        is_cut = v > 20 and a < -0.4  # deceleration fuel cut-off

        # ---- engine speed ----------------------------------------------------------
        idle = p.idle_rpm_cold + (p.idle_rpm_warm - p.idle_rpm_cold) * min(max((T - 20) / 40, 0), 1)
        if v < 1:
            rpm_target = idle
        else:
            locked = v >= p.lockup_kmh and gear >= 2
            slip = 0.0 if locked else 0.35 * math.exp(-v / 15) + 0.04
            rpm_target = max(v * p.rpm_per_kmh(gear) * (1 + slip), idle)
        rpm = 0.5 * rpm_target + 0.5 * rpm_prev if v < 1 else rpm_target
        rpm_prev = rpm
        p_avail = p.torque_nm * rpm * 2 * math.pi / 60 * 0.9
        p_avail = min(p_avail, p.power_kw * 1000)
        demand = 0.0 if is_cut else min(p_wheel / max(p_avail * 0.85, 1.0), 1.0)

        # Misfire: the weak cylinder drops firing events; at idle the crank slows each time.
        noise_sd = 15 if v < 1 else 6
        if h.misfire_rate > 0 and rng.random() < h.misfire_rate * (1.5 if v < 1 else 0.6):
            misfire_events += 1
            rpm -= rng.uniform(25, 70) if v < 1 else rng.uniform(5, 20)
        rpm = max(rpm + rng.normal(0, noise_sd), 0.0)

        # ---- manifold, load, airflow ----------------------------------------------
        frac = 0.20 if is_cut else min(0.28 + 0.70 * demand + 0.04 * (1 - warmth), 0.99)
        map_kpa = baro_kpa * frac + rng.normal(0, 0.6)
        abs_load = map_kpa / baro_kpa * 100 * 0.92
        metered_gs = abs_load / 100 * RHO_AIR * p.displacement_l * rpm / 120 * st.maf_ratio
        maf_read = metered_gs * h.maf_scale * (1 + rng.normal(0, 0.01))
        calc_load = (
            8.0 + rng.normal(0, 1)
            if is_cut
            else p.idle_calc_load_pct * (0.9 + 0.2 * (1 - warmth))
            + (95 - p.idle_calc_load_pct) * demand**0.8
        )

        # ---- closed-loop fuelling and trims -----------------------------------------
        # The ECU meters fuel from the MAF reading. Unmetered air (vacuum leak) or a
        # MAF that under-reads leaves the mixture lean; the ECU adds fuel to fix it, and
        # that correction is what the fuel trims show.
        lean_error_pct = (h.vacuum_leak_gs / max(metered_gs, 0.5) + (1 / h.maf_scale - 1)) * 100
        cl = (T >= 55) and not is_cut and demand < 0.92
        closed_loop[i], fuel_cut[i] = cl, is_cut
        for b in (0, 1):
            ltft = st.ltft_b1 if b == 0 else st.ltft_b2
            offset = st.trim_offset_b1 if b == 0 else st.trim_offset_b2
            need = offset + lean_error_pct
            stft_noise[b] = 0.8 * stft_noise[b] + rng.normal(0, 1.0)
            if cl:
                ltft += (need - ltft) / 900  # long-term learning: ~15 min to adapt
                stft = np.clip(need - ltft, -25, 25) + stft_noise[b]
            else:
                stft = 0.0
            if b == 0:
                st.ltft_b1, out["stft_b1"][i], out["ltft_b1"][i] = ltft, stft, ltft
            else:
                st.ltft_b2, out["stft_b2"][i], out["ltft_b2"][i] = ltft, stft, ltft

        # ---- exhaust sensors -------------------------------------------------------
        if is_cut:
            lam_target = 1.99
        elif not cl:
            lam_target = 0.88 if demand >= 0.92 else 0.93  # enrichment / cold
        else:
            lam_target = 1.0 + rng.normal(0, 0.008) + 0.004 * math.sin(i * 2 * math.pi / 1.4)
        lam_target += h.misfire_rate * rng.uniform(0, 0.6)  # unburnt O2 reads lean
        k = 1 - math.exp(-1.0 / max(h.o2_response_s, 0.05))
        st.lambda_b1 += (lam_target - st.lambda_b1) * k
        out["lambda_b1s1"][i] = min(st.lambda_b1, 1.999)
        # Narrow-band upstream voltage (only reported by cars without an A/F sensor).
        out["o2_b1s1_v"][i] = 0.1 if st.lambda_b1 > 1.0 else 0.8
        if is_cut:
            down = 0.05
        elif not cl:
            down = 0.78
        else:
            # A healthy catalyst stores oxygen and smooths the downstream sensor flat; a
            # worn one lets the upstream switching through.
            swing = (1 - h.catalyst_efficiency) * 0.4 * math.sin(i * 2 * math.pi / 1.4)
            down = 0.66 + swing + rng.normal(0, 0.02)
        out["o2_b1s2_v"][i] = min(max(down, 0.0), 1.1)

        # ---- temperatures -------------------------------------------------------
        op = p.operating_c + h.operating_offset_c
        if v < 5:
            op += max(ambient_c - 25, 0) * 0.25 / h.cooling_capacity  # heat soak in traffic
            op += (1 / h.cooling_capacity - 1) * 18  # weak cooling shows at idle first
            op += 1.5 * math.sin(2 * math.pi * (i + weak_fan_phase) / 90)  # fan cycling
        if h.thermostat_stuck_open > 0:
            airflow = 0.4 + 0.6 * min(v / 60, 1.0)
            op -= h.thermostat_stuck_open * (op - (ambient_c + 25)) * airflow
        tau = tau_warm * (1.2 - 0.4 * demand) * (1 + h.thermostat_stuck_open)
        if T < op:
            T += (op - T) / tau
        else:
            T += (op - T) / 90
        st.coolant_c = T
        iat_target = ambient_c + (16 if v < 5 else 16 * math.exp(-v / 25))
        st.iat_c += (iat_target - st.iat_c) / 60

        # ---- electrical -----------------------------------------------------------
        batt = h.charging_v - h.charging_sag_v_per_load * calc_load / 100 + rng.normal(0, 0.03)
        batt -= 0.08 * (v < 1) * (T > op - 1)  # cooling fan + idle: small dip

        # ---- fuel used -------------------------------------------------------------
        if not is_cut:
            air_gs = metered_gs + h.vacuum_leak_gs
            st.fuel_l -= air_gs / AFR_STOICH / FUEL_G_PER_L

        # ---- device motion + GNSS --------------------------------------------------
        st.heading_rad += rng.normal(0, 0.03) * (v > 1)
        d_m = v_ms
        dist_km += d_m / 1000
        st.lat += d_m * math.cos(st.heading_rad) / 111_320
        st.lon += d_m * math.sin(st.heading_rad) / (111_320 * math.cos(math.radians(st.lat)))

        # ---- record ----------------------------------------------------------------
        out["rpm"][i] = rpm
        out["speed"][i] = v
        out["load"][i] = min(max(calc_load, 0), 100)
        out["throttle"][i] = 14.5 + (70 * demand**0.9 if not is_cut else 0) + rng.normal(0, 0.3)
        out["coolant"][i] = T
        out["iat"][i] = st.iat_c
        out["maf"][i] = max(maf_read, 0.0)
        out["map"][i] = map_kpa
        out["abs_load"][i] = abs_load
        out["timing_adv"][i] = (
            (10 + 3 * (1 - warmth)) if v < 1 else 14 + 20 * (1 - demand)
        ) + rng.normal(0, 1.2)
        out["batt_v"][i] = batt
        out["ecu_voltage"][i] = batt - 0.18 + rng.normal(0, 0.02)
        out["fuel_level"][i] = st.fuel_l / p.fuel_tank_l * 100 + (
            rng.normal(0, 1.0) if v > 5 else 0
        )
        out["ambient"][i] = ambient_c + 1.5 + (2.0 if v < 5 else 0) + rng.normal(0, 0.3)
        out["baro"][i] = baro_kpa + rng.normal(0, 0.2)
        out["acc_x"][i] = a / G + rng.normal(0, 0.03) + rng.normal(0, 0.04)  # + rough road
        out["acc_y"][i] = rng.normal(0, 0.03) + (v > 5) * rng.normal(0, 0.04)
        out["acc_z"][i] = 1.0 + rng.normal(0, 0.05) * (1 + (v > 5))
        out["runtime_since_start"][i] = i
        lat[i], lon[i] = st.lat, st.lon
        gspd[i] = max(v + rng.normal(0, 0.5), 0.0)

    # ---- trip-level bookkeeping (OBD counters) -------------------------------------
    # A "warm-up cycle" in OBD terms: coolant rose at least 22 °C since start and reached
    # at least 70 °C.
    if out["coolant"].max() >= 70 and out["coolant"].max() - coolant_start >= 22:
        st.warmups_since_clear = min(st.warmups_since_clear + 1, 255)
    st.dist_since_clear_km += dist_km
    st.odometer_km += dist_km
    st.fuel_l = max(st.fuel_l, 0.0)

    # Real sensors and PID encodings clamp at their limits; so do we.
    cat = catalogue()
    for name, arr in out.items():
        if name in cat:
            np.clip(arr, cat[name]["min"], cat[name]["max"], out=arr)

    return TripOutput(
        signals=out,
        lat=lat,
        lon=lon,
        gnss_speed=gspd,
        crank_volts=crank,
        resting_v=resting_v,
        distance_km=dist_km,
        extras={
            "closed_loop": closed_loop,
            "fuel_cut": fuel_cut,
            "gear": gear_arr,
            "misfire_events": misfire_events,
            "coolant_start_c": coolant_start,
        },
    )
