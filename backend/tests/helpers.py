from dataclasses import dataclass

PASSWORD = "correct-horse-battery-1"


@dataclass
class TestUser:
    __test__ = False  # not a pytest test class

    id: str
    email: str
    access: str
    refresh: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access}"}


async def signup(client, email="owner@example.com", consents=("service_processing",)) -> TestUser:
    r = await client.post(
        "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": email.split("@")[0],
            "consents": list(consents),
        },
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    t = r.json()
    return TestUser(id=uid, email=email, access=t["access_token"], refresh=t["refresh_token"])
