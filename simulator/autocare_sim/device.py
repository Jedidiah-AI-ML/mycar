"""Freematics ONE+ emulation: what the AutoCare firmware would see, record and upload.

    tl = simulate("toyota_corolla_1_8_2009", days=7, seed=1)
    run = Device("FM1-0001", tl, seed=1, network=Network.lagos(...), sink=MemorySink()).run()

The simulator (steps 3-4) produces the car's *true* state every second. The device:
- **polls** only the PIDs the car supports: fast group every second, slow group every
  7.5 s, start-of-trip PIDs once (PRD 7 polling strategy). Each read takes a few ms and
  occasionally times out;
- adds **events**: trip start/end, VIN and supported PIDs after a boot, readiness, DTCs
  (Modes 03/07/0A) at trip start and end, freeze frames, Mode 06, the cranking trace,
  harsh driving;
- cuts the stream into **batches** (60 s or ~4 KB gzipped) and keeps them in **flash**
  until the server acknowledges them (store-and-forward);
- **uploads** when there is coverage, retrying with backoff. Lost replies cause
  duplicate sends; after an outage the freshest batch goes first (out of order);
- runs on its own **clock**, which drifts, can reset to 1970 on a reboot, and is
  corrected from GNSS or the server's ack;
- sends a small **critical packet** immediately for on-device "Stop safely" conditions.

All numbers are SEED values in `simulator/config/device.yaml`.
"""

import gzip
import json
import zlib
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from autocare_sim.capabilities import FULL, Capabilities
from autocare_sim.config import catalogue, device_config
from autocare_sim.dtc import rules
from autocare_sim.network import ACK_LOST, LOST, NO_SIGNAL, OK, Network
from autocare_sim.run import Timeline, Trip

FAST = ("rpm", "speed", "load", "throttle")
START = ("warmups_since_clear", "dist_since_clear", "dist_mil_on", "runtime_since_start")
MOTION = ("acc_x", "acc_y", "acc_z")
FREEZE = ("rpm", "speed", "load", "coolant", "stft_b1", "ltft_b1", "maf", "map", "iat")
PROTOCOL = "ISO 15765-4 CAN 11/500"  # SEED: all five cars are CAN (2008+ US-market rule)

# Our ECU monitors -> OBD readiness monitor names (Mode 01 PID 01).
READINESS = {
    "misfire": ("misfire",),
    "fuel_system": ("fuel_system_b1", "fuel_system_b2"),
    "components": (),  # comprehensive components: continuous, always complete
    "catalyst": ("catalyst",),
    "o2_sensor": ("o2_sensor",),
    "o2_heater": ("o2_sensor",),
    "evap": ("catalyst",),  # SEED: runs after a long warm drive, like the catalyst test
}
NOT_FITTED = ("egr", "secondary_air", "ac_refrigerant", "heated_catalyst")


def gz(doc: dict) -> bytes:
    """Compact JSON, gzipped. mtime=0 so equal documents give equal bytes."""
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return gzip.compress(raw, compresslevel=6, mtime=0)


# ---------------------------------------------------------------- identity


_TRANSLIT = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
    **dict(zip("JKLMN", range(1, 6), strict=True)),
    "P": 7, "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10), strict=True)),
}  # fmt: skip
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_WMI = {"Corolla": "2T1", "Camry": "4T1", "RX350": "2T2", "Accord": "1HG", "Sienna": "5TD"}
_YEAR = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 2010 = A ... ; 2001-2009 = 1-9


def make_vin(profile, rng: np.random.Generator) -> str:
    """A plausible US-market VIN with a correct check digit (made up, not a real car)."""
    year = profile.years[0]
    ycode = str(year - 2000) if year < 2010 else _YEAR[year - 2010]
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    vds = "".join(letters[int(i)] for i in rng.integers(0, len(letters), 5))
    serial = "".join(str(int(d)) for d in rng.integers(0, 10, 6))
    vin = f"{_WMI.get(profile.model, '1XX')}{vds}0{ycode}U{serial}"
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    check = "X" if total % 11 == 10 else str(total % 11)
    return vin[:8] + check + vin[9:]


def pid_bitmaps(supported: list[str]) -> str:
    """Mode 01 support bitmaps (PIDs 00, 20, 40, ...) as one hex string."""
    cat = catalogue()
    pids = {1, 0x1C} | {int(cat[n]["pid"], 16) for n in supported if cat[n]["pid"]}
    top = max(pids)
    out = ""
    for base in range(0, top + 1, 0x20):
        bits = 0
        for p in pids:
            if base < p <= base + 0x20:
                bits |= 1 << (32 - (p - base))
        if top > base + 0x20:
            bits |= 1  # "next range supported" (PID base+0x20)
        out += f"{bits:08X}"
        if top <= base + 0x20:
            break
    return out


# ---------------------------------------------------------------- clock


@dataclass
class Clock:
    """Device clock = true time + offset. Drifts; can reset; corrected at batch edges."""

    drift_ppm: float
    offset_ms: float = 0.0
    last_true_ms: int = 0
    pending_offset: float | None = None

    def now(self, true_ms: int) -> int:
        if self.last_true_ms:
            self.offset_ms += (true_ms - self.last_true_ms) * self.drift_ppm * 1e-6
        self.last_true_ms = true_ms
        return int(round(true_ms + self.offset_ms))

    def reset(self, true_ms: int) -> None:
        self.offset_ms, self.last_true_ms, self.pending_offset = -float(true_ms), true_ms, None

    def correct_to(self, true_ms: int, error_ms: float) -> None:
        self.pending_offset = error_ms  # device clock := true time (+ small error)

    def apply_pending(self) -> None:
        if self.pending_offset is not None:
            self.offset_ms, self.pending_offset = self.pending_offset, None


# ---------------------------------------------------------------- results


@dataclass
class Pending:
    kind: str  # "batch" | "critical"
    doc: dict  # without sent_at / gnss_time / net (filled in per attempt)
    key: tuple
    est_bytes: int
    created_ms: int  # true time
    attempts: int = 0
    next_try_ms: int = 0


@dataclass
class Delivery:
    true_ms: int
    kind: str
    key: tuple
    outcome: str
    body_bytes: int
    wire_bytes: int


@dataclass
class DeviceRun:
    device_id: str
    produced: list[dict] = field(default_factory=list)  # closed batches, in creation order
    criticals: list[dict] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    dropped: list[tuple] = field(default_factory=list)  # keys dropped because flash was full
    true_t0: dict[tuple, int] = field(default_factory=dict)  # batch key -> true time at t0
    driving_s: int = 0
    max_backlog: int = 0
    reboots: int = 0

    @property
    def wire_bytes(self) -> int:
        return sum(d.wire_bytes for d in self.deliveries if d.outcome != NO_SIGNAL)

    def budget(self) -> dict:
        """Data used: KB per driving hour and MB per month at 2 h/day (P13)."""
        hours = self.driving_s / 3600
        kb_h = self.wire_bytes / 1024 / max(hours, 1e-9)
        body = sum(d.body_bytes for d in self.deliveries if d.outcome != NO_SIGNAL)
        return {
            "driving_h": round(hours, 2),
            "requests": sum(d.outcome != NO_SIGNAL for d in self.deliveries),
            "body_kb_per_h": round(body / 1024 / max(hours, 1e-9), 1),
            "wire_kb_per_h": round(kb_h, 1),
            "mb_per_month_2h_day": round(kb_h * 2 * 30 / 1024, 1),
        }


# ---------------------------------------------------------------- device


class Device:
    def __init__(
        self,
        device_id: str,
        timeline: Timeline,
        seed: int,
        caps: Capabilities = FULL,
        network: Network | None = None,
        sink=None,
        clock_faults: bool = True,
        flash_bytes: int | None = None,
    ):
        self.cfg = device_config()
        self.id, self.tl, self.seed, self.caps = device_id, timeline, seed, caps
        self.p = timeline.profile
        self.net = network or Network.perfect()
        self.sink = sink
        self.clock_faults = clock_faults
        self.flash_cap = flash_bytes or self.cfg["flash"]["capacity_bytes"]
        self.rng = np.random.default_rng([seed, zlib.crc32(device_id.encode()), 1])
        drift = float(self.rng.normal(0, self.cfg["clock"]["drift_ppm_sd"])) if clock_faults else 0
        self.clock = Clock(drift_ppm=drift)
        self.vin = make_vin(self.p, np.random.default_rng([seed, 99]))
        cat = catalogue()
        self.cat = cat
        sup = self.p.supported  # a tuple: fixed order, so polling order is reproducible
        self.fast = [s for s in FAST if s in sup]
        self.slow = [s for s in sup if cat[s]["group"] == "slow"]
        self.start = [s for s in START if s in sup]
        self.fast_hz = self.cfg["polling"]["fast_hz"]
        self.slow_s = self.cfg["polling"]["slow_s"]
        # Persistent on the device (flash): boot and trip counters, unsent batches.
        self.boot, self.trip_no = 0, 0
        self.queue: deque[Pending] = deque()
        self.crit_queue: deque[Pending] = deque()
        self.bytes_up = 0
        self.last_request_ms = -(10**15)
        self.run_ = DeviceRun(device_id)
        self.cmd_events: list[dict] = []  # results/events owed to the server
        self.health_scan_due = False
        self.mil_km = 0.0
        self.fix_at_ms: int | None = None
        self._open: dict | None = None

    # ------------------------------------------------------------ main loop

    def run(self, drain_days: float = 3.0) -> DeviceRun:
        trips = self.tl.trips
        for k, tr in enumerate(trips):
            self._wake(tr, first=k == 0)
            self._drive(tr, prev=trips[k - 1] if k else None)
            end = int(tr.t_ms[-1])
            linger_end = end + self.cfg["power"]["linger_after_ignition_off_s"] * 1000
            nxt = int(trips[k + 1].t_ms[0]) - 2000 if k + 1 < len(trips) else linger_end
            self._idle(end + 1000, min(linger_end, nxt), step_ms=2000)
        # After the last trip the car keeps being used; let the backlog drain.
        if trips:
            end = int(trips[-1].t_ms[-1])
            self._idle(end, end + int(drain_days * 86_400_000), step_ms=10_000)
        return self.run_

    def _wake(self, tr: Trip, first: bool) -> None:
        pw, true0 = self.cfg["power"], int(tr.t_ms[0])
        brownout = tr.out.crank_volts.min() < pw["brownout_crank_v"]
        reboot = first or brownout or self.rng.random() < pw["reboot_p_per_trip"]
        self.booted_now = reboot
        if reboot:
            if not first:
                self.boot += 1
                self.run_.reboots += 1
            lost_rtc = self.rng.random() < self.cfg["clock"]["reset_on_reboot_p"]
            if first or (self.clock_faults and lost_rtc):
                self.clock.reset(true0 - 2000)
                if first and not self.clock_faults:
                    self.clock.offset_ms = 0.0
        lo, hi = self.cfg["polling"]["gnss_first_fix_s"]
        self.fix_at_ms = true0 + int(self.rng.uniform(lo, hi) * 1000) if self.caps.gnss else None

    # ------------------------------------------------------------ one trip

    def _drive(self, tr: Trip, prev: Trip | None) -> None:
        self.trip_no += 1
        self.seq = 0
        sig, n = tr.out.signals, tr.duration_s
        self.run_.driving_s += n
        pol = self.cfg["polling"]

        # Per-signal random streams: switching one capability off never shifts another
        # signal's jitter or dropouts.
        def stream(name: str) -> np.random.Generator:
            return np.random.default_rng([self.seed, self.trip_no, zlib.crc32(name.encode())])

        lat_lo, lat_hi = pol["read_latency_ms"]
        crit_cfg = self.cfg["critical"]
        crit_sent: set[str] = set()
        low_v_since: int | None = None
        last_harsh = -99
        slow_period = max(int(round(self.slow_s)), 1)

        self._open_batch(int(tr.t_ms[0]))
        self._start_events(tr, prev)

        rs = {name: stream(name) for name in [*self.fast, *self.slow, "batt_v", *MOTION, "dev_t"]}
        for i in range(n):
            true_ms = int(tr.t_ms[i])
            if self._open is None:
                self._open_batch(true_ms)
            if self.fix_at_ms is not None and true_ms >= self.fix_at_ms and self.clock_faults:
                self.clock.correct_to(true_ms, 0.0)
                self.fix_at_ms = None
            elif self.fix_at_ms is not None and true_ms >= self.fix_at_ms:
                self.fix_at_ms = None

            # Fast group (rate may be changed by a command).
            per_s = max(int(round(self.fast_hz)), 1)
            for sub in range(per_s):
                lat = 0
                for name in self.fast:
                    r = rs[name]
                    lat += int(r.integers(lat_lo, lat_hi + 1))
                    if r.random() < pol["read_fail_p"]:
                        continue
                    self._sample(name, true_ms + sub * 1000 // per_s + lat, sig[name][i])
            if i % slow_period == 0:
                lat = 0
                for name in self.slow:
                    r = rs[name]
                    lat += int(r.integers(lat_lo, lat_hi + 1))
                    if r.random() < pol["read_fail_p"]:
                        continue
                    self._sample(name, true_ms + 300 + lat, sig[name][i])
                    if name == "coolant" and sig[name][i] >= crit_cfg["coolant_high_c"]:
                        self._critical("coolant_high", tr, i, crit_sent)
            if self.caps.batt_v and i % int(pol["batt_v_s"]) == 0:
                v = sig["batt_v"][i]
                self._sample("batt_v", true_ms + 5, v)
                if v < crit_cfg["voltage_collapse_v"] and sig["rpm"][i] > 400:
                    low_v_since = true_ms if low_v_since is None else low_v_since
                    if true_ms - low_v_since >= crit_cfg["voltage_collapse_s"] * 1000:
                        self._critical("voltage_collapse", tr, i, crit_sent)
                else:
                    low_v_since = None
            if self.caps.accel:
                for name in MOTION:
                    self._sample(name, true_ms + 2, sig[name][i])
                last_harsh = self._harsh(tr, i, last_harsh)
            if i % int(pol["device_temp_s"]) == 0:
                temp = tr.ambient_c + 12 + float(rs["dev_t"].normal(0, 1.0))
                self._sample("device_temp", true_ms + 7, temp)
            if self.caps.gnss and self.fix_at_ms is None and i % int(pol["gnss_s"]) == 0:
                self._fix(true_ms, tr.out.lat[i], tr.out.lon[i], tr.out.gnss_speed[i])
            if self.health_scan_due:
                # The ECU's state *now* is what it was after the previous trip.
                self._health_scan(true_ms, prev.diag if prev else None)

            # Close the batch at 60 s, or earlier when it gets big.
            span = self.clock.now(true_ms) - self._open["t0"]
            bc = self.cfg["batching"]
            if span >= bc["max_span_s"] * 1000 - 1000 or (
                i % bc["size_check_every_s"] == 0 and len(gz(self._open)) >= bc["max_gzip_bytes"]
            ):
                self._close_batch(true_ms)
            self._tick(true_ms)

        end_ms = int(tr.t_ms[-1])
        if self._open is None:
            self._open_batch(end_ms)
        self._end_events(tr, end_ms)
        if tr.diag and tr.diag.mil:
            self.mil_km += tr.out.distance_km
        self._close_batch(end_ms, final=True)
        self._tick(end_ms)

    # ------------------------------------------------------------ batch building

    def _open_batch(self, true_ms: int) -> None:
        self.clock.apply_pending()
        self._open = {
            "v": 1, "device_id": self.id, "fw": self.cfg["firmware"], "boot": self.boot,
            "trip": self.trip_no, "seq": self.seq, "final": False,
            "t0": self.clock.now(true_ms), "s": {}, "g": [], "ev": [],
        }  # fmt: skip
        self._open_true_ms = true_ms

    def _dt(self, true_ms: int) -> int:
        return max(self.clock.now(true_ms) - self._open["t0"], 0)

    def _sample(self, name: str, true_ms: int, value: float) -> None:
        dp = self.cat[name]["dp"]
        v = round(float(value), dp)
        self._open["s"].setdefault(name, []).append([self._dt(true_ms), int(v) if dp == 0 else v])

    def _event(self, true_ms: int, ev: dict) -> None:
        self._open["ev"].append({"t": self._dt(true_ms), **ev})

    def _fix(self, true_ms: int, lat: float, lon: float, spd: float) -> None:
        self._open["g"].append(
            [self._dt(true_ms), round(float(lat), 5), round(float(lon), 5), round(float(spd), 1)]
        )

    def _close_batch(self, true_ms: int, final: bool = False) -> None:
        doc = self._open
        doc["final"] = final
        # Keep each signal's samples sorted (latency can reorder reads within a second).
        for rows in doc["s"].values():
            rows.sort(key=lambda r: r[0])
        doc["ev"].sort(key=lambda e: e["t"])
        self.run_.produced.append(json.loads(json.dumps(doc)))
        key = (self.id, doc["boot"], doc["trip"], doc["seq"])
        self.run_.true_t0[key] = self._open_true_ms
        self._enqueue(Pending("batch", doc, key, len(gz(doc)), true_ms), self.queue)
        self.seq += 1
        self._open = None

    def _enqueue(self, item: Pending, q: deque) -> None:
        q.append(item)
        used = sum(x.est_bytes for x in self.queue) + sum(x.est_bytes for x in self.crit_queue)
        while used > self.flash_cap and len(self.queue) > 1:
            old = self.queue.popleft()  # flash full: the oldest data goes first
            used -= old.est_bytes
            self.run_.dropped.append(old.key)
        self.run_.max_backlog = max(self.run_.max_backlog, len(self.queue))

    # ------------------------------------------------------------ events

    def _start_events(self, tr: Trip, prev: Trip | None) -> None:
        t = int(tr.t_ms[0])
        self._event(t, {"type": "trip_start"})
        if self.caps.crank:
            volts = [round(float(v), 2) for v in tr.out.crank_volts]
            self._event(t, {"type": "crank", "rate_hz": 50.0, "volts": volts})
        if self.booted_now:
            if self.caps.vin:
                self._event(t, {"type": "vin", "vin": self.vin})
            self._event(t, {"type": "pids_supported", "protocol": PROTOCOL,
                            "mode01": pid_bitmaps(list(self.p.supported))})  # fmt: skip
        diag = prev.diag if prev else None
        self._diag_events(t, diag, previous=None)
        self._start_pids(t, tr, prev, at_end=False)

    def _end_events(self, tr: Trip, t: int) -> None:
        self._diag_events(t, tr.diag, previous=self._start_lists)
        if self.caps.freeze_frame and tr.diag:
            for code in [x.code for x in tr.diag.transitions if x.status == "confirmed"]:
                self._event(t, {"type": "freeze_frame", "code": code,
                                "values": self._freeze_values(tr)})  # fmt: skip
        if self.caps.mode06 and tr.diag:
            res = self._mode06(tr)
            if res:
                self._event(t, {"type": "mode06", "results": res})
        self._start_pids(t, tr, None, at_end=True)
        self._event(t, {"type": "trip_end", "reason": "ignition_off"})

    def _dtc_lists(self, diag) -> dict[str, list[str]]:
        if diag is None:
            return {"03": [], "07": [], "0A": []}
        # Permanent DTCs (0A): confirmed codes whose MIL is still on (simplified).
        return {"03": diag.confirmed, "07": diag.pending,
                "0A": diag.confirmed if diag.mil else []}  # fmt: skip

    def _diag_events(self, t: int, diag, previous: dict | None) -> None:
        lists = self._dtc_lists(diag)
        if self.caps.readiness:
            done = set(diag.monitors_complete) if diag else set()
            mons = {m: ("complete" if (not src or done & set(src)) else "incomplete")
                    for m, src in READINESS.items()}  # fmt: skip
            mons |= dict.fromkeys(NOT_FITTED, "na")
            self._event(t, {"type": "readiness", "mil": bool(diag and diag.mil),
                            "dtc_count": len(lists["03"]), "monitors": mons})  # fmt: skip
        allowed = {"03": self.caps.mode03, "07": self.caps.mode07, "0A": self.caps.mode0a}
        for mode, codes in lists.items():
            # Trip start: always report (an empty list says "none"). Trip end: changes only.
            if allowed[mode] and (previous is None or previous[mode] != codes):
                self._event(t, {"type": "dtc", "mode": mode, "codes": list(codes)})
        if previous is None:
            self._start_lists = lists

    def _start_pids(self, t: int, tr: Trip, prev: Trip | None, at_end: bool) -> None:
        src = tr if at_end else prev
        vals = {
            "warmups_since_clear": src.warmups_since_clear if src else 0,
            "dist_since_clear": src.dist_since_clear_km if src else 0.0,
            "dist_mil_on": self.mil_km,
            "runtime_since_start": tr.duration_s - 1 if at_end else 0,
        }
        for name in self.start:
            self._sample(name, t + 50, min(vals[name], self.cat[name]["max"]))

    def _freeze_values(self, tr: Trip) -> dict[str, float]:
        cl = np.flatnonzero(tr.out.extras["closed_loop"])
        i = int(cl[-1]) if len(cl) else tr.duration_s // 2
        return {n: round(float(tr.out.signals[n][i]), self.cat[n]["dp"])
                for n in FREEZE if n in self.p.supported}  # fmt: skip

    def _mode06(self, tr: Trip) -> list[dict]:
        m6, vals, out = self.cfg["mode06"], tr.diag.values, []
        mons = rules()["monitors"]
        if vals.get("o2_sensor") is not None:
            c = m6["o2_sensor"]
            out.append({"mid": c["mid"], "tid": c["tid"], "uasid": c["uasid"],
                        "value": int(vals["o2_sensor"] * c["scale"]),
                        "min": int(mons["o2_sensor"]["fail_below"] * c["scale"]),
                        "max": 65535})  # fmt: skip
        if vals.get("catalyst") is not None:
            c = m6["catalyst"]
            out.append({"mid": c["mid"], "tid": c["tid"], "uasid": c["uasid"],
                        "value": int(vals["catalyst"] * c["scale"]), "min": 0,
                        "max": int(mons["catalyst"]["fail_above"] * c["scale"])})  # fmt: skip
        if vals.get("misfire") is not None:
            c = m6["misfire"]
            weak = tr.health.misfire_cylinder
            revs = float(tr.out.signals["rpm"].sum() / 60)
            limit = int(mons["misfire"]["fail_above"] * revs / 1000)
            for cyl in range(1, self.p.cylinders + 1):
                count = tr.out.extras["misfire_events"] if cyl == weak else 0
                out.append({"mid": c["mid_base"] + cyl, "tid": c["tid"], "uasid": c["uasid"],
                            "value": int(count), "min": 0, "max": limit})  # fmt: skip
        return out

    def _harsh(self, tr: Trip, i: int, last: int) -> int:
        h, s = self.cfg["harsh"], tr.out.signals
        ax, ay = float(s["acc_x"][i]), float(s["acc_y"][i])
        kind = None
        if ax <= -h["brake_g"]:
            kind, peak = "brake", -ax
        elif ax >= h["accel_g"]:
            kind, peak = "accel", ax
        elif abs(ay) >= h["corner_g"]:
            kind, peak = "corner", abs(ay)
        if kind and i - last >= 3:
            self._event(int(tr.t_ms[i]), {"type": "harsh", "kind": kind, "peak_g": round(peak, 2)})
            return i
        return last

    def _critical(self, kind: str, tr: Trip, i: int, sent: set[str]) -> None:
        if kind in sent:
            return
        sent.add(kind)
        true_ms = int(tr.t_ms[i])
        s = tr.out.signals
        values = {n: round(float(s[n][i]), self.cat[n]["dp"])
                  for n in ("coolant", "rpm", "speed", "batt_v")}  # fmt: skip
        doc = {"v": 1, "device_id": self.id, "boot": self.boot, "trip": self.trip_no,
               "t": self.clock.now(true_ms), "kind": kind, "values": values}  # fmt: skip
        if self.caps.gnss and self.fix_at_ms is None:
            doc["pos"] = [round(float(tr.out.lat[i]), 5), round(float(tr.out.lon[i]), 5)]
        self.run_.criticals.append(dict(doc))
        key = (self.id, self.boot, self.trip_no, f"crit-{kind}")
        self._enqueue(Pending("critical", doc, key, len(gz(doc)), true_ms), self.crit_queue)

    # ------------------------------------------------------------ commands

    def _handle_ack(self, ack: dict, true_ms: int) -> None:
        if self.clock_faults:
            lo, hi = self.cfg["clock"]["correction_error_ms"]
            self.clock.correct_to(true_ms, float(self.rng.uniform(lo, hi)))
        for cmd in ack.get("cmds", []):
            if not self.caps.commands:
                self.cmd_events.append({"type": "cmd_result", "command_id": cmd["id"],
                                        "status": "unsupported"})  # fmt: skip
            elif cmd["type"] == "set_poll_rates":
                self.fast_hz = float(cmd["payload"]["fast_hz"])
                self.slow_s = float(cmd["payload"]["slow_s"])
                self.cmd_events.append({"type": "cmd_result", "command_id": cmd["id"],
                                        "status": "done"})  # fmt: skip
            elif cmd["type"] == "health_scan":
                self.health_scan_due = cmd["id"]

    def _health_scan(self, true_ms: int, prev_diag) -> None:
        cmd_id, self.health_scan_due = self.health_scan_due, False
        self._diag_events(true_ms, prev_diag, previous=None)
        self._event(true_ms, {"type": "cmd_result", "command_id": cmd_id, "status": "done"})

    # ------------------------------------------------------------ uplink

    def _idle(self, start_ms: int, end_ms: int, step_ms: int) -> None:
        t = start_ms
        while t < end_ms and (self.queue or self.crit_queue):
            self._tick(t)
            t += step_ms

    def _tick(self, true_ms: int) -> None:
        if self.cmd_events and self._open is not None:
            for ev in self.cmd_events:
                self._event(true_ms, ev)
            self.cmd_events = []
        for _ in range(self.cfg["network"]["requests_per_s"]):
            item, q = self._next_item(true_ms)
            if item is None:
                return
            self._attempt(item, q, true_ms)

    def _next_item(self, true_ms: int):
        for q in (self.crit_queue, self.queue):
            if not q:
                continue
            ready = [x for x in q if x.next_try_ms <= true_ms]
            if not ready:
                continue
            if q is self.queue and self.cfg["network"]["freshest_first"] and len(q) > 1:
                newest = q[-1]
                if newest.attempts == 0 and newest.next_try_ms <= true_ms:
                    return newest, q
            return ready[0], q
        return None, None

    def _attempt(self, item: Pending, q: deque, true_ms: int) -> None:
        if not self.net.covered(true_ms):
            # No signal: nothing goes on air. Check again in 10 s.
            self.run_.deliveries.append(Delivery(true_ms, item.kind, item.key, NO_SIGNAL, 0, 0))
            item.next_try_ms = true_ms + 10_000
            return
        doc = dict(item.doc)
        doc["sent_at"] = max(self.clock.now(true_ms), doc.get("t0", doc.get("t", 0)))
        if item.kind == "batch":
            have_fix = self.caps.gnss and self.fix_at_ms is None
            doc["gnss_time"] = true_ms if have_fix else None
            doc["net"] = {"rssi": self.net.rssi(), "bytes_up": self.bytes_up}
        body = gz(doc)
        outcome = self.net.attempt(true_ms)
        wire = 0
        if outcome != NO_SIGNAL:
            https = self.cfg["https"]
            wire = len(body) + https["per_request_overhead_bytes"]
            if true_ms - self.last_request_ms > https["keepalive_s"] * 1000:
                wire += https["handshake_bytes"]
            self.last_request_ms = true_ms
            self.bytes_up += wire
        self.run_.deliveries.append(
            Delivery(true_ms, item.kind, item.key, outcome, len(body), wire)
        )
        ack = {"ok": True, "cmds": []}
        if outcome in (OK, ACK_LOST) and self.sink is not None:
            try:
                ack = self.sink.post(item.kind, body, true_ms)
            except ConnectionError:
                outcome = LOST  # real HTTP failure: retry later like any lost request
        if outcome == OK:
            q.remove(item)
            self._handle_ack(ack, true_ms)
            return
        # LOST / ACK_LOST: back off and resend later. NO_SIGNAL: check coverage again soon.
        item.attempts += outcome != NO_SIGNAL
        wait = 10.0 if outcome == NO_SIGNAL else self.net.backoff_s(item.attempts)
        item.next_try_ms = true_ms + int(wait * 1000)
