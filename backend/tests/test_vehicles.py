from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.models import AuditEvent
from tests.helpers import TestUser, signup

COROLLA = {
    "make": "Toyota",
    "model": "Corolla",
    "year": 2010,
    "engine": "2ZR-FE 1.8L I4",
    "displacement_l": "1.8",
    "cylinders": 4,
    "odometer_at_install_km": 145000,
}


async def add_vehicle(client, u: TestUser, **over) -> dict:
    r = await client.post("/vehicles", json=COROLLA | over, headers=u.headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- CRUD


async def test_create_list_get_update_delete(client):
    o = await signup(client)
    v = await add_vehicle(client, o, vin="1M8GDM9AXKP042788")
    assert v["my_role"] == "owner"
    assert v["fuel_type"] == "petrol"
    assert v["warnings"] == []

    listed = (await client.get("/vehicles", headers=o.headers)).json()
    assert [x["id"] for x in listed] == [v["id"]]

    r = await client.patch(f"/vehicles/{v['id']}", json={"nickname": "Blue"}, headers=o.headers)
    assert r.status_code == 200
    assert r.json()["nickname"] == "Blue"
    assert r.json()["make"] == "Toyota"  # untouched fields stay

    assert (await client.delete(f"/vehicles/{v['id']}", headers=o.headers)).status_code == 204
    assert (await client.get(f"/vehicles/{v['id']}", headers=o.headers)).status_code == 404
    assert (await client.get("/vehicles", headers=o.headers)).json() == []


@pytest.mark.parametrize(
    "over",
    [
        {"fuel_type": "diesel"},
        {"vin": "1M8GDM9AXKPO42788"},  # contains O
        {"vin": "TOO-SHORT"},
        {"year": 1990},
        {"year": 2999},
        {"odometer_at_install_km": -5},
        {"displacement_l": "12.0"},
        {"make": ""},
    ],
)
async def test_create_validation(client, over):
    o = await signup(client)
    r = await client.post("/vehicles", json=COROLLA | over, headers=o.headers)
    assert r.status_code == 422, r.text


async def test_bad_check_digit_creates_with_warning(client):
    o = await signup(client)
    v = await add_vehicle(client, o, vin="1M8GDM9A1KP042788")
    assert v["warnings"]


async def test_duplicate_active_vin_conflicts(client):
    a = await signup(client, "a@x.com")
    b = await signup(client, "b@x.com")
    v = await add_vehicle(client, a, vin="1M8GDM9AXKP042788")
    r = await client.post(
        "/vehicles", json=COROLLA | {"vin": "1M8GDM9AXKP042788"}, headers=b.headers
    )
    assert r.status_code == 409
    # Once the first is deleted, the VIN is free again (e.g. the car was sold).
    await client.delete(f"/vehicles/{v['id']}", headers=a.headers)
    await add_vehicle(client, b, vin="1M8GDM9AXKP042788")


async def test_patch_cannot_null_required_fields(client):
    o = await signup(client)
    v = await add_vehicle(client, o)
    r = await client.patch(f"/vehicles/{v['id']}", json={"make": None}, headers=o.headers)
    assert r.status_code == 422


# ---------------------------------------------------------------- role matrix


@dataclass
class Scene:
    owner: TestUser
    manager: TestUser
    viewer: TestUser
    target: TestUser  # a second viewer that access-changing requests act on
    stranger: TestUser
    vid: str


@pytest.fixture
async def scene(client) -> Scene:
    o = await signup(client, "owner@x.com")
    m = await signup(client, "manager@x.com")
    v = await signup(client, "viewer@x.com")
    t = await signup(client, "target@x.com")
    s = await signup(client, "stranger@x.com")
    vid = (await add_vehicle(client, o))["id"]
    for who, role in ((m, "manager"), (v, "viewer"), (t, "viewer")):
        r = await client.post(
            f"/vehicles/{vid}/access", json={"email": who.email, "role": role}, headers=o.headers
        )
        assert r.status_code == 201, r.text
    return Scene(o, m, v, t, s, vid)


EXPECTED = {
    #                 owner manager viewer stranger
    "get_vehicle":    (200, 200, 200, 404),
    "patch_vehicle":  (200, 200, 403, 404),
    "delete_vehicle": (204, 403, 403, 404),
    "list_access":    (200, 200, 403, 404),
    "share":          (201, 403, 403, 404),
    "change_role":    (200, 403, 403, 404),
    "remove_other":   (204, 403, 403, 404),
}  # fmt: skip


async def _call(client, action: str, sc: Scene, who: TestUser):
    base, h = f"/vehicles/{sc.vid}", who.headers
    match action:
        case "get_vehicle":
            return await client.get(base, headers=h)
        case "patch_vehicle":
            return await client.patch(base, json={"nickname": "x"}, headers=h)
        case "delete_vehicle":
            return await client.delete(base, headers=h)
        case "list_access":
            return await client.get(f"{base}/access", headers=h)
        case "share":
            new = await signup(client, f"new-{who.email}")
            return await client.post(
                f"{base}/access", json={"email": new.email, "role": "viewer"}, headers=h
            )
        case "change_role":
            return await client.patch(
                f"{base}/access/{sc.target.id}", json={"role": "manager"}, headers=h
            )
        case "remove_other":
            return await client.delete(f"{base}/access/{sc.target.id}", headers=h)
    raise AssertionError(action)


@pytest.mark.parametrize("action", list(EXPECTED))
@pytest.mark.parametrize(
    "role_idx,role", list(enumerate(["owner", "manager", "viewer", "stranger"]))
)
async def test_role_matrix(client, scene, action, role_idx, role):
    r = await _call(client, action, scene, getattr(scene, role))
    assert r.status_code == EXPECTED[action][role_idx], f"{role} {action}: {r.status_code} {r.text}"


# ---------------------------------------------------------------- sharing rules


async def test_shared_user_sees_vehicle_with_role(client, scene):
    listed = (await client.get("/vehicles", headers=scene.viewer.headers)).json()
    assert [(x["id"], x["my_role"]) for x in listed] == [(scene.vid, "viewer")]


async def test_revoked_user_loses_access(client, scene):
    await client.delete(
        f"/vehicles/{scene.vid}/access/{scene.viewer.id}", headers=scene.owner.headers
    )
    r = await client.get(f"/vehicles/{scene.vid}", headers=scene.viewer.headers)
    assert r.status_code == 404


async def test_non_owner_can_leave(client, scene):
    r = await client.delete(
        f"/vehicles/{scene.vid}/access/{scene.viewer.id}", headers=scene.viewer.headers
    )
    assert r.status_code == 204
    assert (await client.get("/vehicles", headers=scene.viewer.headers)).json() == []


async def test_owner_cannot_be_removed_or_demoted(client, scene):
    base = f"/vehicles/{scene.vid}/access/{scene.owner.id}"
    h = scene.owner.headers
    assert (await client.delete(base, headers=h)).status_code == 409
    assert (await client.patch(base, json={"role": "viewer"}, headers=h)).status_code == 409


async def test_share_errors(client, scene):
    url, h = f"/vehicles/{scene.vid}/access", scene.owner.headers
    r = await client.post(url, json={"email": "nobody@x.com", "role": "viewer"}, headers=h)
    assert r.status_code == 404
    r = await client.post(url, json={"email": scene.viewer.email, "role": "viewer"}, headers=h)
    assert r.status_code == 409
    r = await client.post(url, json={"email": scene.stranger.email, "role": "owner"}, headers=h)
    assert r.status_code == 422


async def test_sharing_is_audited(client, scene, session):
    await client.delete(
        f"/vehicles/{scene.vid}/access/{scene.target.id}", headers=scene.owner.headers
    )
    actions = (
        await session.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.subject_id == scene.vid)
            .order_by(AuditEvent.id)
        )
    ).all()
    assert actions == [
        "vehicle.created",
        "access.granted",
        "access.granted",
        "access.granted",
        "access.revoked",
    ]


async def test_export_includes_vehicles_and_owner_delete_cascades(client, scene):
    data = (await client.get("/me/export", headers=scene.viewer.headers)).json()
    assert [v["vehicle"]["id"] for v in data["vehicles"]] == [scene.vid]
    # Owner deletes their account -> the vehicle disappears for everyone.
    assert (await client.delete("/me", headers=scene.owner.headers)).status_code == 204
    r = await client.get(f"/vehicles/{scene.vid}", headers=scene.viewer.headers)
    assert r.status_code == 404
