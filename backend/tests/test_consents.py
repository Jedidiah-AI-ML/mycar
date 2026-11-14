from tests.helpers import PASSWORD, signup


def _status(body, purpose):
    return next(p for p in body["purposes"] if p["purpose"] == purpose)


async def test_signup_records_required_consent(client):
    u = await signup(client)
    body = (await client.get("/me/consents", headers=u.headers)).json()
    assert _status(body, "service_processing")["active"] is True
    assert _status(body, "location")["active"] is False
    assert _status(body, "service_processing")["text"]  # wording is served to the UI


async def test_grant_revoke_regrant_keeps_history(client):
    u = await signup(client)
    assert (await client.post("/me/consents/location", headers=u.headers)).status_code == 201
    # Granting again is a no-op, not an error.
    assert (await client.post("/me/consents/location", headers=u.headers)).status_code == 201
    assert (await client.delete("/me/consents/location", headers=u.headers)).status_code == 204
    assert (await client.post("/me/consents/location", headers=u.headers)).status_code == 201

    body = (await client.get("/me/consents", headers=u.headers)).json()
    location_rows = [h for h in body["history"] if h["purpose"] == "location"]
    assert len(location_rows) == 2
    assert location_rows[0]["revoked_at"] is not None
    assert location_rows[1]["revoked_at"] is None


async def test_required_consent_cannot_be_revoked_alone(client):
    u = await signup(client)
    r = await client.delete("/me/consents/service_processing", headers=u.headers)
    assert r.status_code == 409


async def test_revoke_not_granted_is_404(client):
    u = await signup(client)
    assert (
        await client.delete("/me/consents/model_improvement", headers=u.headers)
    ).status_code == 404


async def test_export_contains_user_and_consents(client):
    u = await signup(client, consents=("service_processing", "location"))
    r = await client.get("/me/export", headers=u.headers)
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["email"] == u.email
    assert {c["purpose"] for c in data["consents"]} == {"service_processing", "location"}
    assert any(a["action"] == "account.created" for a in data["activity"])


async def test_delete_account(client):
    u = await signup(client)
    assert (await client.delete("/me", headers=u.headers)).status_code == 204
    # Existing access token stops working at once; login and refresh fail.
    assert (await client.get("/me", headers=u.headers)).status_code == 401
    r = await client.post("/auth/login", json={"email": u.email, "password": PASSWORD})
    assert r.status_code == 401
    assert (
        await client.post("/auth/refresh", json={"refresh_token": u.refresh})
    ).status_code == 401
    # The same email can sign up again while the old account awaits purge.
    await signup(client, u.email)
