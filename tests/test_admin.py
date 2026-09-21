import uuid

from tests.conftest import auth, img_file, make_image, make_jwt

LOGIN = "/api/v1/admin/auth/login"


async def test_admin_login_and_me(client, admin_token):
    r = await client.get("/api/v1/admin/auth/me", headers=auth(admin_token))
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["username"] == "superadmin" and d["roles"] == ["SUPER_ADMIN"] and "admin.manage" in d["permissions"]


async def test_admin_invalid_password(client):
    r = await client.post(LOGIN, json={"username": "superadmin", "password": "wrong-password-123"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = await client.post(LOGIN, json={"username": "nobody", "password": "wrong-password-123"})
    assert r.status_code == 401


async def test_admin_endpoints_reject_user_jwt_and_no_token(client, user_token):
    assert (await client.get("/api/v1/admin/users")).status_code == 403
    r = await client.get("/api/v1/admin/users", headers=auth(user_token))
    assert r.status_code == 401  # a Supabase JWT is not an admin token


async def test_admin_logout_revokes_session(client, admin_token):
    assert (await client.post("/api/v1/admin/auth/logout", headers=auth(admin_token))).status_code == 200
    r = await client.get("/api/v1/admin/auth/me", headers=auth(admin_token))
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_rbac(client, admin_token):
    # create a VIEWER and a SUPPORT admin
    for username, role in (("viewer1", "VIEWER"), ("support1", "SUPPORT")):
        r = await client.post("/api/v1/admin/admins", headers=auth(admin_token),
                              json={"username": username, "password": "another-long-password", "roles": [role]})
        assert r.status_code == 201, r.text
    viewer = (await client.post(LOGIN, json={"username": "viewer1", "password": "another-long-password"})).json()["data"]["token"]
    support = (await client.post(LOGIN, json={"username": "support1", "password": "another-long-password"})).json()["data"]["token"]

    assert (await client.get("/api/v1/admin/users", headers=auth(viewer))).status_code == 200
    assert (await client.get("/api/v1/admin/audit-logs", headers=auth(viewer))).status_code == 200
    # viewer cannot write
    r = await client.post("/api/v1/admin/packages", headers=auth(viewer),
                          json={"name": "X", "upload_limit": 1, "search_limit": 1, "storage_limit": 1, "api_rate_limit": 1})
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert r.json()["error"]["details"]["required_permission"] == "packages.write"
    # support cannot read audit logs or manage admins
    assert (await client.get("/api/v1/admin/audit-logs", headers=auth(support))).status_code == 403
    assert (await client.get("/api/v1/admin/admins", headers=auth(support))).status_code == 403
    # weak password rejected
    r = await client.post("/api/v1/admin/admins", headers=auth(admin_token),
                          json={"username": "weak", "password": "short", "roles": ["VIEWER"]})
    assert r.status_code == 422


async def test_user_management_flow(client, admin_token):
    uid = str(uuid.uuid4())
    tok = make_jwt(uid, email="managed@example.com", name="Managed")
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 200
    await client.post("/api/v1/faces", headers=auth(tok), files=img_file(make_image()), data={"name": "P"})

    # list / search / get
    r = await client.get("/api/v1/admin/users?q=managed", headers=auth(admin_token))
    assert r.json()["meta"]["total"] == 1 and r.json()["data"][0]["package"]["name"] == "Free"
    r = await client.get(f"/api/v1/admin/users/{uid}", headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["data"]["counts"] == {"persons": 1, "faces": 1}

    # usage / faces / history
    r = await client.get(f"/api/v1/admin/users/{uid}/usage", headers=auth(admin_token))
    assert r.json()["data"]["uploads"]["used"] == 1 and len(r.json()["data"]["history"]) == 1
    assert (await client.get(f"/api/v1/admin/users/{uid}/faces", headers=auth(admin_token))).json()["meta"]["total"] == 1
    assert len((await client.get(f"/api/v1/admin/users/{uid}/uploads", headers=auth(admin_token))).json()["data"]) == 1

    # change package
    pkgs = (await client.get("/api/v1/admin/packages", headers=auth(admin_token))).json()["data"]
    pro = next(p for p in pkgs if p["name"] == "Pro")
    r = await client.patch(f"/api/v1/admin/users/{uid}/package", headers=auth(admin_token), json={"package_id": pro["id"]})
    assert r.status_code == 200 and r.json()["data"]["package"]["name"] == "Pro"
    r = await client.get("/api/v1/me/usage", headers=auth(tok))
    assert r.json()["data"]["uploads"]["limit"] == 10000 and r.headers["X-RateLimit-Limit"] == "300"

    # change role
    r = await client.patch(f"/api/v1/admin/users/{uid}/role", headers=auth(admin_token), json={"role": "member"})
    assert r.json()["data"]["role"] == "member"

    # suspend + activate
    r = await client.patch(f"/api/v1/admin/users/{uid}/status", headers=auth(admin_token), json={"status": "suspended"})
    assert r.json()["data"]["status"] == "suspended"
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 403
    r = await client.patch(f"/api/v1/admin/users/{uid}/status", headers=auth(admin_token), json={"status": "active"})
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 200

    # delete
    r = await client.delete(f"/api/v1/admin/users/{uid}", headers=auth(admin_token))
    assert r.status_code == 200
    assert (await client.get("/api/v1/me", headers=auth(tok))).status_code == 404
    assert (await client.get(f"/api/v1/admin/users/{uuid.uuid4()}", headers=auth(admin_token))).status_code == 404

    # audit trail
    r = await client.get("/api/v1/admin/audit-logs?resource_id=" + uid, headers=auth(admin_token))
    actions = [a["action"] for a in r.json()["data"]]
    for expected in ("user_viewed", "user_package_changed", "user_role_changed", "user_suspended", "user_activated", "user_deleted"):
        assert expected in actions, actions
    r = await client.get("/api/v1/admin/audit-logs?action=admin_login", headers=auth(admin_token))
    assert r.json()["data"][0]["actor_type"] == "admin" and r.json()["data"][0]["ip_address"]


async def test_package_management(client, admin_token):
    body = {"name": "Startup", "description": "d", "price": 49, "currency": "USD", "billing_cycle": "monthly",
            "upload_limit": 5000, "search_limit": 5000, "storage_limit": 10 * 1024 ** 3, "max_users": 5, "api_rate_limit": 120}
    r = await client.post("/api/v1/admin/packages", headers=auth(admin_token), json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["data"]["id"]
    assert (await client.post("/api/v1/admin/packages", headers=auth(admin_token), json=body)).status_code == 409

    r = await client.get(f"/api/v1/admin/packages/{pid}", headers=auth(admin_token))
    assert r.json()["data"]["upload_limit"] == 5000
    r = await client.patch(f"/api/v1/admin/packages/{pid}", headers=auth(admin_token), json={"upload_limit": 6000, "is_active": False})
    assert r.json()["data"]["upload_limit"] == 6000 and r.json()["data"]["is_active"] is False
    # public list hides inactive
    assert "Startup" not in [p["name"] for p in (await client.get("/api/v1/packages")).json()["data"]]

    # assign and then deletion is blocked
    uid = str(uuid.uuid4())
    await client.get("/api/v1/me", headers=auth(make_jwt(uid)))
    await client.patch(f"/api/v1/admin/users/{uid}/package", headers=auth(admin_token), json={"package_id": pid})
    r = await client.delete(f"/api/v1/admin/packages/{pid}", headers=auth(admin_token))
    assert r.status_code == 409
    # inactive package blocks the user
    r = await client.get("/api/v1/me", headers=auth(make_jwt(uid)))
    assert r.status_code == 402 and r.json()["error"]["code"] == "SUBSCRIPTION_INACTIVE"

    # move user away, delete package
    free = next(p for p in (await client.get("/api/v1/packages")).json()["data"] if p["name"] == "Free")
    await client.patch(f"/api/v1/admin/users/{uid}/package", headers=auth(admin_token), json={"package_id": free["id"]})
    r = await client.delete(f"/api/v1/admin/packages/{pid}", headers=auth(admin_token))
    assert r.status_code == 200
    # default package cannot be deleted
    assert (await client.delete(f"/api/v1/admin/packages/{free['id']}", headers=auth(admin_token))).status_code == 409
    r = await client.get("/api/v1/admin/audit-logs?resource_type=package", headers=auth(admin_token))
    assert {"package_created", "package_updated", "package_deleted"} <= {a["action"] for a in r.json()["data"]}


async def test_expired_subscription(client, admin_token):
    uid = str(uuid.uuid4())
    await client.get("/api/v1/me", headers=auth(make_jwt(uid)))
    free = next(p for p in (await client.get("/api/v1/packages")).json()["data"] if p["name"] == "Free")
    r = await client.patch(f"/api/v1/admin/users/{uid}/package", headers=auth(admin_token),
                           json={"package_id": free["id"], "status": "expired"})
    assert r.status_code == 200
    r = await client.get("/api/v1/me", headers=auth(make_jwt(uid)))
    assert r.status_code == 402 and r.json()["error"]["code"] == "PACKAGE_EXPIRED"


async def test_dashboard(client, admin_token, user_token):
    await client.post("/api/v1/faces", headers=auth(user_token), files=img_file(make_image()), data={"name": "P"})
    r = await client.get("/api/v1/admin/dashboard", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["users"]["total"] == 1 and d["faces"]["faces"] == 1 and d["today"]["uploads"] == 1
    assert d["package_distribution"] == {"Free": 1} and d["storage"]["used_bytes"] > 0


async def test_health(client):
    r = await client.get("/health/live")
    assert r.status_code == 200
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"]["face_engine"]["ok"] and r.json()["checks"]["database"]["ok"]
    assert r.json()["checks"]["supabase"]["ok"] is False  # not configured in tests


async def test_openapi(client):
    r = await client.get("/api/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/api/v1/faces/search" in spec["paths"]
    op = spec["paths"]["/api/v1/faces/search"]["post"]
    assert "429" in op["responses"] and "401" in op["responses"] and op["description"]
    assert {"SupabaseJWT", "ApiKey", "AdminToken"} <= set(spec["components"]["securitySchemes"])
    assert (await client.get("/api/docs")).status_code == 200
