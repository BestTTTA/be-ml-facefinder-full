"""EmbeddingService: image bytes → validated image → face embeddings.

Owns the single engine instance per process and runs inference in a thread pool
so the event loop is never blocked.
"""
import asyncio
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import Settings, get_settings
from app.core.exceptions import EmbeddingFailed, InvalidImage, MultipleFacesDetected, NoFaceDetected
from app.core.logging import get_logger
from app.engines.base import DetectedFace, FaceEngine

log = get_logger(__name__)

_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass
class ValidatedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str
    array_bgr: np.ndarray


class EmbeddingService:
    def __init__(self, engine: FaceEngine, settings: Optional[Settings] = None, workers: int = 2):
        self.engine = engine
        self.settings = settings or get_settings()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="face-engine")

    # --- lifecycle ---
    def load(self) -> None:
        if not self.engine.is_ready():
            self.engine.load()
            log.info("face_engine_loaded", extra={"engine": self.engine.name, "dimension": self.engine.dimension})
        if self.engine.dimension != self.settings.EMBEDDING_DIMENSION:
            raise RuntimeError(f"Engine dimension {self.engine.dimension} != EMBEDDING_DIMENSION "
                               f"{self.settings.EMBEDDING_DIMENSION}")

    def is_ready(self) -> bool:
        return self.engine.is_ready()

    # --- validation ---
    def validate_image(self, data: bytes, declared_mime: Optional[str], filename: Optional[str]) -> ValidatedImage:
        s = self.settings
        if not data:
            raise InvalidImage("Empty file")
        if len(data) > s.FACE_MAX_FILE_SIZE_BYTES:
            raise InvalidImage(f"File exceeds maximum size of {s.FACE_MAX_FILE_SIZE_BYTES} bytes",
                               details={"max_bytes": s.FACE_MAX_FILE_SIZE_BYTES, "size": len(data)})
        if filename:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in {"jpg", "jpeg", "png", "webp"}:
                raise InvalidImage("Unsupported file extension", details={"extension": ext})
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format or ""
                real_mime = _MIME_BY_FORMAT.get(fmt)
                if real_mime not in s.allowed_mime_types:
                    raise InvalidImage("Unsupported image type", details={"detected": real_mime or fmt})
                rgb = ImageOps.exif_transpose(im).convert("RGB")
                arr = np.asarray(rgb)[:, :, ::-1].copy()  # RGB -> BGR for the engine
                width, height = rgb.size
        except InvalidImage:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise InvalidImage("Corrupted or unreadable image", details={"reason": str(e)[:200]}) from e
        if declared_mime and declared_mime not in s.allowed_mime_types:
            raise InvalidImage("Unsupported content type", details={"content_type": declared_mime})
        return ValidatedImage(data=data, mime_type=real_mime, width=width, height=height,
                              sha256=hashlib.sha256(data).hexdigest(), array_bgr=arr)

    # --- inference ---
    async def detect_faces(self, image: ValidatedImage) -> List[DetectedFace]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, self.engine.detect, image.array_bgr)
        except Exception as e:  # engine failure must not leak internals
            log.exception("embedding_failed")
            raise EmbeddingFailed(details={"reason": type(e).__name__}) from e

    async def single_face(self, image: ValidatedImage, allow_multiple: Optional[bool] = None) -> DetectedFace:
        faces = await self.detect_faces(image)
        if not faces:
            raise NoFaceDetected()
        allow_multiple = self.settings.FACE_ALLOW_MULTIPLE_FACES if allow_multiple is None else allow_multiple
        if len(faces) > 1 and not allow_multiple:
            raise MultipleFacesDetected(details={"faces_detected": len(faces)})
        # largest / most confident face first
        faces.sort(key=lambda f: (f.det_score, (f.bbox.get("x2", 0) - f.bbox.get("x1", 0))), reverse=True)
        return faces[0]


# --- process-wide singleton -------------------------------------------------
_service: Optional[EmbeddingService] = None


def build_engine(settings: Settings) -> FaceEngine:
    if settings.FACE_ENGINE == "fake":
        from app.engines.fake_engine import FakeFaceEngine
        return FakeFaceEngine(dimension=settings.EMBEDDING_DIMENSION)
    from app.engines.insightface_engine import InsightFaceEngine
    return InsightFaceEngine(model_name=settings.FACE_MODEL_NAME, model_root=settings.FACE_MODEL_PATH,
                             ctx_id=settings.FACE_CTX_ID, det_size=settings.FACE_DET_SIZE,
                             dimension=settings.EMBEDDING_DIMENSION)


def get_embedding_service() -> EmbeddingService:
    global _service
    if _service is None:
        s = get_settings()
        _service = EmbeddingService(build_engine(s), s)
    return _service


def set_embedding_service(service: Optional[EmbeddingService]) -> None:
    global _service
    _service = service
