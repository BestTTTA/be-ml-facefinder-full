"""Face registration, search, and face/person management. All tenant-scoped."""
import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import embedding_dep, require_scope, storage_dep
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.exceptions import ValidationFailed
from app.core.permissions import SCOPE_FACES_READ, SCOPE_FACES_SEARCH, SCOPE_FACES_UPLOAD, SCOPE_PERSONS_READ
from app.core.redis import get_redis
from app.core.responses import ok
from app.schemas.common import AUTH_ERRORS, SuccessResponse, error_responses
from app.schemas.models import FaceOut, PersonIn, PersonOut, SearchOut, UploadOut
from app.services import audit_service, face_service
from app.services.auth_service import Principal
from app.services.embedding_service import EmbeddingService
from app.services.storage_service import StorageService

router = APIRouter(tags=["faces"])
_MAX_UPLOAD_READ = 32 * 1024 * 1024  # hard cap; FACE_MAX_FILE_SIZE_BYTES applies after


async def _read_upload(file: UploadFile, s: Settings) -> bytes:
    data = await file.read(_MAX_UPLOAD_READ + 1)
    if len(data) > _MAX_UPLOAD_READ:
        raise ValidationFailed("File too large", code="INVALID_IMAGE", status_code=413)
    return data


def _idem_key(tenant_id: uuid.UUID, key: str) -> str:
    return f"idem:upload:{tenant_id}:{key[:128]}"


@router.post("/faces", response_model=SuccessResponse[UploadOut], status_code=201,
             responses={**AUTH_ERRORS, **error_responses(400, 409, 413, 422, 500, 503)},
             summary="Register a face",
             description="""Upload one image containing exactly one face and attach it to a person.

**Auth:** Supabase JWT or API key with scope `faces:upload`.

Pipeline: auth → status → package → upload quota → file validation → face detection → embedding →
storage → DB → usage. Quota is only consumed when the face is registered successfully.

Person resolution: `person_id` (existing) → `external_user_id` (upsert within your tenant) → new person.
Send `metadata` as a JSON object string. Use the `Idempotency-Key` header to make retries safe.""")
async def upload_face(request: Request,
                      image: UploadFile = File(..., description="JPEG/PNG/WebP, max FACE_MAX_FILE_SIZE_BYTES"),
                      person_id: Optional[uuid.UUID] = Form(None),
                      external_user_id: Optional[str] = Form(None, max_length=200),
                      name: Optional[str] = Form(None, max_length=200),
                      email: Optional[str] = Form(None, max_length=320),
                      phone: Optional[str] = Form(None, max_length=50),
                      company: Optional[str] = Form(None, max_length=200),
                      department: Optional[str] = Form(None, max_length=200),
                      metadata: Optional[str] = Form(None, description='JSON object, e.g. {"employee_code":"EMP-001"}'),
                      idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                      principal: Principal = Depends(require_scope(SCOPE_FACES_UPLOAD)),
                      db: AsyncSession = Depends(get_db),
                      embedding: EmbeddingService = Depends(embedding_dep),
                      storage: StorageService = Depends(storage_dep)):
    s = get_settings()
    redis = get_redis()
    if idempotency_key:
        cached = await redis.get(_idem_key(principal.tenant_id, idempotency_key))
        if cached:
            return ok(json.loads(cached), meta={"idempotent_replay": True})

    meta: Dict[str, Any] = {}
    if metadata:
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError as e:
            raise ValidationFailed("metadata must be a JSON object") from e
        if not isinstance(meta, dict):
            raise ValidationFailed("metadata must be a JSON object")
    attrs = PersonIn(external_user_id=external_user_id, name=name, email=email, phone=phone, company=company,
                     department=department, metadata=meta).model_dump()

    data = await _read_upload(image, s)
    res = await face_service.upload_face(db, principal, data=data, content_type=image.content_type,
                                         filename=image.filename, person_id=person_id, person_attrs=attrs,
                                         embedding=embedding, storage=storage, idempotency_key=idempotency_key)
    payload = {"face": await face_service.face_to_dict(res.face),
               "person": face_service.person_to_dict(res.person),
               "image": {"id": str(res.image.id), "mime_type": res.image.mime_type, "size_bytes": res.image.size_bytes,
                         "width": res.image.width, "height": res.image.height},
               "processing_time_ms": res.processing_ms}
    if idempotency_key:
        await redis.set(_idem_key(principal.tenant_id, idempotency_key), json.dumps(payload, default=str),
                        ex=s.IDEMPOTENCY_TTL_SECONDS)
    return ok(payload)


@router.post("/faces/search", response_model=SuccessResponse[SearchOut],
             responses={**AUTH_ERRORS, **error_responses(400, 413, 422, 500)},
             summary="Search a face",
             description="""Find the person matching the face in the uploaded image, **within your tenant only**.

**Auth:** Supabase JWT or API key with scope `faces:search`.

Uses pgvector cosine similarity (HNSW index). `threshold` defaults to FACE_SIMILARITY_THRESHOLD.
Returns the best person plus up to `top_k` candidates. Search quota is only consumed on a completed search.""")
async def search_face(request: Request, image: UploadFile = File(...),
                      threshold: Optional[float] = Form(None, ge=0.0, le=1.0),
                      top_k: Optional[int] = Form(None, ge=1, le=50),
                      principal: Principal = Depends(require_scope(SCOPE_FACES_SEARCH)),
                      db: AsyncSession = Depends(get_db),
                      embedding: EmbeddingService = Depends(embedding_dep)):
    data = await _read_upload(image, get_settings())
    result = await face_service.search_face(db, principal, data=data, content_type=image.content_type,
                                            filename=image.filename, embedding=embedding, threshold=threshold, top_k=top_k)
    return ok(result)


@router.get("/faces", response_model=SuccessResponse[list[FaceOut]], responses=AUTH_ERRORS, summary="List faces")
async def list_faces(person_id: Optional[uuid.UUID] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     with_urls: bool = Query(False, description="Include short-lived signed image URLs"),
                     principal: Principal = Depends(require_scope(SCOPE_FACES_READ)),
                     db: AsyncSession = Depends(get_db), storage: StorageService = Depends(storage_dep)):
    rows, total = await face_service.list_faces(db, principal.tenant_id, person_id=person_id, limit=limit, offset=offset)
    return ok([await face_service.face_to_dict(f, storage, with_urls) for f in rows],
              meta={"total": total, "limit": limit, "offset": offset})


@router.get("/faces/{face_id}", response_model=SuccessResponse[FaceOut], responses={**AUTH_ERRORS, **error_responses(404)},
            summary="Get face (with signed URLs)")
async def get_face(face_id: uuid.UUID, principal: Principal = Depends(require_scope(SCOPE_FACES_READ)),
                   db: AsyncSession = Depends(get_db), storage: StorageService = Depends(storage_dep)):
    f = await face_service.get_face(db, principal.tenant_id, face_id)
    return ok(await face_service.face_to_dict(f, storage, with_urls=True))


@router.delete("/faces/{face_id}", response_model=SuccessResponse[dict], responses={**AUTH_ERRORS, **error_responses(404)},
               summary="Delete face", description="Deletes the face embedding and its image; frees storage quota.")
async def delete_face(face_id: uuid.UUID, request: Request, principal: Principal = Depends(require_scope(SCOPE_FACES_UPLOAD)),
                      db: AsyncSession = Depends(get_db), storage: StorageService = Depends(storage_dep)):
    f = await face_service.delete_face(db, principal.tenant_id, face_id, storage)
    await audit_service.record(db, action="face_deleted", actor_type="user", actor_id=principal.user_id,
                               resource_type="face", resource_id=f.id, request=request)
    return ok({"deleted": True, "face_id": str(f.id)})


# --- persons ---

@router.get("/persons", response_model=SuccessResponse[list[PersonOut]], responses=AUTH_ERRORS, summary="List persons")
async def list_persons(q: Optional[str] = Query(None, max_length=200), limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0), principal: Principal = Depends(require_scope(SCOPE_PERSONS_READ)),
                       db: AsyncSession = Depends(get_db)):
    rows, total = await face_service.list_persons(db, principal.tenant_id, q=q, limit=limit, offset=offset)
    return ok([face_service.person_to_dict(p, include_faces=True) for p in rows],
              meta={"total": total, "limit": limit, "offset": offset})


@router.get("/persons/{person_id}", response_model=SuccessResponse[PersonOut], responses={**AUTH_ERRORS, **error_responses(404)},
            summary="Get person")
async def get_person(person_id: uuid.UUID, principal: Principal = Depends(require_scope(SCOPE_PERSONS_READ)),
                     db: AsyncSession = Depends(get_db)):
    p = await face_service.get_person(db, principal.tenant_id, person_id)
    return ok(face_service.person_to_dict(p, include_faces=True))


@router.patch("/persons/{person_id}", response_model=SuccessResponse[PersonOut], responses={**AUTH_ERRORS, **error_responses(404)},
              summary="Update person metadata")
async def update_person(person_id: uuid.UUID, body: PersonIn, principal: Principal = Depends(require_scope(SCOPE_FACES_UPLOAD)),
                        db: AsyncSession = Depends(get_db)):
    p = await face_service.update_person(db, principal.tenant_id, person_id, body.model_dump(exclude_unset=True))
    return ok(face_service.person_to_dict(p, include_faces=True))


@router.delete("/persons/{person_id}", response_model=SuccessResponse[dict], responses={**AUTH_ERRORS, **error_responses(404)},
               summary="Delete person and all their faces")
async def delete_person(person_id: uuid.UUID, request: Request, principal: Principal = Depends(require_scope(SCOPE_FACES_UPLOAD)),
                        db: AsyncSession = Depends(get_db), storage: StorageService = Depends(storage_dep)):
    n = await face_service.delete_person(db, principal.tenant_id, person_id, storage)
    await audit_service.record(db, action="person_deleted", actor_type="user", actor_id=principal.user_id,
                               resource_type="person", resource_id=person_id, metadata={"faces_deleted": n}, request=request)
    return ok({"deleted": True, "person_id": str(person_id), "faces_deleted": n})
