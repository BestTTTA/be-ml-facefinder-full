"""InsightFace (RetinaFace + ArcFace `buffalo_l`) engine — same model the legacy app used."""
from typing import List, Optional

import numpy as np

from app.engines.base import DetectedFace


class InsightFaceEngine:
    name = "insightface"

    def __init__(self, model_name: str = "buffalo_l", model_root: Optional[str] = None,
                 ctx_id: int = -1, det_size: int = 640, dimension: int = 512):
        self.model_name = model_name
        self.model_root = model_root
        self.ctx_id = ctx_id
        self.det_size = det_size
        self.dimension = dimension
        self._app = None

    def load(self) -> None:
        from insightface.app import FaceAnalysis  # imported lazily: heavy, optional in tests

        kwargs = {"name": self.model_name}
        if self.model_root:
            kwargs["root"] = self.model_root
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if self.ctx_id >= 0 else ["CPUExecutionProvider"]
        app = FaceAnalysis(providers=providers, **kwargs)
        app.prepare(ctx_id=self.ctx_id, det_size=(self.det_size, self.det_size))
        self._app = app

    def is_ready(self) -> bool:
        return self._app is not None

    def detect(self, image_bgr: np.ndarray) -> List[DetectedFace]:
        if self._app is None:
            raise RuntimeError("Face engine not loaded")
        out: List[DetectedFace] = []
        for f in self._app.get(image_bgr):
            emb = getattr(f, "normed_embedding", None)
            if emb is None:
                emb = f.embedding / (np.linalg.norm(f.embedding) + 1e-12)
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            out.append(DetectedFace(embedding=np.asarray(emb, dtype=np.float32),
                                    bbox={"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                                    det_score=float(getattr(f, "det_score", 0.0))))
        return out
