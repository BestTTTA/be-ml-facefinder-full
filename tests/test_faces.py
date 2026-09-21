import asyncio
import json

from tests.conftest import auth, img_file, make_image, make_jwt, set_package_limits

RED, GREEN, BLUE = (200, 30, 30), (30, 200, 30), (30, 30, 200)


async def upload(client, token, color=RED, name="John Doe", ext_id="EMP-001", width=100, headers=None, **form):
    data = {"name": name, "external_user_id": ext_id, "company": "Example", "department": "Engineering",
            "metadata": json.dumps({"position": "Engineer"}), **form}
    return await client.post("/api/v1/faces", headers={**auth(token), **(headers or {})},
                             files=img_file(make_image(color, width=width)), data=data)


async def search(client, token, color=RED, width=100, **form):
    return await client.post("/api/v1/faces/search", headers=auth(token),
                             files=img_file(make_image(color, width=width)), data=form)


# --- upload ---------------------------------------------------------------------

async def test_upload_valid_image(client, user_token, storage_backend):
    r = await upload(client, user_token)
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    assert d["person"]["external_user_id"] == "EMP-001"
    assert d["person"]["metadata"] == {"position": "Engineer"}
    assert d["face"]["model"] == "fake"
    assert d["image"]["mime_type"] == "image/jpeg"
    assert len(storage_backend.objects) == 2  # original + thumbnail
    key = next(k for k in storage_backend.objects if "/original/" in k)
    assert key.startswith("tenant/") and "/persons/" in key

    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 1
    assert usage["storage"]["used"] == d["image"]["size_bytes"] > 0

    # face detail with signed urls
    r = await client.get(f"/api/v1/faces/{d['face']['id']}", headers=auth(user_token))
    assert r.status_code == 200 and r.json()["data"]["image_url"].startswith("memory://")


async def test_upload_invalid_image(client, user_token):
    r = await client.post("/api/v1/faces", headers=auth(user_token),
                          files={"image": ("face.jpg", b"not-an-image", "image/jpeg")}, data={"name": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_IMAGE"
    r = await client.post("/api/v1/faces", headers=auth(user_token),
                          files={"image": ("face.gif", make_image(fmt="GIF"), "image/gif")}, data={"name": "x"})
    assert r.status_code == 400
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 0  # failures do not consume quota


async def test_upload_no_face(client, user_token):
    r = await upload(client, user_token, width=20)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NO_FACE_DETECTED"


async def test_upload_multiple_faces(client, user_token):
    r = await upload(client, user_token, width=300)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MULTIPLE_FACES_DETECTED"
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 0 and usage["storage"]["used"] == 0


async def test_upload_duplicate_image(client, user_token):
    assert (await upload(client, user_token)).status_code == 201
    r = await upload(client, user_token)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_IMAGE"


async def test_upload_idempotency_key(client, user_token):
    hdr = {"Idempotency-Key": "abc-123"}
    r1 = await upload(client, user_token, headers=hdr)
    r2 = await upload(client, user_token, headers=hdr)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data"]["face"]["id"] == r2.json()["data"]["face"]["id"]
    assert r2.json()["meta"]["idempotent_replay"] is True
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 1


async def test_upload_quota_exceeded(client, user_token, admin_token):
    await set_package_limits(client, admin_token, "Free", upload_limit=2)
    assert (await upload(client, user_token, color=RED, ext_id="A")).status_code == 201
    assert (await upload(client, user_token, color=GREEN, ext_id="B")).status_code == 201
    r = await upload(client, user_token, color=BLUE, ext_id="C")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "UPLOAD_LIMIT_EXCEEDED"
    await set_package_limits(client, admin_token, "Free", upload_limit=100)


async def test_storage_limit(client, user_token, admin_token):
    await set_package_limits(client, admin_token, "Free", storage_limit=10)
    r = await upload(client, user_token)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "STORAGE_LIMIT_EXCEEDED"
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 0  # upload reservation released
    await set_package_limits(client, admin_token, "Free", storage_limit=500 * 1024 * 1024)


async def test_concurrent_uploads_cannot_bypass_quota(client, user_token, admin_token):
    await set_package_limits(client, admin_token, "Free", upload_limit=5, api_rate_limit=1000)
    colors = [(i * 12 % 256, (i * 37) % 256, (i * 91) % 256) for i in range(20)]
    results = await asyncio.gather(*[upload(client, user_token, color=c, ext_id=f"P{i}") for i, c in enumerate(colors)])
    codes = [r.status_code for r in results]
    assert codes.count(201) == 5, codes
    assert codes.count(429) == 15, codes
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["uploads"]["used"] == 5
    await set_package_limits(client, admin_token, "Free", upload_limit=100, api_rate_limit=10)


async def test_delete_face_frees_storage(client, user_token, storage_backend):
    d = (await upload(client, user_token)).json()["data"]
    r = await client.delete(f"/api/v1/faces/{d['face']['id']}", headers=auth(user_token))
    assert r.status_code == 200
    assert storage_backend.objects == {}
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["storage"]["used"] == 0
    assert (await client.get(f"/api/v1/faces/{d['face']['id']}", headers=auth(user_token))).status_code == 404


# --- search ---------------------------------------------------------------------

async def test_search_match(client, user_token):
    assert (await upload(client, user_token, color=RED, ext_id="EMP-001", name="John")).status_code == 201
    assert (await upload(client, user_token, color=GREEN, ext_id="EMP-002", name="Jane")).status_code == 201
    r = await search(client, user_token, color=RED)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["match"] is True
    assert d["person"]["external_user_id"] == "EMP-001"
    assert d["person"]["name"] == "John"
    assert d["similarity"] > 0.99 and d["confidence"] > 0.9
    assert d["candidates"][0]["person"]["external_user_id"] == "EMP-001"
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["searches"]["used"] == 1


async def test_search_no_match(client, user_token):
    assert (await upload(client, user_token, color=RED)).status_code == 201
    r = await search(client, user_token, color=BLUE)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["match"] is False and d["person"] is None and d["candidates"] == []


async def test_search_threshold_override(client, user_token):
    assert (await upload(client, user_token, color=RED)).status_code == 201
    r = await search(client, user_token, color=BLUE, threshold="0.0")
    assert r.json()["data"]["match"] is True  # everything matches at threshold 0


async def test_search_no_face_does_not_consume_quota(client, user_token):
    r = await search(client, user_token, width=20)
    assert r.status_code == 422
    usage = (await client.get("/api/v1/me/usage", headers=auth(user_token))).json()["data"]
    assert usage["searches"]["used"] == 0


async def test_search_quota_exceeded(client, user_token, admin_token):
    await set_package_limits(client, admin_token, "Free", search_limit=1)
    assert (await search(client, user_token)).status_code == 200
    r = await search(client, user_token)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "SEARCH_LIMIT_EXCEEDED"
    assert r.json()["error"]["message"] == "Monthly face search quota exceeded"
    await set_package_limits(client, admin_token, "Free", search_limit=100)


async def test_rate_limit(client, user_token, admin_token):
    await set_package_limits(client, admin_token, "Free", api_rate_limit=3)
    codes = [(await client.get("/api/v1/me", headers=auth(user_token))).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = await client.get("/api/v1/me", headers=auth(user_token))
    assert r.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert r.headers["X-RateLimit-Limit"] == "3" and "Retry-After" in r.headers
    await set_package_limits(client, admin_token, "Free", api_rate_limit=10)


async def test_tenant_isolation(client):
    tok_a, tok_b = make_jwt(email="a@x.com"), make_jwt(email="b@x.com")
    d = (await upload(client, tok_a, color=RED, ext_id="EMP-001")).json()["data"]
    # B cannot find A's face
    r = await search(client, tok_b, color=RED)
    assert r.json()["data"]["match"] is False
    # B cannot read/delete A's resources by id
    assert (await client.get(f"/api/v1/faces/{d['face']['id']}", headers=auth(tok_b))).status_code == 404
    assert (await client.get(f"/api/v1/persons/{d['person']['id']}", headers=auth(tok_b))).status_code == 404
    assert (await client.delete(f"/api/v1/faces/{d['face']['id']}", headers=auth(tok_b))).status_code == 404
    assert (await client.post("/api/v1/faces", headers=auth(tok_b), files=img_file(make_image(GREEN)),
                              data={"person_id": d["person"]["id"]})).status_code == 404
    # same external id in another tenant is a distinct person
    d2 = (await upload(client, tok_b, color=GREEN, ext_id="EMP-001")).json()["data"]
    assert d2["person"]["id"] != d["person"]["id"]
    assert len((await client.get("/api/v1/persons", headers=auth(tok_b))).json()["data"]) == 1


async def test_person_upsert_and_management(client, user_token):
    d1 = (await upload(client, user_token, color=RED, ext_id="EMP-001")).json()["data"]
    d2 = (await upload(client, user_token, color=(201, 31, 31), ext_id="EMP-001", name="John Updated")).json()["data"]
    assert d1["person"]["id"] == d2["person"]["id"]
    r = await client.get(f"/api/v1/persons/{d1['person']['id']}", headers=auth(user_token))
    assert r.json()["data"]["name"] == "John Updated" and r.json()["data"]["face_count"] == 2
    r = await client.patch(f"/api/v1/persons/{d1['person']['id']}", headers=auth(user_token),
                           json={"department": "Sales", "metadata": {"k": "v"}})
    assert r.json()["data"]["department"] == "Sales" and r.json()["data"]["metadata"] == {"k": "v"}
    r = await client.get("/api/v1/persons?q=EMP", headers=auth(user_token))
    assert r.json()["meta"]["total"] == 1
    r = await client.delete(f"/api/v1/persons/{d1['person']['id']}", headers=auth(user_token))
    assert r.json()["data"]["faces_deleted"] == 2
    assert (await client.get("/api/v1/faces", headers=auth(user_token))).json()["meta"]["total"] == 0
    assert (await search(client, user_token, color=RED)).json()["data"]["match"] is False
