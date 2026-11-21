"""Phase 3 step 7: maintenance, service records, odometer and trips endpoints."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.api.maintenance import lagos_today
from app.models import AuditEvent, OdometerReading, Trip, TripStatus
from tests.test_vehicles import add_vehicle, scene  # noqa: F401 - fixture

TODAY = lagos_today()


async def trip(session, vehicle_id, days_ago: float, km: float, *, brakes=0, accels=0,
               idle_s=600, driving_s=1800, source="speed"):  # fmt: skip
    """A summarised trip, as the trip pipeline would leave it."""
    started = datetime.now(UTC) - timedelta(days=days_ago)
    t = Trip(
        vehicle_id=uuid.UUID(str(vehicle_id)), device_boot=0,
        device_trip=int(started.timestamp() * 1000) % 2**31, status=TripStatus.complete,
        started_at=started, ended_at=started + timedelta(seconds=idle_s + driving_s),
        distance_km=km, distance_source=source, idle_s=idle_s, driving_s=driving_s,
        harsh_brake_n=brakes, harsh_accel_n=accels, summarised_at=datetime.now(UTC),
    )  # fmt: skip
    session.add(t)
    await session.commit()


def item(body: dict, key: str) -> dict:
    return next(i for i in body["items"] if i["item"] == key)


# ---------------------------------------------------------------- the forecast view


async def test_new_car_lists_ten_items_all_unknown(client, scene):  # noqa: F811
    r = await client.get(f"/vehicles/{scene.vid}/maintenance", headers=scene.viewer.headers)
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["items"]) == 10 and {i["status"] for i in b["items"]} == {"unknown"}
    oil = item(b, "engine_oil")
    assert oil["base_interval_km"] == 5000 and oil["oil_variant"] == "mineral"
    assert oil["oil_variant_assumed"] is True  # Q1: unknown oil type -> the shorter interval
    assert "tell us when it was last done" in oil["reasons"][0]
    assert b["usage"]["method"] == "prior"  # no trips yet: the Lagos prior
    # The test car was added with its install odometer: that is the estimate until it drives.
    assert b["odometer"]["anchor_source"] == "install"
    assert b["odometer"]["estimate_km"] == b["odometer"]["km_since_anchor"] + 145_000


async def test_a_service_record_drives_the_forecast(client, session, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    h = scene.manager.headers
    r = await client.patch(base, json={"odometer_at_install_km": 100_000,
                                       "installed_at": (TODAY - timedelta(days=40)).isoformat()},
                           headers=h)  # fmt: skip
    assert r.status_code == 200, r.text
    await trip(session, scene.vid, 20, 1_500.0)
    await trip(session, scene.vid, 5, 600.0)
    r = await client.post(f"{base}/service-records", headers=h, json={
        "item_key": "engine_oil", "done_on": (TODAY - timedelta(days=30)).isoformat(),
        "odometer_km": 100_000})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["odometer_check"]["flagged"] is False
    b = (await client.get(f"{base}/maintenance", headers=scene.viewer.headers)).json()
    assert b["odometer"]["estimate_km"] == 102_100
    oil = item(b, "engine_oil")
    # 2,100 km since the change -> 2,900 left; 30 days ago + 3 months by time.
    assert (
        oil["remaining_km"] == 2900 and oil["last_service"] == (TODAY - timedelta(30)).isoformat()
    )
    assert oil["status"] in ("ok", "soon") and b["items"][0]["status"] != "unknown"
    assert b["usage"]["km_per_day"] == pytest.approx(2_100 / 30, abs=0.1)


async def test_oil_type_changes_the_interval(client, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    r = await client.patch(base, json={"oil_type": "full_synthetic"}, headers=scene.owner.headers)
    assert r.status_code == 200 and r.json()["oil_type"] == "full_synthetic"
    oil = item((await client.get(f"{base}/maintenance", headers=scene.owner.headers)).json(),
               "engine_oil")  # fmt: skip
    assert (oil["base_interval_km"], oil["interval_months"]) == (7500, 6)
    assert oil["oil_variant_assumed"] is False
    r = await client.patch(base, json={"oil_type": "olive"}, headers=scene.owner.headers)
    assert r.status_code == 422


async def test_hard_driving_applies_wear_multipliers(client, session, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    for d in range(1, 11):  # 10 trips, 30 km each, 4 hard stops each: 13.3 per 100 km
        await trip(session, scene.vid, d, 30.0, brakes=4, idle_s=1300, driving_s=1000)
    b = (await client.get(f"{base}/maintenance", headers=scene.viewer.headers)).json()
    assert b["wear"]["km_in_window"] == 300 and b["wear"]["harsh_brakes_per_100km"] == 13.33
    assert b["wear"]["idle_share"] == pytest.approx(0.565, abs=0.001)
    brakes = item(b, "brake_inspection")
    assert brakes["wear_factor"] == 0.8 and brakes["interval_km"] == 8000  # above 10 / 100 km
    assert item(b, "engine_oil")["wear_factor"] == 0.9  # idle share above 50 %
    assert item(b, "tyre_rotation")["wear_factor"] == 1.0


# ---------------------------------------------------------------- service records


async def test_service_record_validation(client, scene):  # noqa: F811
    url = f"/vehicles/{scene.vid}/service-records"
    h = scene.owner.headers
    bad = [
        {"item_key": "flux_capacitor", "done_on": TODAY.isoformat()},
        {"item_key": "engine_oil", "done_on": (TODAY + timedelta(days=1)).isoformat()},
        {"item_key": "engine_oil", "done_on": "1985-01-01"},
        {"item_key": "engine_oil", "done_on": TODAY.isoformat(), "odometer_km": -1},
        {"item_key": "engine_oil", "done_on": TODAY.isoformat(), "notes": "x" * 501},
    ]
    for body in bad:
        r = await client.post(url, json=body, headers=h)
        assert r.status_code == 422, body


async def test_back_dated_record_is_checked_as_of_its_own_date(client, session, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    h = scene.owner.headers
    await client.patch(base, json={"odometer_at_install_km": 100_000,
                                   "installed_at": (TODAY - timedelta(days=60)).isoformat()},
                       headers=h)  # fmt: skip
    await trip(session, scene.vid, 50, 1_000.0)
    await trip(session, scene.vid, 10, 2_000.0)  # after the service below
    # Estimate on that day was 101,000; today it's 103,000. 101,050 is right for that day.
    r = await client.post(f"{base}/service-records", headers=h, json={
        "item_key": "tyre_rotation", "done_on": (TODAY - timedelta(days=30)).isoformat(),
        "odometer_km": 101_050})  # fmt: skip
    check = r.json()["odometer_check"]
    assert check["estimate_km"] == 101_000 and check["flagged"] is False


async def test_deleting_a_record_removes_its_odometer_value(client, session, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    h = scene.manager.headers
    typo = {"item_key": "engine_oil", "done_on": TODAY.isoformat(), "odometer_km": 999_999}
    r = await client.post(f"{base}/service-records", headers=h, json=typo)
    rid = r.json()["id"]

    async def readings():
        return await session.scalar(select(func.count()).select_from(OdometerReading))

    assert await readings() == 1
    assert (await client.get(f"{base}/service-records", headers=scene.viewer.headers)).json()[0][
        "id"] == rid  # fmt: skip
    assert (await client.delete(f"{base}/service-records/{rid}", headers=h)).status_code == 204
    assert await readings() == 0  # the typo can't linger as an anchor
    assert (await client.delete(f"{base}/service-records/{rid}", headers=h)).status_code == 404
    actions = (await session.scalars(select(AuditEvent.action))).all()
    assert "service.recorded" in actions and "service.deleted" in actions


async def test_a_record_of_another_car_is_not_found(client, session, scene):  # noqa: F811
    other = await add_vehicle(client, scene.stranger, vin=None)
    rec = {"item_key": "coolant", "done_on": TODAY.isoformat()}
    url = f"/vehicles/{other['id']}/service-records"
    rid = (await client.post(url, headers=scene.stranger.headers, json=rec)).json()["id"]
    r = await client.delete(
        f"/vehicles/{scene.vid}/service-records/{rid}", headers=scene.owner.headers
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- odometer


async def test_odometer_entries_agree_or_get_flagged(client, session, scene):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    h = scene.manager.headers
    await client.patch(base, json={"odometer_at_install_km": 50_000,
                                   "installed_at": (TODAY - timedelta(days=20)).isoformat()},
                       headers=h)  # fmt: skip
    await trip(session, scene.vid, 10, 400.0)
    r = await client.post(f"{base}/odometer", json={"km": 50_420}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["mismatch"] is False and "agrees" in r.json()["message"]
    r = await client.post(f"{base}/odometer", json={"km": 30_000}, headers=h)
    assert r.json()["mismatch"] is True and "differs" in r.json()["message"]
    b = (await client.get(f"{base}/maintenance", headers=h)).json()
    assert b["odometer"]["estimate_km"] == 50_420  # the flagged entry isn't trusted
    assert b["odometer"]["last_entry_mismatch"] is True
    assert (await client.post(f"{base}/odometer", json={"km": -5}, headers=h)).status_code == 422


# ---------------------------------------------------------------- trips


async def test_trips_newest_first_with_paging(client, session, scene):  # noqa: F811
    for d in (1, 2, 3):
        await trip(session, scene.vid, d, 10.0 * d)
    url = f"/vehicles/{scene.vid}/trips"
    r = await client.get(url, params={"limit": 2}, headers=scene.viewer.headers)
    assert r.status_code == 200
    page = r.json()
    assert [t["distance_km"] for t in page] == [10.0, 20.0]
    r = await client.get(url, params={"limit": 2, "before": page[-1]["started_at"]},
                         headers=scene.viewer.headers)  # fmt: skip
    assert [t["distance_km"] for t in r.json()] == [30.0]
    assert (
        await client.get(url, params={"limit": 101}, headers=scene.viewer.headers)
    ).status_code == 422


# ---------------------------------------------------------------- role matrix

EXPECTED = {
    #                owner manager viewer stranger
    "maintenance":   (200, 200, 200, 404),
    "list_records":  (200, 200, 200, 404),
    "add_record":    (201, 201, 403, 404),
    "delete_record": (204, 204, 403, 404),
    "odometer":      (201, 201, 403, 404),
    "trips":         (200, 200, 200, 404),
}  # fmt: skip
ROLES = list(enumerate(["owner", "manager", "viewer", "stranger"]))


@pytest.mark.parametrize("action", list(EXPECTED))
@pytest.mark.parametrize("role_idx,role", ROLES)
async def test_maintenance_role_matrix(client, scene, action, role_idx, role):  # noqa: F811
    base = f"/vehicles/{scene.vid}"
    rec = {"item_key": "engine_oil", "done_on": date.today().isoformat()}
    if action == "delete_record":
        rid = (await client.post(f"{base}/service-records", json=rec,
                                 headers=scene.owner.headers)).json()["id"]  # fmt: skip
    h = getattr(scene, role).headers
    match action:
        case "maintenance":
            r = await client.get(f"{base}/maintenance", headers=h)
        case "list_records":
            r = await client.get(f"{base}/service-records", headers=h)
        case "add_record":
            r = await client.post(f"{base}/service-records", json=rec, headers=h)
        case "delete_record":
            r = await client.delete(f"{base}/service-records/{rid}", headers=h)
        case "odometer":
            r = await client.post(f"{base}/odometer", json={"km": 1000}, headers=h)
        case "trips":
            r = await client.get(f"{base}/trips", headers=h)
    assert r.status_code == EXPECTED[action][role_idx], f"{role} {action}: {r.status_code} {r.text}"
