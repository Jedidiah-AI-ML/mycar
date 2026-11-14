from datetime import UTC, datetime, timedelta

import jwt
import pytest

from tests.helpers import PASSWORD, signup


async def test_register_login_me(client):
    u = await signup(client, "Wisdom@Example.com")
    r = await client.get("/me", headers=u.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "wisdom@example.com"  # stored lower-case
    assert body["timezone"] == "Africa/Lagos"
    assert "password_hash" not in body


async def test_register_duplicate_email_any_case(client):
    await signup(client, "a@example.com")
    r = await client.post(
        "/auth/register",
        json={
            "email": "A@EXAMPLE.com",
            "password": PASSWORD,
            "display_name": "x",
            "consents": ["service_processing"],
        },
    )
    assert r.status_code == 409


async def test_register_requires_service_consent(client):
    r = await client.post(
        "/auth/register",
        json={
            "email": "a@example.com",
            "password": PASSWORD,
            "display_name": "x",
            "consents": ["location"],
        },
    )
    assert r.status_code == 422
    assert "service_processing" in r.json()["detail"]


@pytest.mark.parametrize(
    "patch",
    [{"password": "short"}, {"email": "not-an-email"}, {"display_name": ""}],
)
async def test_register_validation(client, patch):
    body = {
        "email": "a@example.com",
        "password": PASSWORD,
        "display_name": "x",
        "consents": ["service_processing"],
    } | patch
    assert (await client.post("/auth/register", json=body)).status_code == 422


async def test_login_failures_look_identical(client):
    await signup(client, "a@example.com")
    wrong_pw = await client.post(
        "/auth/login", json={"email": "a@example.com", "password": "x" * 12}
    )
    no_user = await client.post(
        "/auth/login", json={"email": "b@example.com", "password": "x" * 12}
    )
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


@pytest.mark.parametrize("header", [None, "Bearer garbage", "Basic abc"])
async def test_me_rejects_missing_or_bad_token(client, header):
    headers = {"Authorization": header} if header else {}
    assert (await client.get("/me", headers=headers)).status_code == 401


async def test_expired_and_wrongly_signed_tokens_rejected(client):
    u = await signup(client)
    past = datetime.now(UTC) - timedelta(minutes=1)
    claims = {"sub": u.id, "type": "access", "exp": past}
    secret = "test-secret-not-for-production-0123456789abcdef"
    expired = jwt.encode(claims, secret, algorithm="HS256")
    forged = jwt.encode(
        claims | {"exp": past + timedelta(hours=1)},
        "other-secret-xxxxxxxxxxxxxxxxxxxxxxxx",
        algorithm="HS256",
    )
    for tok in (expired, forged):
        r = await client.get("/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401


async def test_refresh_rotates_and_detects_reuse(client):
    u = await signup(client)
    r1 = await client.post("/auth/refresh", json={"refresh_token": u.refresh})
    assert r1.status_code == 200
    new = r1.json()
    assert new["refresh_token"] != u.refresh
    assert (
        await client.get("/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    ).status_code == 200

    # Re-using the old token fails, and (theft suspected) also kills the new one.
    assert (
        await client.post("/auth/refresh", json={"refresh_token": u.refresh})
    ).status_code == 401
    assert (
        await client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]})
    ).status_code == 401


async def test_logout_revokes_refresh_token(client):
    u = await signup(client)
    assert (await client.post("/auth/logout", json={"refresh_token": u.refresh})).status_code == 204
    assert (
        await client.post("/auth/refresh", json={"refresh_token": u.refresh})
    ).status_code == 401
    # Unknown token: still 204 (reveals nothing).
    assert (await client.post("/auth/logout", json={"refresh_token": "nope"})).status_code == 204
