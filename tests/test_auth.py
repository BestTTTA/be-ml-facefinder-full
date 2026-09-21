import uuid

from tests.conftest import auth, make_jwt


async def test_valid_jwt_creates_profile_and_subscription(client):
    uid = str(uuid.uuid4())
    r = await client.get("/api/v1/me", headers=auth(make_jwt(uid, email="a@example.com", name="Alice")))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["data"]["id"] == uid
    assert body["data"]["email"] == "a@example.com"
    assert body["data"]["name"] == "Alice"
    assert body["data"]["status"] == "active"
    assert body["meta"]["auth_type"] == "jwt"
    assert "X-RateLimit-Limit" in r.headers and "X-Request-ID" in r.headers

    r = await client.get("/api/v1/me/package", headers=auth(make_jwt(uid)))
    assert r.json()["data"]["name"] == "Free"


async def test_missing_auth(client):
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_invalid_jwt(client):
    r = await client.get("/api/v1/me", headers=auth(make_jwt(secret="wrong-secret")))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"
    r = await client.get("/api/v1/me", headers=auth("garbage.token.value"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


async def test_expired_jwt(client):
    r = await client.get("/api/v1/me", headers=auth(make_jwt(exp_delta=-10)))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_wrong_audience(client):
    r = await client.get("/api/v1/me", headers=auth(make_jwt(aud="anon")))
    assert r.status_code == 401


async def test_suspended_user(client, admin_token):
    uid = str(uuid.uuid4())
    tok = make_jwt(uid)
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 200
    r = await client.patch(f"/api/v1/admin/users/{uid}/status", headers=auth(admin_token),
                           json={"status": "suspended", "reason": "abuse"})
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/me", headers=auth(tok))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "USER_SUSPENDED"
    # reactivate
    await client.patch(f"/api/v1/admin/users/{uid}/status", headers=auth(admin_token), json={"status": "active"})
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 200


async def test_me_endpoints(client, user_token):
    for path in ("/api/v1/me/profile", "/api/v1/me/status", "/api/v1/me/package", "/api/v1/me/quota",
                 "/api/v1/me/usage", "/api/v1/usage", "/api/v1/me/usage/history"):
        r = await client.get(path, headers=auth(user_token))
        assert r.status_code == 200, (path, r.text)
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"] == {"used": 0, "limit": 100, "remaining": 100, "unlimited": False}
    assert usage["package"]["name"] == "Free"
    assert usage["period"]["start"] < usage["period"]["end"]


async def test_refresh_requires_supabase_config(client):
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


async def test_error_envelope_on_validation(client, user_token):
    r = await client.post("/api/v1/me/api-keys", headers=auth(user_token), json={"name": ""})
    assert r.status_code == 422
    assert r.json()["success"] is False
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_security_headers_and_404_envelope(client):
    r = await client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
