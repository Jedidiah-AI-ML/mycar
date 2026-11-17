"""Phase 2 step 5 gate: the emulated Freematics sends valid payloads, loses nothing through
outages, capability switches remove exactly their data, and the data budget is printed."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from jsonschema import Draft202012Validator

from autocare_sim.capabilities import FULL, Capabilities
from autocare_sim.config import REPO, catalogue, profile_ids
from autocare_sim.device import Device, gz, make_vin, pid_bitmaps
from autocare_sim.faults import fault
from autocare_sim.network import Network
from autocare_sim.outputs.freematics import FreematicsHttpSink
from autocare_sim.outputs.memory import MemorySink
from autocare_sim.physics import Health
from autocare_sim.run import simulate

COROLLA = "toyota_corolla_1_8_2009"
DAY_MS = 86_400_000
SCHEMA = json.loads(
    (REPO / "contracts" / "freematics_one_plus.v1.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA)
DEVICE_SIGNALS = {"batt_v", "acc_x", "acc_y", "acc_z", "device_temp"}


def _validator_for(def_name: str) -> Draft202012Validator:
    return Draft202012Validator({**SCHEMA, "oneOf": [{"$ref": f"#/$defs/{def_name}"}]})


def lagos(tl, seed):
    start, end = int(tl.trips[0].t_ms[0]), int(tl.trips[-1].t_ms[-1])
    return Network.lagos(start, end + 3 * DAY_MS, seed=seed)


def flatten(batches: list[dict]) -> dict[str, set]:
    """Every sample, fix and event on the absolute device clock, grouped by kind."""
    out: dict[str, set] = {}
    for b in batches:
        t0 = b["t0"]
        for name, rows in b["s"].items():
            out.setdefault(f"s:{name}", set()).update((t0 + dt, v) for dt, v in rows)
        if b["g"]:
            out.setdefault("g", set()).update((t0 + r[0], *r[1:]) for r in b["g"])
        for ev in b["ev"]:
            kind = ev["type"] + (f":{ev['mode']}" if ev["type"] == "dtc" else "")
            body = json.dumps({k: v for k, v in ev.items() if k != "t"}, sort_keys=True)
            out.setdefault(f"e:{kind}", set()).add((t0 + ev["t"], body))
    return out


# ---------------------------------------------------------------- the fleet run


@pytest.fixture(scope="module")
def fleet():
    """5 cars x 7 days, Lagos coverage (outages, lost requests, lost acks)."""
    runs = {}
    for k, pid in enumerate(profile_ids()):
        tl = simulate(pid, days=7, seed=10 + k)
        net = lagos(tl, seed=10 + k)
        sink = MemorySink()
        run = Device(f"FM1-{k:04d}", tl, seed=10 + k, network=net, sink=sink).run()
        runs[pid] = (tl, run, sink, net)
    return runs


def test_every_payload_validates_against_the_contract(fleet):
    ack_v = _validator_for("AckV1")
    n = 0
    for _, _run, sink, _ in fleet.values():
        for _, kind, doc in sink.received:
            errors = list(VALIDATOR.iter_errors(doc))
            assert not errors, (kind, errors[0].message[:300])
            n += 1
        assert sink.criticals == [] or all(c["kind"] for c in sink.criticals)
    ack = MemorySink().post("batch", gz(fleet[COROLLA][1].produced[0]), 1)
    assert not list(ack_v.iter_errors(ack))
    assert n > 1000


def test_nothing_lost_through_outages(fleet):
    for pid, (_, run, sink, _net) in fleet.items():
        produced = {(b["device_id"], b["boot"], b["trip"], b["seq"]): b for b in run.produced}
        assert run.dropped == []
        assert set(sink.batches) == set(produced), pid
        for key, doc in sink.batches.items():
            p = produced[key]
            assert (doc["s"], doc["g"], doc["ev"], doc["t0"]) == (p["s"], p["g"], p["ev"], p["t0"])
        assert flatten(list(sink.batches.values())) == flatten(run.produced)
    # The network really was bad: outages, duplicates and out-of-order arrival happened.
    sinks = [s for _, _, s, _ in fleet.values()]
    assert sum(s.duplicates for s in sinks) > 0
    assert any(a[1:] > b[1:] for s in sinks for a, b in zip(s.arrival, s.arrival[1:], strict=False))
    assert all(net.outages for *_, net in fleet.values())


def test_flash_full_drops_the_oldest_batches():
    tl = simulate(COROLLA, days=3, seed=2)
    start, end = int(tl.trips[0].t_ms[0]), int(tl.trips[-1].t_ms[-1])
    net = Network.lagos(start, end, seed=2)
    net.outages = [(start - 1000, end + DAY_MS)]  # no coverage until a day after the last trip
    sink = MemorySink()
    run = Device("FM1-9000", tl, seed=2, network=net, sink=sink, flash_bytes=60_000).run()
    order = [(b["device_id"], b["boot"], b["trip"], b["seq"]) for b in run.produced]
    assert run.dropped and set(run.dropped).isdisjoint(sink.batches)
    assert set(run.dropped) | set(sink.batches) == set(order)
    assert run.dropped == order[: len(run.dropped)]  # oldest first


def test_polls_only_supported_pids_at_planned_rates(fleet):
    cat = catalogue()
    for _, (tl, run, _, _) in fleet.items():
        sup = set(tl.profile.supported)
        seen = {name for b in run.produced for name in b["s"]}
        assert seen <= sup | DEVICE_SIGNALS
        full = [b for b in run.produced if not b["final"] and b["seq"] > 0]
        for b in full[:50]:
            span_s = 60
            assert abs(len(b["s"]["rpm"]) - span_s) <= 3  # fast group ~1 Hz
            slow = [n for n in b["s"] if cat[n]["group"] == "slow"]
            for n in slow:
                assert 6 <= len(b["s"][n]) <= 10  # slow group every ~7.5 s


def test_trip_events(fleet):
    tl, run, _, _ = fleet[COROLLA]
    by_trip: dict[tuple, list[dict]] = {}
    for b in run.produced:
        by_trip.setdefault((b["boot"], b["trip"]), []).append(b)
    assert len(by_trip) == len(tl.trips)
    for k, batches in enumerate(by_trip.values()):
        first, last = batches[0], batches[-1]
        types = [e["type"] for e in first["ev"]]
        assert first["seq"] == 0 and types[0] == "trip_start"
        assert {"crank", "readiness", "dtc"} <= set(types)
        assert {e["mode"] for e in first["ev"] if e["type"] == "dtc"} == {"03", "07", "0A"}
        assert last["final"] and last["ev"][-1]["type"] == "trip_end"
        assert [b["seq"] for b in batches] == list(range(len(batches)))
        if k == 0:
            assert {"vin", "pids_supported"} <= set(types)


def test_vin_and_pid_bitmaps():
    from autocare_sim.config import load_profile

    p = load_profile(COROLLA)
    vin = make_vin(p, __import__("numpy").random.default_rng(1))
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    tr = {**{str(d): d for d in range(10)}, **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
          **dict(zip("JKLMN", range(1, 6), strict=True)), "P": 7, "R": 9,
          **dict(zip("STUVWXYZ", range(2, 10), strict=True))}  # fmt: skip
    total = sum(tr[c] * w for c, w in zip(vin, weights, strict=True))
    assert len(vin) == 17 and vin[8] == ("X" if total % 11 == 10 else str(total % 11))
    assert not set(vin) & set("IOQ")
    # Decode the bitmaps back into PIDs.
    hexs = pid_bitmaps(list(p.supported))
    pids = set()
    for k in range(len(hexs) // 8):
        bits = int(hexs[8 * k : 8 * k + 8], 16)
        pids |= {k * 0x20 + i for i in range(1, 33) if bits & (1 << (32 - i))}
    cat = catalogue()
    want = {int(cat[n]["pid"], 16) for n in p.supported if cat[n]["pid"]} | {0x01, 0x1C}
    assert pids - {0x20, 0x40, 0x60, 0x80, 0xA0} == want


# ---------------------------------------------------------------- capabilities


@pytest.fixture(scope="module")
def faulty_week():
    """Thermostat sticks early: DTCs, freeze frames and Mode 06 all have content."""
    return simulate(COROLLA, days=8, seed=5, faults=[fault("stuck_open_thermostat", onset=1,
                                                            ramp=3)])  # fmt: skip


REMOVES = {
    "mode03": {"e:dtc:03"},
    "mode07": {"e:dtc:07"},
    "mode0a": {"e:dtc:0A"},
    "mode06": {"e:mode06"},
    "freeze_frame": {"e:freeze_frame"},
    "readiness": {"e:readiness"},
    "vin": {"e:vin"},
    "batt_v": {"s:batt_v"},
    "crank": {"e:crank"},
    "gnss": {"g"},
    "accel": {"s:acc_x", "s:acc_y", "s:acc_z", "e:harsh"},
}


def _clean_run(tl, caps: Capabilities):
    sink = MemorySink()
    run = Device("FM1-0100", tl, seed=5, caps=caps, sink=sink, clock_faults=False).run()
    return run, sink


@pytest.mark.parametrize("cap", sorted(REMOVES))
def test_capability_switch_removes_exactly_its_data(cap, faulty_week):
    full_run, _ = _clean_run(faulty_week, FULL)
    full = flatten(full_run.produced)
    for k in REMOVES[cap] - {"e:harsh"}:
        assert full.get(k), f"{k} should have data in the full run"
    run, sink = _clean_run(faulty_week, FULL.without(cap))
    got = flatten(run.produced)
    assert not (set(got) & REMOVES[cap])
    assert {k: v for k, v in full.items() if k not in REMOVES[cap]} == got
    if cap == "gnss":
        assert all(d["gnss_time"] is None for _, kind, d in sink.received if kind == "batch")


# ---------------------------------------------------------------- clock


def test_clock_resets_and_is_corrected(fleet):
    _, run, _, _ = fleet[COROLLA]
    first = run.produced[0]
    key = lambda b: (b["device_id"], b["boot"], b["trip"], b["seq"])  # noqa: E731
    assert first["t0"] < 10_000_000  # brand-new device: clock starts at 1970
    later = run.produced[len(run.produced) // 2 :]
    errors = [abs(b["t0"] - run.true_t0[key(b)]) for b in later]
    # Corrected from GNSS / acks; between corrections only drift and post-reboot resets.
    assert sorted(errors)[len(errors) // 2] < 2_000


# ---------------------------------------------------------------- critical packets


def test_critical_packets_sent_immediately():
    tl = simulate(COROLLA, days=2, seed=6,
                  health_fn=lambda d, k: Health(operating_offset_c=32, cooling_capacity=0.5,
                                                charging_v=11.4))  # fmt: skip
    sink = MemorySink()
    run = Device("FM1-0200", tl, seed=6, sink=sink, clock_faults=False).run()
    kinds = [c["kind"] for c in sink.criticals]
    assert "coolant_high" in kinds and "voltage_collapse" in kinds
    crit_v = _validator_for("CriticalV1")
    for c in sink.criticals:
        assert not list(crit_v.iter_errors(c))
    per_trip = {}
    for c in sink.criticals:
        per_trip.setdefault((c["trip"], c["kind"]), 0)
        per_trip[(c["trip"], c["kind"])] += 1
    assert max(per_trip.values()) == 1  # once per kind per trip
    crit = [d for d in run.deliveries if d.kind == "critical"]
    # Perfect coverage: delivered in the same second it was detected.
    for d, c in zip(crit, run.criticals, strict=True):
        assert abs(d.true_ms - c["t"]) <= 1000


# ---------------------------------------------------------------- commands


def test_remote_commands():
    tl = simulate(COROLLA, days=2, seed=7)
    sink = MemorySink()
    sink.commands = [
        {"id": 5, "type": "health_scan", "payload": {}},
        {"id": 6, "type": "set_poll_rates", "payload": {"fast_hz": 2, "slow_s": 5}},
    ]
    run = Device("FM1-0300", tl, seed=7, sink=sink, clock_faults=False).run()
    results = {e["command_id"]: e["status"] for b in run.produced for e in b["ev"]
               if e["type"] == "cmd_result"}  # fmt: skip
    assert results == {5: "done", 6: "done"}
    late = [b for b in run.produced[5:] if not b["final"] and b["seq"] > 0]
    assert late and abs(len(late[-1]["s"]["rpm"]) - 120) <= 6  # now 2 Hz

    sink2 = MemorySink()
    sink2.commands = [{"id": 7, "type": "health_scan", "payload": {}}]
    run2 = Device("FM1-0301", tl, seed=7, sink=sink2, clock_faults=False,
                  caps=FULL.without("commands")).run()  # fmt: skip
    res2 = [e for b in run2.produced for e in b["ev"] if e["type"] == "cmd_result"]
    assert [(e["command_id"], e["status"]) for e in res2] == [(7, "unsupported")]


def test_same_seed_same_payloads():
    tl = simulate(COROLLA, days=2, seed=8)
    a = Device("FM1-0400", tl, seed=8, network=lagos(tl, 8), sink=MemorySink()).run()
    b = Device("FM1-0400", tl, seed=8, network=lagos(tl, 8), sink=MemorySink()).run()
    assert a.produced == b.produced
    assert [d.__dict__ for d in a.deliveries] == [d.__dict__ for d in b.deliveries]


# ---------------------------------------------------------------- HTTPS client


def test_http_sink_posts_gzip_json_with_device_token():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["path"] = self.path
            seen["headers"] = dict(self.headers)
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            reply = json.dumps({"ok": True, "dup": False, "server_time": 123, "cmds": []})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        sink = FreematicsHttpSink(url, "FM1-0001", "s3cret")
        doc = {"v": 1, "device_id": "FM1-0001"}
        ack = sink.post("batch", gz(doc))
        assert ack["server_time"] == 123
        assert seen["path"] == "/ingest/freematics_one_plus/batches"
        assert seen["headers"]["Content-Encoding"] == "gzip"
        assert seen["headers"]["Authorization"] == "Bearer FM1-0001.s3cret"
        assert json.loads(gzip.decompress(seen["body"])) == doc
        sink.post("critical", gz(doc))
        assert seen["path"] == "/ingest/freematics_one_plus/critical"
    finally:
        server.shutdown()
    with pytest.raises(ValueError):
        FreematicsHttpSink("http://example.com", "FM1-0001", "x")


# ---------------------------------------------------------------- data budget


def test_data_budget(fleet, capsys):
    rows = []
    for pid, (_, run, _sink, _net) in fleet.items():
        b = run.budget()
        rows.append((pid, b))
        assert b["mb_per_month_2h_day"] < 30  # SEED P13 target
    with capsys.disabled():
        print("\n\nData budget (7 days, Lagos coverage; wire = gzip body + HTTPS overhead)")
        print(f"{'car':28s} {'drive h':>7s} {'req':>5s} {'body KB/h':>9s} {'wire KB/h':>9s}"
              f" {'MB/month @2h/day':>17s}")  # fmt: skip
        for pid, b in rows:
            print(f"{pid:28s} {b['driving_h']:7.1f} {b['requests']:5d} {b['body_kb_per_h']:9.1f}"
                  f" {b['wire_kb_per_h']:9.1f} {b['mb_per_month_2h_day']:17.1f}")  # fmt: skip
