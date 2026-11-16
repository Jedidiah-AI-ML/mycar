"""Payload contract v1 (ADR-006): shape, limits, exported schema, size budget."""

import gzip
import json
import math
import random

import jsonschema
import pytest
from pydantic import ValidationError

from app.gateway import contract
from app.gateway.contract import AckV1, BatchV1, CriticalV1
from app.gateway.signals import catalogue

# SEED - validate (P6): a 60 s batch should stay under this, gzipped.
SIZE_BUDGET_BYTES = 4096

T0 = 1_763_290_000_000


def typical_batch(seconds: int = 60, seed: int = 1) -> dict:
    """A realistic 60 s city-driving batch at the planned poll rates (P5):
    fast 1 Hz, slow every 7.5 s, accelerometer 1 Hz, GNSS every 5 s."""
    rnd = random.Random(seed)
    cat = catalogue()
    s: dict[str, list] = {}

    def add(name: str, period_ms: int, gen):
        sig = cat.by_name[name]
        s[name] = [
            [dt, round(min(max(gen(dt), sig.min), sig.max), sig.dp)]
            for dt in range(0, seconds * 1000, period_ms)
        ]

    speed = lambda dt: 25 + 15 * math.sin(dt / 9000) + rnd.uniform(-2, 2)  # noqa: E731
    add("speed", 1000, speed)
    add("rpm", 1000, lambda dt: 750 + speed(dt) * 45 + rnd.uniform(-40, 40))
    add("load", 1000, lambda dt: 22 + rnd.uniform(-6, 12))
    add("throttle", 1000, lambda dt: 14 + rnd.uniform(-3, 9))
    for name, base, spread in [
        ("coolant", 90, 1), ("iat", 41, 1), ("stft_b1", 0.8, 3), ("ltft_b1", 3.1, 0.2),
        ("maf", 6.5, 2.5), ("map", 38, 8), ("o2_b1s1_v", 0.45, 0.4), ("o2_b1s2_v", 0.68, 0.05),
        ("ecu_voltage", 14.1, 0.1), ("fuel_level", 54, 0), ("ambient", 33, 0),
        ("timing_adv", 12, 4), ("batt_v", 14.05, 0.1),
    ]:  # fmt: skip
        add(name, 7500, lambda dt, b=base, sp=spread: b + rnd.uniform(-sp, sp))
    for axis, base in (("acc_x", 0.0), ("acc_y", 0.0), ("acc_z", 1.0)):
        add(axis, 1000, lambda dt, b=base: b + rnd.uniform(-0.15, 0.15))
    g = [[dt, round(6.5244 + dt * 1e-8, 6), round(3.3792 + dt * 1e-8, 6), round(speed(dt), 1)]
         for dt in range(0, seconds * 1000, 5000)]  # fmt: skip
    return {
        "v": 1, "device_id": "FM1-0001", "fw": "0.1.0", "boot": 17, "trip": 42, "seq": 3,
        "t0": T0, "sent_at": T0 + seconds * 1000 + 250, "s": s, "g": g, "ev": [],
        "net": {"rssi": -71, "bytes_up": 183402},
    }  # fmt: skip


def with_events(batch: dict) -> dict:
    batch = json.loads(json.dumps(batch))
    batch["ev"] = [
        {"t": 0, "type": "trip_start"},
        {"t": 0, "type": "vin", "vin": "JTDBU4EE9A9123456"},
        {"t": 0, "type": "pids_supported", "protocol": "ISO 15765-4 CAN 11/500",
         "mode01": "BE3FA813A005B011"},
        {"t": 10, "type": "readiness", "mil": False, "dtc_count": 1,
         "monitors": {"catalyst": "complete", "evap": "incomplete", "egr": "na"}},
        {"t": 20, "type": "dtc", "mode": "07", "codes": ["P0128"]},
        {"t": 30, "type": "freeze_frame", "code": "P0128", "values": {"coolant": 71, "rpm": 812}},
        {"t": 40, "type": "mode06", "results": [
            {"mid": 1, "tid": 128, "uasid": 10, "value": 512, "min": 0, "max": 1023}]},
        {"t": 50, "type": "crank", "rate_hz": 50, "volts": [12.6, 10.1, 9.8, 11.9, 13.8]},
        {"t": 60, "type": "harsh", "kind": "brake", "peak_g": 0.41},
        {"t": 70, "type": "cmd_result", "command_id": 9, "status": "done"},
        {"t": 59000, "type": "trip_end", "reason": "ignition_off"},
    ]  # fmt: skip
    batch["final"] = True
    return batch


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(contract.SCHEMA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- happy path


def test_typical_batch_round_trips():
    raw = with_events(typical_batch())
    b = BatchV1.model_validate(raw)
    assert b.key == ("FM1-0001", 17, 42, 3)
    again = BatchV1.model_validate_json(b.model_dump_json())
    assert again == b
    assert {type(e).__name__ for e in b.ev} >= {"TripStart", "DtcEvent", "Mode06Event"}


def test_exported_schema_accepts_what_pydantic_accepts(schema):
    jsonschema.validate(with_events(typical_batch()), schema)
    critical = {"v": 1, "device_id": "FM1-0001", "boot": 1, "trip": 2, "t": T0,
                "sent_at": T0 + 50, "kind": "coolant_high", "values": {"coolant": 121}}  # fmt: skip
    CriticalV1.model_validate(critical)
    jsonschema.validate(critical, schema)
    ack = AckV1(server_time=T0, cmds=[{"id": 1, "type": "health_scan"}]).model_dump(mode="json")
    jsonschema.validate(ack, schema)


def test_size_budget():
    raw = typical_batch()
    body = json.dumps(raw, separators=(",", ":")).encode()
    gz = gzip.compress(body, compresslevel=6)
    print(f"\n60 s batch: {len(body)} B JSON, {len(gz)} B gzipped "
          f"=> {len(gz) * 60 / 1024:.0f} KB per driving hour")  # fmt: skip
    assert len(gz) < SIZE_BUDGET_BYTES


# ---------------------------------------------------------------- forward compatibility


def test_unknown_signal_and_event_are_accepted():
    raw = typical_batch()
    raw["s"]["future_signal"] = [[0, 1.0]]
    raw["ev"] = [{"t": 0, "type": "tyre_pressure", "kpa": [220, 221, 219, 222]}]
    b = BatchV1.model_validate(raw)
    assert b.ev[0].type == "tyre_pressure"


# ---------------------------------------------------------------- rejections


def _mutate(path_fn):
    raw = with_events(typical_batch())
    path_fn(raw)
    return raw


@pytest.mark.parametrize(
    "name,mutate",
    [
        ("bad device id", lambda r: r.update(device_id="fm1 0001")),
        ("unknown top-level field", lambda r: r.update(colour="blue")),
        ("wrong version", lambda r: r.update(v=2)),
        ("negative seq", lambda r: r.update(seq=-1)),
        ("unsorted samples", lambda r: r["s"]["rpm"].reverse()),
        ("non-finite value", lambda r: r["s"]["rpm"].__setitem__(0, [0, float("nan")])),
        ("too many samples", lambda r: r["s"].update(rpm=[[0, 1.0]] * 3601)),
        ("bad DTC code in a known event", lambda r: r["ev"][4].update(codes=["P012"])),
        ("bad mode in a known event", lambda r: r["ev"][4].update(mode="04")),
        ("GNSS out of range", lambda r: r["g"].__setitem__(0, [0, 95.0, 3.0, 10.0])),
        ("sent before t0", lambda r: r.update(sent_at=r["t0"] - 5000)),
        ("dt beyond 6 h", lambda r: r["s"]["rpm"].append([6 * 3600 * 1000 + 1, 800.0])),
    ],
)
def test_rejects(name, mutate):
    with pytest.raises(ValidationError):
        BatchV1.model_validate(_mutate(mutate))


# ---------------------------------------------------------------- catalogue + drift


def test_signal_catalogue_is_consistent():
    cat = catalogue()
    assert len(cat.signals) >= 25
    for s in cat.signals:
        assert contract.BatchV1.model_fields  # catalogue names must be valid payload keys
        BatchV1.model_validate(typical_batch() | {"s": {s.name: [[0, s.min]]}})
        if s.pid is not None:
            assert len(s.pid) == 2 and int(s.pid, 16) >= 0
    # Ids are stable forever: these must never change meaning.
    assert cat.by_id[1].name == "rpm" and cat.by_id[2].name == "speed"
    assert cat.by_id[10].name == "coolant" and cat.by_id[60].name == "batt_v"


def test_exported_files_are_up_to_date():
    """If this fails: run `uv run python -m app.gateway.contract` and commit contracts/."""
    assert contract.SCHEMA_FILE.read_text(encoding="utf-8") == contract.render(
        contract.schema_document()
    )
    assert contract.SIGNALS_EXPORT.read_text(encoding="utf-8") == contract.render(
        contract.signals_document()
    )
