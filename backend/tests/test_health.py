from app.api.health import engine_dep
from app.db.session import make_engine


async def test_health_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["x-request-id"]


async def test_request_id_is_echoed(client):
    r = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


async def test_ready_ok_with_db_at_head(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"db": "ok", "migrations": "head"}


async def test_ready_503_when_db_down(client):
    # Nothing listens on port 1, so the connection fails fast.
    bad = make_engine("postgresql+asyncpg://x:y@127.0.0.1:1/none", connect_timeout_s=1)
    client.app.dependency_overrides[engine_dep] = lambda: bad
    try:
        r = await client.get("/health/ready")
    finally:
        client.app.dependency_overrides.clear()
        await bad.dispose()
    assert r.status_code == 503
    assert r.json()["checks"]["db"].startswith("error")
