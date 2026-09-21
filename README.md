# Face Recognition API Platform

Multi-tenant Face Recognition SaaS API: register persons + faces, search a face and get the matching
person's metadata back. Built on FastAPI, PostgreSQL + pgvector, Redis, Supabase Auth (Google OAuth),
InsightFace (`buffalo_l`, 512-d ArcFace embeddings) and MinIO/S3 or Supabase Storage.

* Swagger UI: `http://localhost:8000/api/docs` · OpenAPI: `/api/openapi.json` · Health: `/health`
* Every response is `{"success": true, "data": …, "meta": …}` or `{"success": false, "error": {"code", "message", "details"}}`.

## Quick start

```bash
cp .env.example .env            # fill in Supabase + admin bootstrap values
docker compose up -d            # api + postgres(pgvector) + redis + minio
curl http://localhost:8000/health
```

The API container runs `alembic upgrade head` on start (set `SKIP_MIGRATIONS=1` to disable), loads the face
model once per worker, and creates the first `SUPER_ADMIN` from `ADMIN_BOOTSTRAP_USERNAME/PASSWORD` if no
admin exists.

Local development (Python 3.11+):

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # or bin/pip on Linux/macOS
docker compose up -d postgres redis minio
alembic upgrade head
uvicorn app.main:app --reload
```

## Authentication

| Caller | How | Notes |
|---|---|---|
| End user | Supabase Auth (Google OAuth on the client) → `Authorization: Bearer <access_token>` | First login provisions tenant + profile + default (`Free`) subscription |
| Integration | `X-API-Key: fr_live_…` (or `Authorization: Bearer fr_live_…`) | Scoped: `faces:upload`, `faces:search`, `faces:read`, `persons:read`; expiring; revocable; rotatable |
| Admin | `POST /api/v1/admin/auth/login` (username/password, Argon2id) → `Authorization: Bearer <admin token>` | Separate from user auth; server-side sessions; RBAC |

JWT verification supports HS256 (`SUPABASE_JWT_SECRET`) and asymmetric keys via the project's JWKS
(`SUPABASE_URL/auth/v1/.well-known/jwks.json`).

Every protected request goes through: **auth → user status → tenant → scope/permission → subscription
status → rate limit (Redis, per package) → quota (atomic, per period) → execute → usage + logs**.

## API surface (v1)

```
POST /api/v1/auth/refresh              POST /api/v1/auth/logout
GET  /api/v1/me  /profile  /status  /package  /quota  /usage  /usage/history     PATCH /api/v1/me/profile
POST/GET /api/v1/me/api-keys           DELETE /api/v1/me/api-keys/{id}   POST /api/v1/me/api-keys/{id}/rotate
POST /api/v1/faces                     POST /api/v1/faces/search
GET  /api/v1/faces  /faces/{id}        DELETE /api/v1/faces/{id}
GET/PATCH/DELETE /api/v1/persons[/{id}]
GET  /api/v1/packages
POST /api/v1/admin/auth/login|logout   GET /api/v1/admin/auth/me   GET/POST /api/v1/admin/admins   PATCH /admin/admins/{id}/roles
GET  /api/v1/admin/dashboard
GET  /api/v1/admin/users[/{id}]        PATCH /admin/users/{id}[/role|/package|/status]   DELETE /admin/users/{id}
GET  /api/v1/admin/users/{id}/usage|faces|uploads|searches     DELETE /admin/users/{id}/faces/{face_id}
GET/POST /api/v1/admin/packages        GET/PATCH/DELETE /admin/packages/{id}
GET  /api/v1/admin/audit-logs
GET  /health  /health/live  /health/ready
```

### Register a face

```bash
curl -X POST http://localhost:8000/api/v1/faces \
  -H "X-API-Key: fr_live_..." -H "Idempotency-Key: 7d1f…" \
  -F image=@john.jpg -F external_user_id=EMP-001 -F name="John Doe" \
  -F company="Example" -F metadata='{"position":"Engineer"}'
```

Rejections: `INVALID_IMAGE` (400), `NO_FACE_DETECTED` / `MULTIPLE_FACES_DETECTED` (422),
`DUPLICATE_IMAGE` (409), `UPLOAD_LIMIT_EXCEEDED` / `STORAGE_LIMIT_EXCEEDED` (429). Quota is only consumed on success.

### Search

```bash
curl -X POST http://localhost:8000/api/v1/faces/search -H "X-API-Key: fr_live_..." -F image=@query.jpg
```

```json
{"success": true, "data": {"match": true, "confidence": 0.9968, "similarity": 0.9277,
  "person": {"id": "…", "external_user_id": "EMP-001", "name": "John Doe", "metadata": {"position": "Engineer"}},
  "candidates": [...], "threshold": 0.45, "processing_time_ms": 383}}
```

`similarity` is cosine similarity from pgvector (HNSW index); `confidence` is a logistic calibration around
`FACE_SIMILARITY_THRESHOLD` (0.5 at the threshold). Search never crosses tenants.

## Packages, subscriptions, quota

Packages live in the `packages` table (seeded: Free / Basic / Pro / Enterprise) and are fully editable by
admins; `-1` means unlimited. Each tenant has a subscription (`trial|active|past_due|cancelled|expired|suspended`).
Usage is tracked per calendar month in `usage_periods` (never deleted; a new row is created each month,
storage carries over) plus immutable `usage_logs`, `upload_logs`, `search_logs`. Quota checks are single
atomic `UPDATE … WHERE count + n <= limit` statements, so concurrent requests cannot overspend.
Rate limiting is a per-tenant Redis window sized by the package (`X-RateLimit-*` headers, `429`).

## Storage layout

`tenant/{tenant_id}/persons/{person_id}/original/{image_id}.{ext}` and `…/thumbnails/{image_id}.jpg`
in a private bucket. Images are only ever exposed through short-lived signed URLs
(`SIGNED_URL_TTL_SECONDS`). For MinIO behind a proxy set `MINIO_PUBLIC_URL` so signatures match the public host.

## Project layout

```
app/
  main.py               app factory, lifespan (model load, admin bootstrap), error handlers
  api/deps.py           auth / scope / permission / rate-limit dependencies
  api/v1/               auth, me, api_keys, faces (+persons, search), packages, admin, health
  core/                 config, exceptions + error codes, response envelope, logging, security, permissions, db, redis
  engines/              FaceEngine protocol, InsightFace engine, deterministic fake engine (tests)
  services/             embedding, storage, supabase, auth, package, quota, usage, api_key, rate_limit, audit, face, admin
  models/, schemas/     SQLAlchemy models, Pydantic schemas
  middleware/           request id, access log, security headers
migrations/             Alembic (0001_initial_schema seeds roles, permissions, packages)
tests/                  pytest suite (real Postgres/pgvector, fakeredis, fake engine)
facefindr/, main.py     legacy Facemenow event app; mounted at /legacy when LEGACY_API_ENABLED=true
```

## Testing & checks

```bash
docker compose up -d postgres redis
pytest -q                         # 48 tests; uses database facefinder_test (created automatically)
ruff check app tests && mypy -p app
alembic check                     # model ↔ migration drift
```

## Deployment notes

* Production requires `ADMIN_JWT_SECRET`, Supabase settings, `CORS_ORIGINS` (not `*`), and a Postgres with the
  `vector` extension available (`CREATE EXTENSION vector` needs superuser once; the migration does it).
* Run one uvicorn worker per container and scale containers, or set `--workers N` (model loads once per worker).
* GPU: install `onnxruntime-gpu` and set `FACE_CTX_ID=0`.
