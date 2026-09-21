from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import get_engine
from tests.conftest import auth, img_file, make_image


async def create_key(client, token, **body):
    payload = {"name": "server", "scopes": ["faces:upload", "faces:search"], "expires_in": "30d", **body}
    r = await client.post("/api/v1/me/api-keys", headers=auth(token), json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def test_create_and_use_api_key(client, user_token):
    d = await create_key(client, user_token)
    assert d["api_key"].startswith("fr_live_") and d["key_prefix"] == d["api_key"][:16]
    assert d["status"] == "active" and d["expires_at"] is not None

    # list never returns the raw key
    r = await client.get("/api/v1/me/api-keys", headers=auth(user_token))
    assert "api_key" not in r.json()["data"][0]

    # hash only in DB
    async with get_engine().connect() as conn:
        row = (await conn.execute(text("SELECT key_hash FROM api_keys"))).one()
    assert d["api_key"] not in row[0] and len(row[0]) == 64

    # use via X-API-Key and via Bearer
    r = await client.get("/api/v1/me", headers={"X-API-Key": d["api_key"]})
    assert r.status_code == 200 and r.json()["meta"]["auth_type"] == "api_key"
    r = await client.get("/api/v1/me", headers=auth(d["api_key"]))
    assert r.status_code == 200

    # upload with the key
    r = await client.post("/api/v1/faces", headers={"X-API-Key": d["api_key"]},
                          files=img_file(make_image()), data={"name": "Key User"})
    assert r.status_code == 201, r.text


async def test_scope_enforced(client, user_token):
    d = await create_key(client, user_token, scopes=["faces:search"])
    r = await client.post("/api/v1/faces", headers={"X-API-Key": d["api_key"]}, files=img_file(make_image()), data={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    # api keys cannot manage api keys
    r = await client.get("/api/v1/me/api-keys", headers={"X-API-Key": d["api_key"]})
    assert r.status_code == 403


async def test_invalid_key(client):
    r = await client.get("/api/v1/me", headers={"X-API-Key": "fr_live_doesnotexist"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_API_KEY"


async def test_revoke_key(client, user_token):
    d = await create_key(client, user_token)
    r = await client.delete(f"/api/v1/me/api-keys/{d['id']}", headers=auth(user_token))
    assert r.status_code == 200 and r.json()["data"]["status"] == "revoked"
    r = await client.get("/api/v1/me", headers={"X-API-Key": d["api_key"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_expired_key(client, user_token):
    d = await create_key(client, user_token, expires_in=None,
                         expires_at=(datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat())
    async with get_engine().begin() as conn:  # fast-forward expiry
        await conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 minute'"))
    r = await client.get("/api/v1/me", headers={"X-API-Key": d["api_key"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"
    r = await client.get("/api/v1/me/api-keys", headers=auth(user_token))
    assert r.json()["data"][0]["status"] == "expired"


async def test_expiry_validation(client, user_token):
    r = await client.post("/api/v1/me/api-keys", headers=auth(user_token), json={"name": "x", "expires_in": "2w"})
    assert r.status_code == 422
    r = await client.post("/api/v1/me/api-keys", headers=auth(user_token), json={"name": "x", "expires_in": "never"})
    assert r.status_code == 201 and r.json()["data"]["expires_at"] is None
    r = await client.post("/api/v1/me/api-keys", headers=auth(user_token), json={"name": "x", "scopes": ["admin:all"]})
    assert r.status_code == 422


async def test_rotate_key(client, user_token):
    d = await create_key(client, user_token)
    r = await client.post(f"/api/v1/me/api-keys/{d['id']}/rotate", headers=auth(user_token))
    assert r.status_code == 200, r.text
    n = r.json()["data"]
    assert n["api_key"] != d["api_key"] and n["id"] != d["id"] and n["scopes"] == d["scopes"]
    assert (await client.get("/api/v1/me", headers={"X-API-Key": d["api_key"]})).status_code == 401
    assert (await client.get("/api/v1/me", headers={"X-API-Key": n["api_key"]})).status_code == 200


async def test_cannot_touch_other_users_keys(client):
    from tests.conftest import make_jwt
    a, b = make_jwt(), make_jwt()
    d = await create_key(client, a)
    assert (await client.delete(f"/api/v1/me/api-keys/{d['id']}", headers=auth(b))).status_code == 404
    assert (await client.post(f"/api/v1/me/api-keys/{d['id']}/rotate", headers=auth(b))).status_code == 404


async def test_suspended_user_key_stops_working(client, user_token, admin_token):
    d = await create_key(client, user_token)
    uid = (await client.get("/api/v1/me", headers=auth(user_token))).json()["data"]["id"]
    await client.patch(f"/api/v1/admin/users/{uid}/status", headers=auth(admin_token), json={"status": "suspended"})
    r = await client.get("/api/v1/me", headers={"X-API-Key": d["api_key"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "USER_SUSPENDED"
