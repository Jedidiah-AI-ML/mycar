"""Phase 2 step 7 gate: provisioning, claiming and device status.

- the token and claim code are shown once and never stored in plain text
- a wrong claim code -> 403 (same as an unknown device)
- a device can't be in two cars, a car can't have two devices
- claim / unclaim are audited
- role matrix for the device routes
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.gateway import auth
from app.models import AuditEvent, Device, DeviceAssignment
from tests.helpers import signup
from tests.test_vehicles import add_vehicle, scene  # noqa: F401 - fixture


async def make_admin(client, session, email="admin@x.com"):
    a = await signup(client, email)
    await session.execute(text("UPDATE users SET is_admin = true WHERE email = :e"), {"e": email})
    await session.commit()
    return a


async def provision(client, admin, **body) -> dict:
    r = await client.post("/admin/devices", json=body, headers=admin.headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- provisioning


async def test_provision_returns_secrets_once_and_stores_only_hashes(client, session):
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    assert d["id"] == "FM1-0001" and d["type"] == "freematics_one_plus"
    device_id, secret = auth.parse_token(d["token"])
    assert device_id == d["id"] and len(secret) >= 40
    assert d["label_qr"] == f"autocare:claim:{d['id']}:{d['claim_code']}"

    # Nothing in the row contains the secret or the claim code in plain text.
    row = (await session.execute(text("SELECT * FROM devices"))).mappings().one()
    dump = " ".join(str(v) for v in row.values())
    for plain in (secret, d["token"], d["claim_code"], auth.normalise_claim_code(d["claim_code"])):
        assert plain not in dump
    assert row["token_hash"] == auth.hash_secret(secret)
    assert row["claim_code_hash"] == auth.hash_claim_code(d["claim_code"])
    assert row["claim_code_hash"] != auth.hash_secret(auth.normalise_claim_code(d["claim_code"]))

    # No route ever returns them again.
    assert (await provision(client, admin))["id"] == "FM1-0002"
    events = (await session.scalars(select(AuditEvent.action))).all()
    assert events.count("device.provisioned") == 2


async def test_provision_explicit_id_conflict_and_admin_only(client, session):
    admin = await make_admin(client, session)
    d = await provision(client, admin, id="fm1-0100", sim_iccid="8923401234567890123")
    assert d["id"] == "FM1-0100"
    assert (await provision(client, admin))["id"] == "FM1-0101"  # continues after the highest
    r = await client.post("/admin/devices", json={"id": "FM1-0100"}, headers=admin.headers)
    assert r.status_code == 409
    for bad in ({"id": "bad id!"}, {"type": "teltonika"}, {"sim_iccid": "123"}):
        r = await client.post("/admin/devices", json=bad, headers=admin.headers)
        assert r.status_code == 422, bad
    user = await signup(client, "someone@x.com")
    r = await client.post("/admin/devices", json={}, headers=user.headers)
    assert r.status_code == 404  # admin routes aren't advertised
    assert (await client.post("/admin/devices", json={})).status_code == 401


# ---------------------------------------------------------------- device token check


async def test_device_token_authentication(client, session):
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    ok = await auth.authenticate_device(session, f"Bearer {d['token']}")
    assert ok is not None and ok.id == d["id"]
    device_id, secret = auth.parse_token(d["token"])
    wrong = auth.device_token(device_id, auth.new_device_secret())
    other = auth.device_token("FM1-9999", secret)
    for header in (None, "", d["token"], "Bearer nonsense", f"Bearer {wrong}", f"Bearer {other}",
                   f"Basic {d['token']}"):  # fmt: skip
        assert await auth.authenticate_device(session, header) is None, header
    await session.execute(update(Device).values(disabled_at=datetime.now(UTC)))
    assert await auth.authenticate_device(session, f"Bearer {d['token']}") is None


def test_claim_code_format_and_normalisation():
    codes = {auth.new_claim_code() for _ in range(200)}
    assert len(codes) == 200
    for c in codes:
        assert len(c) == 9 and c[4] == "-"
        assert set(c.replace("-", "")) <= set(auth.CLAIM_ALPHABET)
    c = next(iter(codes))
    assert auth.hash_claim_code(c) == auth.hash_claim_code(c.lower().replace("-", " "))


# ---------------------------------------------------------------- claiming


async def test_claim_status_unclaim_flow_is_audited(client, session, scene):  # noqa: F811
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    url = f"/vehicles/{scene.vid}/device"
    body = {"device_id": d["id"], "claim_code": d["claim_code"].lower()}

    r = await client.post(url, json=body, headers=scene.manager.headers)
    assert r.status_code == 201, r.text
    st = r.json()
    assert st["device_id"] == d["id"] and st["online"] is False and st["signal"] == "unknown"
    assert st["data_used_this_month_bytes"] == 0
    assert "token" not in r.text and d["claim_code"] not in r.text
    # Claiming again for the same car is a harmless no-op.
    assert (await client.post(url, json=body, headers=scene.owner.headers)).status_code == 200

    r = await client.get(url, headers=scene.viewer.headers)
    assert r.status_code == 200 and r.json()["device_id"] == d["id"]

    assert (await client.delete(url, headers=scene.owner.headers)).status_code == 204
    assert (await client.get(url, headers=scene.viewer.headers)).status_code == 404
    assert (await client.delete(url, headers=scene.owner.headers)).status_code == 404

    events = (
        await session.execute(
            select(AuditEvent.action, AuditEvent.actor_user_id, AuditEvent.details).where(
                AuditEvent.action.like("device.%claimed")
            )
        )
    ).all()
    assert [(a, str(u), det["device_id"]) for a, u, det in events] == [
        ("device.claimed", scene.manager.id, d["id"]),
        ("device.unclaimed", scene.owner.id, d["id"]),
    ]


async def test_wrong_claim_code_and_unknown_device_are_both_403(client, session, scene):  # noqa: F811
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    url = f"/vehicles/{scene.vid}/device"
    wrong = auth.new_claim_code()
    for body in (
        {"device_id": d["id"], "claim_code": wrong},
        {"device_id": "FM1-9999", "claim_code": d["claim_code"]},
    ):
        r = await client.post(url, json=body, headers=scene.owner.headers)
        assert r.status_code == 403
        assert r.json()["detail"] == "Device ID or claim code is wrong"
    await session.execute(update(Device).values(disabled_at=datetime.now(UTC)))
    await session.commit()
    body = {"device_id": d["id"], "claim_code": d["claim_code"]}
    assert (await client.post(url, json=body, headers=scene.owner.headers)).status_code == 403


async def test_one_device_per_car_and_one_car_per_device(client, session):
    admin = await make_admin(client, session)
    d1, d2 = await provision(client, admin), await provision(client, admin)
    alice = await signup(client, "alice@x.com")
    bob = await signup(client, "bob@x.com")
    car_a = (await add_vehicle(client, alice))["id"]
    car_b = (await add_vehicle(client, bob, vin=None))["id"]

    def claim(car, d, who):
        return client.post(
            f"/vehicles/{car}/device",
            json={"device_id": d["id"], "claim_code": d["claim_code"]},
            headers=who.headers,
        )

    assert (await claim(car_a, d1, alice)).status_code == 201
    # Bob knows d1's code (e.g. bought the car's old label) but it's in Alice's car.
    r = await claim(car_b, d1, bob)
    assert r.status_code == 409 and "another car" in r.json()["detail"]
    # Alice's car can't take a second device.
    r = await claim(car_a, d2, alice)
    assert r.status_code == 409 and "already has a device" in r.json()["detail"]
    # Once Alice unlinks it, Bob can claim it; history is kept.
    assert (await client.delete(f"/vehicles/{car_a}/device", headers=alice.headers)).status_code
    assert (await claim(car_b, d1, bob)).status_code == 201
    rows = (await session.scalars(select(DeviceAssignment))).all()
    assert len(rows) == 2 and sum(r.unassigned_at is None for r in rows) == 1


async def test_deleting_the_car_frees_its_device(client, session):
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    alice = await signup(client, "alice@x.com")
    car = (await add_vehicle(client, alice))["id"]
    body = {"device_id": d["id"], "claim_code": d["claim_code"]}
    assert (
        await client.post(f"/vehicles/{car}/device", json=body, headers=alice.headers)
    ).status_code == 201
    assert (await client.delete(f"/vehicles/{car}", headers=alice.headers)).status_code == 204
    car2 = (await add_vehicle(client, alice, vin=None))["id"]
    r = await client.post(f"/vehicles/{car2}/device", json=body, headers=alice.headers)
    assert r.status_code == 201


async def test_status_online_signal_data_and_capabilities(client, session, scene):  # noqa: F811
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    url = f"/vehicles/{scene.vid}/device"
    body = {"device_id": d["id"], "claim_code": d["claim_code"]}
    await client.post(url, json=body, headers=scene.owner.headers)
    now = datetime.now(UTC)
    await session.execute(
        update(Device).values(
            last_seen_at=now - timedelta(seconds=60), last_rssi=-80, bytes_month=12_345,
            bytes_month_start=now.date().replace(day=1), firmware_version="0.1.0",
            capabilities={"mode06": False},
        )
    )  # fmt: skip
    await session.commit()
    st = (await client.get(url, headers=scene.viewer.headers)).json()
    assert st["online"] is True and st["signal"] == "fair" and st["firmware_version"] == "0.1.0"
    assert st["data_used_this_month_bytes"] == 12_345
    caps = st["capabilities"]
    assert caps["mode06"] is False and caps["mode03"] is True
    assert caps["gnss"] is False  # the owner signed up without location consent

    r = await client.post("/me/consents/location", headers=scene.owner.headers)
    assert r.status_code == 201, r.text
    await session.execute(
        update(Device).values(
            last_seen_at=now - timedelta(minutes=6),
            bytes_month_start=(now.date().replace(day=1) - timedelta(days=1)).replace(day=1),
        )
    )
    await session.commit()
    st = (await client.get(url, headers=scene.viewer.headers)).json()
    assert st["capabilities"]["gnss"] is True
    assert st["online"] is False  # seen 6 min ago (window: 5 min, SEED P9)
    assert st["data_used_this_month_bytes"] == 0  # counter is from last month


# ---------------------------------------------------------------- role matrix

EXPECTED = {
    #             owner manager viewer stranger
    "claim":     (201, 201, 403, 404),
    "status":    (200, 200, 200, 404),
    "unclaim":   (204, 204, 403, 404),
}  # fmt: skip


@pytest.mark.parametrize("action", list(EXPECTED))
@pytest.mark.parametrize(
    "role_idx,role", list(enumerate(["owner", "manager", "viewer", "stranger"]))
)
async def test_device_role_matrix(client, session, scene, action, role_idx, role):  # noqa: F811
    admin = await make_admin(client, session)
    d = await provision(client, admin)
    url = f"/vehicles/{scene.vid}/device"
    body = {"device_id": d["id"], "claim_code": d["claim_code"]}
    if action != "claim":
        assert (await client.post(url, json=body, headers=scene.owner.headers)).status_code == 201
    who = getattr(scene, role).headers
    match action:
        case "claim":
            r = await client.post(url, json=body, headers=who)
        case "status":
            r = await client.get(url, headers=who)
        case "unclaim":
            r = await client.delete(url, headers=who)
    assert r.status_code == EXPECTED[action][role_idx], f"{role} {action}: {r.status_code} {r.text}"
