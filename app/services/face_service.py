"""Persons, face upload pipeline and pgvector search. Every query is scoped by tenant_id."""
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, DuplicateImage, FaceNotFound, PersonNotFound, SearchFailed
from app.core.logging import get_logger, request_id_ctx
from app.models import Face, Image, Person, SearchLog, UploadLog
from app.services import usage_service
from app.services.auth_service import Principal
from app.services.embedding_service import EmbeddingService
from app.services.quota_service import QuotaReservation, release_storage
from app.services.storage_service import StorageService

log = get_logger(__name__)

PERSON_FIELDS = ("external_user_id", "name", "email", "phone", "company", "department")


# --- persons -----------------------------------------------------------------

def person_to_dict(p: Person, include_faces: bool = False) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": str(p.id), "external_user_id": p.external_user_id, "name": p.name, "email": p.email,
         "phone": p.phone, "company": p.company, "department": p.department, "metadata": p.metadata_ or {},
         "created_at": p.created_at, "updated_at": p.updated_at}
    if include_faces:
        d["faces"] = [{"id": str(f.id), "image_id": str(f.image_id), "bbox": f.bbox, "det_score": f.det_score,
                       "created_at": f.created_at} for f in p.faces]
        d["face_count"] = len(p.faces)
    return d


async def get_person(db: AsyncSession, tenant_id: uuid.UUID, person_id: uuid.UUID) -> Person:
    p = await db.get(Person, person_id)
    if p is None or p.tenant_id != tenant_id or p.deleted_at is not None:
        raise PersonNotFound()
    return p


async def list_persons(db: AsyncSession, tenant_id: uuid.UUID, *, q: Optional[str] = None,
                       limit: int = 50, offset: int = 0) -> tuple[List[Person], int]:
    base = select(Person).where(Person.tenant_id == tenant_id, Person.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        base = base.where((Person.name.ilike(like)) | (Person.external_user_id.ilike(like)) | (Person.email.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list((await db.execute(base.order_by(Person.created_at.desc()).limit(limit).offset(offset))).scalars())
    return rows, total


async def upsert_person(db: AsyncSession, tenant_id: uuid.UUID, created_by: uuid.UUID,
                        person_id: Optional[uuid.UUID], attrs: Dict[str, Any]) -> Person:
    """Attach to an explicit person, else match by external_user_id within the tenant, else create."""
    person: Optional[Person] = None
    if person_id:
        person = await get_person(db, tenant_id, person_id)
    elif attrs.get("external_user_id"):
        person = (await db.execute(select(Person).where(Person.tenant_id == tenant_id,
                                                        Person.external_user_id == attrs["external_user_id"],
                                                        Person.deleted_at.is_(None)))).scalar_one_or_none()
    if person is None:
        person = Person(tenant_id=tenant_id, created_by=created_by,
                        **{k: attrs.get(k) for k in PERSON_FIELDS}, metadata_=attrs.get("metadata") or {})
        db.add(person)
    else:
        for k in PERSON_FIELDS:
            if attrs.get(k) is not None:
                setattr(person, k, attrs[k])
        if attrs.get("metadata"):
            person.metadata_ = {**(person.metadata_ or {}), **attrs["metadata"]}
    await db.flush()
    return person


async def update_person(db: AsyncSession, tenant_id: uuid.UUID, person_id: uuid.UUID, attrs: Dict[str, Any]) -> Person:
    person = await get_person(db, tenant_id, person_id)
    for k in PERSON_FIELDS:
        if k in attrs:
            setattr(person, k, attrs[k])
    if "metadata" in attrs and attrs["metadata"] is not None:
        person.metadata_ = attrs["metadata"]
    await db.flush()
    return person


async def delete_person(db: AsyncSession, tenant_id: uuid.UUID, person_id: uuid.UUID,
                        storage: StorageService) -> int:
    person = await get_person(db, tenant_id, person_id)
    faces = list((await db.execute(select(Face).where(Face.person_id == person.id, Face.deleted_at.is_(None)))).scalars())
    for f in faces:
        await _delete_face_row(db, tenant_id, f, storage)
    person.deleted_at = func.now()
    await db.flush()
    return len(faces)


# --- upload ------------------------------------------------------------------

@dataclass
class UploadResult:
    face: Face
    image: Image
    person: Person
    processing_ms: int


async def upload_face(db: AsyncSession, principal: Principal, *, data: bytes, content_type: Optional[str],
                      filename: Optional[str], person_id: Optional[uuid.UUID], person_attrs: Dict[str, Any],
                      embedding: EmbeddingService, storage: StorageService,
                      idempotency_key: Optional[str] = None, settings: Optional[Settings] = None) -> UploadResult:
    """Package/upload-quota → validate → detect → embed → store → persist → usage.
    Quota reservations are released if any later step fails."""
    t0 = time.perf_counter()
    tenant_id = principal.tenant_id
    quota = QuotaReservation(db, tenant_id)
    status, err_code, image_id, person_ref = "failed", None, None, person_id
    try:
        await quota.reserve_upload(principal.package)
        img = embedding.validate_image(data, content_type, filename)
        thumb = storage.make_thumbnail(img.data)
        await quota.reserve_storage(principal.package, len(img.data) + len(thumb))

        dup = (await db.execute(select(Image).where(Image.tenant_id == tenant_id, Image.sha256 == img.sha256,
                                                    Image.deleted_at.is_(None)))).scalar_one_or_none()
        if dup:
            raise DuplicateImage(details={"image_id": str(dup.id), "person_id": str(dup.person_id) if dup.person_id else None})

        face_det = await embedding.single_face(img)
        person = await upsert_person(db, tenant_id, principal.user_id, person_id, person_attrs)
        person_ref = person.id

        image = Image(tenant_id=tenant_id, person_id=person.id, uploaded_by=principal.user_id, storage_key="",
                      mime_type=img.mime_type, size_bytes=len(img.data) + len(thumb), width=img.width,
                      height=img.height, sha256=img.sha256)
        db.add(image)
        await db.flush()
        image_id = image.id
        okey, tkey = await storage.store_image(tenant_id, person.id, image.id, img.data, img.mime_type, thumbnail=thumb)
        image.storage_key, image.thumbnail_key = okey, tkey

        face = Face(tenant_id=tenant_id, person_id=person.id, image_id=image.id,
                    embedding=face_det.embedding.tolist(), bbox=face_det.bbox, det_score=face_det.det_score,
                    model_name=embedding.engine.name)
        db.add(face)
        await db.flush()

        await usage_service.log_usage(db, tenant_id, "upload", user_id=principal.user_id,
                                      api_key_id=principal.api_key_id, resource_id=face.id,
                                      idempotency_key=idempotency_key, request_id=request_id_ctx.get())
        quota.commit()
        status = "success"
        ms = int((time.perf_counter() - t0) * 1000)
        db.add(UploadLog(tenant_id=tenant_id, user_id=principal.user_id, api_key_id=principal.api_key_id,
                         image_id=image.id, person_id=person.id, file_size=len(data), processing_time_ms=ms,
                         status=status, request_id=request_id_ctx.get()))
        await db.flush()
        return UploadResult(face=face, image=image, person=person, processing_ms=ms)
    except AppError as e:
        err_code = e.code
        status = "rejected" if e.status_code < 500 else "failed"
        raise
    except Exception:
        err_code = "INTERNAL_ERROR"
        raise
    finally:
        if status != "success":
            await quota.release_all()
            # the request transaction will roll back; write the upload log in its own transaction
            await _write_upload_log(tenant_id, principal, image_id, person_ref, len(data),
                                    int((time.perf_counter() - t0) * 1000), status, err_code)


async def _write_upload_log(tenant_id, principal, image_id, person_id, size, ms, status, err_code) -> None:
    from app.core.database import get_sessionmaker
    try:
        async with get_sessionmaker()() as s:
            s.add(UploadLog(tenant_id=tenant_id, user_id=principal.user_id, api_key_id=principal.api_key_id,
                            image_id=image_id, person_id=person_id, file_size=size, processing_time_ms=ms,
                            status=status, error_code=err_code, request_id=request_id_ctx.get()))
            await s.commit()
    except Exception:
        log.warning("upload_log_write_failed")


# --- search ------------------------------------------------------------------

def confidence_from_similarity(sim: float, threshold: float, steepness: float = 12.0) -> float:
    """Logistic calibration: 0.5 exactly at the threshold, →1 as similarity grows past it."""
    return round(1.0 / (1.0 + math.exp(-steepness * (sim - threshold))), 4)


async def search_face(db: AsyncSession, principal: Principal, *, data: bytes, content_type: Optional[str],
                      filename: Optional[str], embedding: EmbeddingService, threshold: Optional[float] = None,
                      top_k: Optional[int] = None, settings: Optional[Settings] = None) -> Dict[str, Any]:
    s = settings or get_settings()
    threshold = s.FACE_SIMILARITY_THRESHOLD if threshold is None else threshold
    top_k = top_k or s.FACE_SEARCH_TOP_K
    t0 = time.perf_counter()
    tenant_id = principal.tenant_id
    quota = QuotaReservation(db, tenant_id)
    log_row = SearchLog(tenant_id=tenant_id, user_id=principal.user_id, api_key_id=principal.api_key_id,
                        status="failed", request_id=request_id_ctx.get())
    try:
        await quota.reserve_search(principal.package)
        img = embedding.validate_image(data, content_type, filename)
        query_face = await embedding.single_face(img, allow_multiple=True)  # search uses the dominant face
        vec = query_face.embedding.tolist()

        dist = Face.embedding.cosine_distance(vec)
        q = (select(Face.id, Face.person_id, Face.image_id, (1 - dist).label("similarity"))
             .where(Face.tenant_id == tenant_id, Face.deleted_at.is_(None))   # tenant isolation
             .order_by(dist).limit(max(top_k * 4, 20)))
        try:
            rows = (await db.execute(q)).all()
        except Exception as e:
            log.exception("vector_search_failed")
            raise SearchFailed(details={"reason": type(e).__name__}) from e

        # best face per person, above threshold
        best: Dict[uuid.UUID, Dict[str, Any]] = {}
        for face_id, pid, image_id, sim in rows:
            sim = float(sim)
            if sim < threshold:
                continue
            if pid not in best or sim > best[pid]["similarity"]:
                best[pid] = {"face_id": face_id, "image_id": image_id, "similarity": sim}
        ranked = sorted(best.items(), key=lambda kv: kv[1]["similarity"], reverse=True)[:top_k]

        persons = {}
        if ranked:
            pres = await db.execute(select(Person).where(Person.id.in_([pid for pid, _ in ranked]),
                                                         Person.tenant_id == tenant_id, Person.deleted_at.is_(None)))
            persons = {p.id: p for p in pres.scalars()}

        candidates = []
        for pid, m in ranked:
            p = persons.get(pid)
            if p is None:
                continue
            candidates.append({"similarity": round(m["similarity"], 4),
                               "confidence": confidence_from_similarity(m["similarity"], threshold),
                               "face_id": str(m["face_id"]), "image_id": str(m["image_id"]),
                               "person": person_to_dict(p)})
        top = candidates[0] if candidates else None
        ms = int((time.perf_counter() - t0) * 1000)
        log_row.status = "match" if top else "no_match"
        log_row.matched_person_id = uuid.UUID(top["person"]["id"]) if top else None
        log_row.similarity = top["similarity"] if top else None
        log_row.result_count = len(candidates)
        log_row.processing_time_ms = ms
        db.add(log_row)
        await usage_service.log_usage(db, tenant_id, "search", user_id=principal.user_id,
                                      api_key_id=principal.api_key_id, request_id=request_id_ctx.get())
        quota.commit()
        return {"match": top is not None, "confidence": top["confidence"] if top else 0.0,
                "similarity": top["similarity"] if top else None, "person": top["person"] if top else None,
                "face_id": top["face_id"] if top else None, "candidates": candidates,
                "threshold": threshold, "query": {"faces_detected": 1, "bbox": query_face.bbox},
                "processing_time_ms": ms}
    except AppError as e:
        log_row.status = "rejected" if e.status_code < 500 else "failed"
        log_row.error_code = e.code
        raise
    finally:
        if log_row.status not in ("match", "no_match"):
            await quota.release_all()
            log_row.processing_time_ms = int((time.perf_counter() - t0) * 1000)
            await _write_search_log(log_row)


async def _write_search_log(row: SearchLog) -> None:
    from app.core.database import get_sessionmaker
    try:
        async with get_sessionmaker()() as s:
            s.add(SearchLog(tenant_id=row.tenant_id, user_id=row.user_id, api_key_id=row.api_key_id,
                            status=row.status, error_code=row.error_code, processing_time_ms=row.processing_time_ms,
                            request_id=row.request_id))
            await s.commit()
    except Exception:
        log.warning("search_log_write_failed")


# --- faces -------------------------------------------------------------------

async def get_face(db: AsyncSession, tenant_id: uuid.UUID, face_id: uuid.UUID) -> Face:
    f = await db.get(Face, face_id)
    if f is None or f.tenant_id != tenant_id or f.deleted_at is not None:
        raise FaceNotFound()
    return f


async def list_faces(db: AsyncSession, tenant_id: uuid.UUID, *, person_id: Optional[uuid.UUID] = None,
                     limit: int = 50, offset: int = 0) -> tuple[List[Face], int]:
    base = select(Face).where(Face.tenant_id == tenant_id, Face.deleted_at.is_(None))
    if person_id:
        base = base.where(Face.person_id == person_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list((await db.execute(base.order_by(Face.created_at.desc()).limit(limit).offset(offset))).scalars())
    return rows, total


async def _delete_face_row(db: AsyncSession, tenant_id: uuid.UUID, face: Face, storage: StorageService) -> None:
    face.deleted_at = func.now()
    image = await db.get(Image, face.image_id)
    if image and image.deleted_at is None:
        image.deleted_at = func.now()
        await storage.delete_keys(image.storage_key, image.thumbnail_key)
        await release_storage(db, tenant_id, image.size_bytes)
    await db.flush()


async def delete_face(db: AsyncSession, tenant_id: uuid.UUID, face_id: uuid.UUID, storage: StorageService) -> Face:
    face = await get_face(db, tenant_id, face_id)
    await _delete_face_row(db, tenant_id, face, storage)
    return face


async def face_to_dict(f: Face, storage: Optional[StorageService] = None, with_urls: bool = False) -> Dict[str, Any]:
    d = {"id": str(f.id), "person_id": str(f.person_id), "image_id": str(f.image_id), "bbox": f.bbox,
         "det_score": f.det_score, "model": f.model_name, "created_at": f.created_at}
    if with_urls and storage is not None and f.image is not None:
        d["image_url"] = await storage.signed_url(f.image.storage_key)
        d["thumbnail_url"] = await storage.signed_url(f.image.thumbnail_key)
        d["image"] = {"mime_type": f.image.mime_type, "size_bytes": f.image.size_bytes,
                      "width": f.image.width, "height": f.image.height}
    return d


async def tenant_counts(db: AsyncSession, tenant_id: uuid.UUID) -> Dict[str, int]:
    persons = (await db.execute(select(func.count()).select_from(Person)
                                .where(Person.tenant_id == tenant_id, Person.deleted_at.is_(None)))).scalar_one()
    faces = (await db.execute(select(func.count()).select_from(Face)
                              .where(Face.tenant_id == tenant_id, Face.deleted_at.is_(None)))).scalar_one()
    return {"persons": persons, "faces": faces}
